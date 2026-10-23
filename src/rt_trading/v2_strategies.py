"""v2 2단계 후보 전략(SPEC 5절, PRD 03). 숫자는 시험 전에 고정한다."""

from datetime import datetime, timedelta

from rt_trading.engine import Order, View
from rt_trading.market_data import open_bar

KODEX200, INVERSE = "069500", "114800"
TREND_DAYS = 200
SETTINGS = {  # 후보 → (엔진 시작 위치, 며칠 보유 방식)
    "turn_of_month": (1, True),
    "trend_200": (TREND_DAYS, True),
    "last_half_hour": (1, False),
    "overnight_trend": (TREND_DAYS, True),
}


def _shift(clock: str, minutes: int) -> str:
    return (datetime.strptime(clock, "%H:%M") + timedelta(minutes=minutes)).strftime("%H:%M")


def _at(view: View, clock: str) -> bool:
    """지금이 clock 분이고 그날 KODEX 200 봉에 그 시각의 봉이 있는가."""
    bars = view.today[KODEX200]
    return view.time == clock and bool(bars) and bars[-1].time == clock


def _target(view: View) -> str:
    """전 거래일 추세가 위면 KODEX 200, 아래면 인버스. adj_closes의 마지막 200개 합과 200 × 마지막 값을 비교한다."""
    window = view.adj_closes[-TREND_DAYS:]
    return KODEX200 if TREND_DAYS * window[-1] > sum(window) else INVERSE


def _sell_day(calendar: list[str], day: str) -> str | None:
    """day가 사는 날(그 달 마지막 거래일의 전 거래일)이면 그 회차의 파는 날(다음 달 셋째 거래일). 아니거나 파는 날이 달력에 없으면 None."""
    index = calendar.index(day)
    if index + 1 >= len(calendar):
        return None
    last = calendar[index + 1]
    if index + 2 < len(calendar) and calendar[index + 2][:7] == last[:7]:
        return None
    year, month = int(last[:4]), int(last[5:7])
    following = [date for date in calendar if date[:7] == f"{year + month // 12}-{month % 12 + 1:02d}"]
    return following[2] if len(following) >= 3 else None


class TurnOfMonth:
    """①: 사는 날 마감 10분 전에 KODEX 200을 사서 그 회차의 파는 날 마감 10분 전에 판다."""

    def __call__(self, view: View) -> Order | None:
        if view.defect or view.pending or not _at(view, _shift(view.session.close, -10)):
            return None
        if view.holding:
            return Order("sell", None, "signal") if _sell_day(view.calendar, view.holding.entry[:10]) == view.date else None
        sell_day = _sell_day(view.calendar, view.date)
        if sell_day and sell_day not in view.defect_days:
            return Order("buy", KODEX200, "signal")
        return None


class Trend200:
    """②: 마감 10분 전에 목표 종목을 들고 있게 한다. 이미 들고 있으면 그대로 둔다."""

    def __call__(self, view: View) -> Order | None:
        if view.defect or view.pending or not _at(view, _shift(view.session.close, -10)):
            return None
        target = _target(view)
        if not view.holding:
            return Order("buy", target, "signal")
        return Order("switch", target, "switch") if view.holding.symbol != target else None


class LastHalfHour:
    """③: 개장 30분 뒤 KODEX 200 종가가 기준가보다 높은 날 마감 30분 전에 사서, 엔진이 종가에 판다."""

    def __call__(self, view: View) -> Order | None:
        if view.defect or view.pending or view.holding or not _at(view, _shift(view.session.close, -30)):
            return None
        bars, signal_time = view.today[KODEX200], _shift(view.session.open, 30)
        signal = next((bar for bar in bars if bar.time == signal_time), None)
        opening = open_bar(bars)
        if signal and opening and opening.time <= signal_time and signal.close > view.base[KODEX200]:
            return Order("buy", KODEX200, "signal")
        return None


class OvernightTrend:
    """④: 마감 10분 전에 목표 종목을 사고, 종가 동시호가 분에 팔아 다음 거래일 시가에 체결되게 한다."""

    def __call__(self, view: View) -> Order | None:
        if view.defect or view.pending:
            return None
        if view.time == view.session.bars[-1]:
            return Order("sell", None, "signal") if view.holding else None
        if _at(view, _shift(view.session.close, -10)) and not view.holding:
            return Order("buy", _target(view), "signal")
        return None


CANDIDATES = {"turn_of_month": TurnOfMonth, "trend_200": Trend200, "last_half_hour": LastHalfHour, "overnight_trend": OvernightTrend}

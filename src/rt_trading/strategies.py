"""후보 전략: 노이즈 영역 돌파(SPEC 5.1). 숫자는 백테스트 전에 고정한다."""

from fractions import Fraction

from rt_trading import sessions
from rt_trading.engine import Order, View
from rt_trading.market_data import open_bar

KODEX200, INVERSE = "069500", "114800"
LOOKBACK_DAYS = 14
FIRST_DECISION_MINUTES = 30
DECISION_INTERVAL_MINUTES = 30
LAST_DECISION_BEFORE_CLOSE_MINUTES = 30
PARAMS = {
    "sigma_lookback_days": LOOKBACK_DAYS,
    "first_decision_after_open_minutes": FIRST_DECISION_MINUTES,
    "decision_interval_minutes": DECISION_INTERVAL_MINUTES,
    "last_decision_before_close_minutes": LAST_DECISION_BEFORE_CLOSE_MINUTES,
}


def decision_times(session: sessions.Session) -> list[str]:
    """개장 30분 뒤부터 30분마다 마감 30분 전까지의 판단 시각."""
    first = _minutes(session.open) + FIRST_DECISION_MINUTES
    last = _minutes(session.close) - LAST_DECISION_BEFORE_CLOSE_MINUTES
    return [_clock(minute) for minute in range(first, last + 1, DECISION_INTERVAL_MINUTES)]


class NoiseBreakout:
    """노이즈 영역 돌파. 시뮬레이션 한 번에 하나씩 만든다. 지난 거래일의 시가와 봉 종가는 날짜마다 한 번만 정리한다."""

    def __init__(self) -> None:
        self._table = sessions.exceptions()
        self._days: dict[str, tuple[int, int, dict[str, int]] | None] = {}  # 날짜 → (개장 분, 시가, 시각별 종가)

    def __call__(self, view: View) -> Order | None:
        if view.defect or view.time not in decision_times(view.session):
            return None
        bars = view.today[KODEX200]
        opening, volume = open_bar(bars), sum(bar.volume for bar in bars)
        if opening is None or not volume or bars[-1].time != view.time:
            return None
        sigma = self._sigma(view, _minutes(view.time) - _minutes(view.session.open))
        if sigma is None:
            return None
        # 경계와 종가가 같을 때 엄격한 비교가 뒤집히지 않도록 분수로 정확히 계산한다
        base = view.base[KODEX200]
        upper, lower = max(opening.open, base) * (1 + sigma), min(opening.open, base) * (1 - sigma)
        vwap = Fraction(sum((bar.high + bar.low + bar.close) * bar.volume for bar in bars), 3 * volume)
        close = bars[-1].close
        holding = view.holding.symbol if view.holding else None
        if holding == KODEX200:
            if close < lower:
                return Order("switch", INVERSE, "switch")
            if close < max(upper, vwap):
                return Order("sell", None, "stop")
        elif holding == INVERSE:
            if close > upper:
                return Order("switch", KODEX200, "switch")
            if close > min(lower, vwap):
                return Order("sell", None, "stop")
        elif close > upper:
            return Order("buy", KODEX200, "signal")
        elif close < lower:
            return Order("buy", INVERSE, "signal")
        return None

    def _sigma(self, view: View, minute: int) -> Fraction | None:
        """σ(m): 직전 14거래일의 |m분 시점 봉 종가 ÷ 그날 시가 − 1| 평균. 봉이 없는 날이 있으면 None."""
        days = view.past_days[-LOOKBACK_DAYS:]
        moves = []
        for day in days:
            if day not in self._days:
                bars = view.past[KODEX200][day]
                opening = open_bar(bars)
                start = _minutes(sessions.session(day, self._table).open)
                self._days[day] = opening and (start, opening.open, {bar.time: bar.close for bar in bars})
            summary = self._days[day]
            close = summary and summary[2].get(_clock(summary[0] + minute))
            if close is None:
                return None
            moves.append(Fraction(abs(close - summary[1]), summary[1]))
        return sum(moves) / len(moves) if len(moves) == LOOKBACK_DAYS else None


def _minutes(clock: str) -> int:
    hour, minute = clock.split(":")
    return int(hour) * 60 + int(minute)


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"

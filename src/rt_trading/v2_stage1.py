"""v2 1단계: v0 일봉으로 며칠 보유 후보 ①②④를 시험한다(v2 SPEC 4.2·5절, PRD 01).

가격·자산·판정은 decimal 기본 문맥으로 계산하고, JSON에 쓸 때만 float으로 바꾼다.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

KODEX200, INVERSE = "069500", "114800"
C = Decimal("0.000188")  # 매수·매도마다 수수료
TAX = Decimal("0.154")  # 인버스 매매 이익에 매기는 세율
TREND_DAYS = 200
# v0 코드가 만드는 daily_candles 표: (열 이름, 선언 형식, NOT NULL, 기본 키 위치)
COLUMNS = [
    ("symbol", "TEXT", 1, 1),
    ("date", "TEXT", 1, 2),
    ("open", "REAL", 1, 0),
    ("close", "REAL", 1, 0),
    ("adj_open", "REAL", 1, 0),
    ("adj_close", "REAL", 1, 0),
]
PARAMS = {
    "turn_of_month": {"buy_trading_days_before_month_end": 1, "sell_trading_day_of_next_month": 3},
    "trend_200": {"trend_days": TREND_DAYS},
    "overnight_trend": {"trend_days": TREND_DAYS},
}


class Stage1Error(Exception):
    """1단계 데이터를 쓸 수 없다. code는 V2_DATA_MISSING 또는 V2_STAGE1_DATA_INVALID다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class Day(NamedTuple):
    date: str
    open: Decimal  # 원가격
    close: Decimal
    adj_open: Decimal
    adj_close: Decimal


class Data(NamedTuple):
    k200: list[Day]  # 날짜 오름차순. 이 날짜들이 달력이다
    inverse: list[Day]


class Trade(NamedTuple):
    symbol: str
    buy: str  # 산 날
    sell: str  # 판 날
    net: Decimal  # 수수료·세금을 뺀 수익률
    gross: Decimal  # 비용 전 수익률 g − 1


class Result(NamedTuple):
    closes: list[tuple[str, Decimal]]  # 시험 기간의 (날짜, 종가 자산)
    trades: list[Trade]


def load(path: Path) -> Data:
    """v0 일봉을 읽고 SPEC 3절의 1단계 확인을 한다. 어기면 Stage1Error다."""
    if not path.exists():
        raise Stage1Error("V2_DATA_MISSING", f"{path} 파일이 없다")
    with closing(sqlite3.connect(path)) as db:
        columns = [(name, kind, notnull, pk) for _, name, kind, notnull, _, pk in db.execute("PRAGMA table_info(daily_candles)")]
        if columns != COLUMNS:
            raise Stage1Error("V2_STAGE1_DATA_INVALID", "daily_candles 표나 그 열·기본 키가 v0와 다르다")
        query = "SELECT date, open, close, adj_open, adj_close FROM daily_candles WHERE symbol = ? ORDER BY date"
        k200, inverse = (
            [Day(date, *(Decimal(str(price)) for price in prices)) for date, *prices in db.execute(query, (symbol,))]
            for symbol in (KODEX200, INVERSE)
        )
    if not k200 or not inverse:
        raise Stage1Error("V2_STAGE1_DATA_INVALID", "두 종목 중 행이 없는 종목이 있다")
    if any(price <= 0 for day in k200 + inverse for price in day[1:]):
        raise Stage1Error("V2_STAGE1_DATA_INVALID", "0 이하인 가격이 있다")
    first = inverse[0].date
    if [day.date for day in k200 if day.date >= first] != [day.date for day in inverse]:
        raise Stage1Error("V2_STAGE1_DATA_INVALID", f"인버스 첫 거래일({first})부터 두 종목의 거래일이 다르다")
    if sum(day.date < first for day in k200) < TREND_DAYS:
        raise Stage1Error("V2_STAGE1_DATA_INVALID", f"인버스 첫 거래일 전 KODEX 200 거래일이 {TREND_DAYS}일보다 적다")
    return Data(k200, inverse)


class _Account:
    """자산 1에서 시작하는 계좌. 들고 있을 때 equity는 산 직전 자산이다."""

    def __init__(self) -> None:
        self.equity = Decimal(1)
        self.holding: tuple[str, str, Decimal] | None = None  # (종목, 산 날, 산 가격)
        self.trades: list[Trade] = []

    def buy(self, symbol: str, date: str, price: Decimal) -> None:
        self.holding = (symbol, date, price)

    def sell(self, date: str, price: Decimal) -> None:
        symbol, bought, paid = self.holding
        g = price / paid
        tax = TAX * max(Decimal(0), g - 1) if symbol == INVERSE else Decimal(0)
        net = (g * (1 - C) - tax) / (1 + C) - 1
        self.equity *= 1 + net
        self.trades.append(Trade(symbol, bought, date, net, g - 1))
        self.holding = None

    def mark(self, close: dict[str, Decimal]) -> Decimal:
        if not self.holding:
            return self.equity
        symbol, _, paid = self.holding
        return self.equity / (1 + C) * close[symbol] / paid


def _prices(data: Data) -> dict[str, dict[str, Day]]:
    return {symbol: {day.date: day for day in days} for symbol, days in ((KODEX200, data.k200), (INVERSE, data.inverse))}


def _run(data: Data, start: str, step: Callable[[_Account, int, str, bool], None]) -> Result:
    """start부터 달력의 거래일마다 step(계좌, 달력 위치, 날짜, 마지막 날)을 부르고 종가 자산을 기록한다."""
    prices, calendar = _prices(data), [day.date for day in data.k200]
    account, closes = _Account(), []
    for index in range(calendar.index(start), len(calendar)):
        date = calendar[index]
        last = index == len(calendar) - 1
        step(account, index, date, last)
        if last and account.holding:  # 마지막 날에는 남은 보유를 종가에 판다
            account.sell(date, prices[account.holding[0]][date].adj_close)
        closes.append((date, account.mark({symbol: days[date].adj_close for symbol, days in prices.items() if date in days})))
    return Result(closes, account.trades)


def turn_of_month(data: Data) -> Result:
    """①: 달 마지막 거래일의 전 거래일 종가에 사서 다음 달 셋째 거래일 종가에 판다."""
    calendar = [day.date for day in data.k200]
    adj_close = {day.date: day.adj_close for day in data.k200}
    months: dict[str, list[int]] = {}
    for index, date in enumerate(calendar):
        months.setdefault(date[:7], []).append(index)
    sell_on = {}  # 산 날 → 판 날. 판 날이 달력에 있는 회차만 넣는다
    for month, indexes in months.items():
        year, number = int(month[:4]), int(month[5:])
        following = months.get(f"{year + number // 12}-{number % 12 + 1:02d}", [])
        if indexes[-1] >= 1 and len(following) >= 3:
            sell_on[calendar[indexes[-1] - 1]] = calendar[following[2]]

    def step(account: _Account, index: int, date: str, last: bool) -> None:
        if account.holding and sell_on[account.holding[1]] == date:
            account.sell(date, adj_close[date])
        if date in sell_on and not last:
            account.buy(KODEX200, date, adj_close[date])

    return _run(data, calendar[0], step)


def trends(data: Data) -> dict[str, bool]:
    """날짜 → 추세가 위인가. 그날을 포함한 직전 200거래일 수정 종가 합 S와 200 × 그날 수정 종가를 비교한다."""
    closes = [day.adj_close for day in data.k200]
    result, total = {}, Decimal(0)
    for index, close in enumerate(closes):
        total += close - (closes[index - TREND_DAYS] if index >= TREND_DAYS else 0)
        if index >= TREND_DAYS - 1:
            result[data.k200[index].date] = TREND_DAYS * close > total
    return result


def _targets(data: Data) -> Callable[[int], str]:
    """달력 위치 → 그날의 목표 종목(전 거래일 추세가 위면 KODEX 200, 아래면 인버스)."""
    up, calendar = trends(data), [day.date for day in data.k200]
    return lambda index: KODEX200 if up[calendar[index - 1]] else INVERSE


def trend_200(data: Data) -> Result:
    """②: 종가에 목표 종목을 들고 있게 한다. 이미 들고 있으면 그대로 둔다."""
    target, prices = _targets(data), _prices(data)

    def step(account: _Account, index: int, date: str, last: bool) -> None:
        if last:
            return
        wanted = target(index)
        if account.holding and account.holding[0] != wanted:
            account.sell(date, prices[account.holding[0]][date].adj_close)
        if not account.holding:
            account.buy(wanted, date, prices[wanted][date].adj_close)

    return _run(data, data.inverse[0].date, step)


def overnight_trend(data: Data) -> Result:
    """④: 종가에 목표 종목을 사서 다음 거래일 시가에 판다."""
    target, prices = _targets(data), _prices(data)

    def step(account: _Account, index: int, date: str, last: bool) -> None:
        if account.holding:
            account.sell(date, prices[account.holding[0]][date].adj_open)
        if not last:
            wanted = target(index)
            account.buy(wanted, date, prices[wanted][date].adj_close)

    return _run(data, data.inverse[0].date, step)


def summary(candidate_id: str, result: Result) -> dict:
    """SPEC 7절 stage1.json의 candidates 항목 하나. 값은 JSON에 쓸 수 있게 float이다."""
    closes, nets = result.closes, [trade.net for trade in result.trades]
    days, final = len(closes), closes[-1][1]
    peak, drawdown, below_limit = Decimal(1), Decimal(0), True
    for _, equity in closes:  # E_0 = 1부터 본다
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
        below_limit = below_limit and equity * 5 > peak * 4  # 낙폭 20% 미만인지는 나눗셈 없이 판정한다
    boundary = closes[days // 2 - 1][1] if days // 2 else Decimal(1)
    yearly, previous = {}, Decimal(1)
    for year, equity in {date[:4]: equity for date, equity in closes}.items():
        yearly[year] = float(equity / previous - 1)
        previous = equity
    gross = Decimal(1)
    for trade in result.trades:
        gross *= 1 + trade.gross
    first, second = boundary - 1, final / boundary - 1
    criteria = {"net_positive": final > 1, "drawdown_below_limit": below_limit, "both_halves_positive": first > 0 and second > 0}
    return {
        "id": candidate_id,
        "params": PARAMS[candidate_id],
        "start": closes[0][0],
        "end": closes[-1][0],
        "trading_days": days,
        "final_equity": float(final),
        "total_return": float(final - 1),
        "annualized_return": float(final) ** (252 / days) - 1,
        "max_drawdown": float(drawdown),
        "first_half_return": float(first),
        "second_half_return": float(second),
        "yearly_returns": yearly,
        "trade_count": len(nets),
        "win_rate": sum(net > 0 for net in nets) / len(nets) if nets else None,
        "avg_trade_return": float(sum(nets) / len(nets)) if nets else None,
        "gross_total_return": float(gross - 1),
        "criteria": criteria,
        "passed": all(criteria.values()),
    }


def build(data: Data) -> dict:
    """stage1.json의 내용."""
    results = {"turn_of_month": turn_of_month(data), "trend_200": trend_200(data), "overnight_trend": overnight_trend(data)}
    return {
        "run": {"data_start": data.k200[0].date, "data_end": data.k200[-1].date, "commission_rate": float(C), "inverse_tax_rate": float(TAX)},
        "candidates": [summary(candidate_id, result) for candidate_id, result in results.items()],
    }

"""시뮬레이션 엔진. 분마다 주문을 체결하고 자산을 평가하며 손실 한도를 지킨다(SPEC 4절)."""

from collections.abc import Callable
from datetime import date as Date
from typing import NamedTuple

from rt_trading.market_data import Bar, MarketData
from rt_trading.sessions import Session

INITIAL_CASH = 500_000
WARMUP_DAYS = 14  # 시뮬레이션은 데이터의 15번째 거래일부터 한다
KODEX200, INVERSE = "069500", "114800"
TICK_CHANGE = "2023-12-11"  # 이날부터 2,000원 미만 ETF 호가 단위가 1원


class Order(NamedTuple):
    kind: str  # buy, sell, switch
    symbol: str | None  # buy·switch로 살 종목. sell은 None
    reason: str  # signal, stop, switch


class Holding(NamedTuple):
    symbol: str
    quantity: int
    price: int  # 매수 체결가
    cost: int  # 매수 금액 + 수수료
    entry: str  # 매수 체결 시각
    received: int = 0  # 보유 중 받은 분배금 − 세금


class View(NamedTuple):
    """전략이 볼 수 있는 것. 지난 거래일과 오늘 이 분까지의 봉뿐이다."""

    date: str
    time: str  # 방금 끝난 봉 시각
    session: Session
    base: dict[str, int]  # 종목 → 오늘 기준가
    defect: bool
    today: dict[str, list[Bar]]  # 종목 → 오늘 이 분까지의 봉
    past: dict[str, dict[str, list[Bar]]]  # 종목 → 지난 거래일 → 봉
    past_days: list[str]  # 지난 거래일 오름차순
    holding: Holding | None
    pending: bool  # 대기 중인 주문이 있는가
    calendar: list[str] = []  # 거래일 날짜 전체
    defect_days: list[str] = []  # 결함일 표
    adj_closes: list[int] = []  # 전 거래일까지의 KODEX 200 일봉 수정 종가(날짜 오름차순)


class Fill(NamedTuple):
    time: str
    symbol: str
    side: str  # buy, sell
    quantity: int
    price: int
    fee: int
    tax: int
    reason: str  # signal, stop, switch, limit, close


class Trade(NamedTuple):
    symbol: str
    entry: str
    exit: str
    quantity: int
    pnl: int  # 수수료와 세금을 뺀 순손익


class Discard(NamedTuple):
    time: str
    kind: str  # 버리거나 취소한 주문의 종류
    symbol: str | None
    reason: str  # entry_blocked(진입 막힘), pending(대기 주문 있음), after_close(마감 지남), last_day(마지막 거래일), switch_head(v3 부하 교체)


class PreOpen(NamedTuple):
    """v3 개장 전 처리(v3 PRD 02). 그날 첫 봉 전에 대기 매수를 취소하거나, 보유를 팔거나, 시가 동시호가에 살 종목을 정한다."""

    cancel: bool = False
    sell: bool = False
    buy: str | None = None


class Records(NamedTuple):
    equity: list[tuple[str, int]]  # (봉 시각, 자산). 첫 값은 시뮬레이션 첫 봉 전의 시작 자금
    closes: dict[str, int]  # 날짜 → 마감 자산
    fills: list[Fill]
    trades: list[Trade]
    limits: list[tuple[str, str]]  # (시각, daily·weekly·cumulative). 기간마다 처음 한 번
    discarded: list[Discard]
    distributions: list[tuple[str, str, int, int]] = []  # (날짜, 종목, 분배금, 세금)


class _Buy(NamedTuple):
    symbol: str
    kind: str  # buy 또는 switch
    reason: str
    after_sell: bool  # 전환 매수라 매도 체결을 기다린다


def run(
    data: MarketData,
    strategy: Callable[[View], Order | None],
    start: int = WARMUP_DAYS,
    overnight: bool = False,
    calendar: list[str] | None = None,
    final_day: str | None = None,
    overnight_on: Callable[[str], bool] | None = None,
    pre_open: Callable[[View], PreOpen | None] | None = None,
) -> Records:
    """start 거래일부터 마지막 거래일까지 분마다 PRD 03의 순서로 처리한다.

    overnight이면 v2 PRD 02의 며칠 보유 방식이다. 기본값으로 부르면 v1과 같다.
    v3 PRD 02의 선택 인자: calendar는 전략에 넘기는 달력, final_day는 마지막 거래일로 볼 날(데이터에 없으면 어느 날도 아니다),
    overnight_on은 날짜별 며칠 보유 방식, pre_open은 날마다 첫 봉 전에 부르는 개장 전 처리다. 주지 않으면 지금과 같다.
    """
    symbols = sorted(data.bars)  # 입력 순서와 관계없이 같은 결과를 낸다
    defects = set(data.defects)
    records = Records([("", INITIAL_CASH)], {}, [], [], [], [], [])
    cash, holding, sell_reason, buy = INITIAL_CASH, None, None, None
    past, past_days, adj_closes = {symbol: {} for symbol in symbols}, [], []
    last_close = weekly_base = INITIAL_CASH
    week, cumulative_hit = None, False
    last_price = {}  # 종목 → 가장 최근 봉 종가. 거래일이 바뀌어도 이어 쓴다
    days = calendar or data.days  # 전략에 넘기는 달력
    for index, date in enumerate(data.days):
        if index >= start:
            final = date == final_day if final_day else index == len(data.days) - 1
            overnight_today = overnight_on(date) if overnight_on else overnight
            settle_at_close = not overnight_today or final
            if Date.fromisoformat(date).isocalendar()[:2] != week:
                week, weekly_base, weekly_hit = Date.fromisoformat(date).isocalendar()[:2], last_close, False
            daily_base, daily_hit = last_close, False
            session, close_time = data.sessions[date], data.sessions[date].bars[-1]
            bars = {symbol: {bar.time: bar for bar in data.bars[symbol].get(date, [])} for symbol in symbols}
            today, opened = {symbol: [] for symbol in symbols}, set()
            base = {symbol: data.base[symbol][date] for symbol in symbols}
            times = sorted(set().union(*bars.values()))

            # 첫 봉 전: 들고 있는 종목의 분배락일이면 분배금 − 세금을 받는다
            if holding and base[holding.symbol] < data.daily[holding.symbol][data.days[index - 1]][0]:
                amount = (data.daily[holding.symbol][data.days[index - 1]][0] - base[holding.symbol]) * holding.quantity
                tax = amount * 154 // 1000
                cash += amount - tax
                holding = holding._replace(received=holding.received + amount - tax)
                records.distributions.append((date, holding.symbol, amount, tax))
            if overnight_today and final and buy:
                records.discarded.append(Discard(f"{date} {times[0]}", buy.kind, buy.symbol, "last_day"))
                buy = None
            if pre_open:
                pending = bool(sell_reason or buy)
                empty = {symbol: [] for symbol in symbols}
                plan = pre_open(
                    View(date, session.open, session, base, date in defects, empty, past, past_days, holding, pending, days, data.defects, adj_closes)
                )
                stamp = f"{date} {session.open}"
                if plan and plan.cancel and buy:
                    records.discarded.append(Discard(stamp, buy.kind, buy.symbol, "switch_head"))
                    buy = None
                if plan and plan.sell and holding:
                    sell_reason = "switch_head"
                if plan and plan.buy:
                    selling = bool(plan.sell and holding)
                    if holding and not selling:
                        records.discarded.append(Discard(stamp, "buy", plan.buy, "pending"))
                    elif final:
                        records.discarded.append(Discard(stamp, "buy", plan.buy, "last_day"))
                    elif cumulative_hit or weekly_hit:
                        records.discarded.append(Discard(stamp, "buy", plan.buy, "entry_blocked"))
                    elif buy:
                        records.discarded.append(Discard(stamp, "buy", plan.buy, "pending"))
                    else:
                        buy = _Buy(plan.buy, "buy", "signal", after_sell=selling)
            for time in times:
                stamp, now, auction = f"{date} {time}", {symbol: bars[symbol].get(time) for symbol in symbols}, {}
                for symbol, bar in now.items():
                    if bar:
                        today[symbol].append(bar)
                        last_price[symbol] = bar.close
                        auction[symbol] = time == close_time or (bar.volume > 0 and symbol not in opened)
                        if bar.volume:
                            opened.add(symbol)

                # 1. 종가 동시호가 분
                if time == close_time and settle_at_close:
                    if buy:
                        records.discarded.append(Discard(stamp, buy.kind, buy.symbol, "after_close"))
                        buy = None
                    if holding and not sell_reason:
                        sell_reason = "close"

                # 2~3. 매도 먼저, 매수 나중에 체결하고, 전량 청산된 거래를 확정한다
                if sell_reason and holding and _fillable(now[holding.symbol]):
                    bar = now[holding.symbol]
                    price = bar.open if auction[holding.symbol] else _tick_down(bar.open, date)
                    amount = price * holding.quantity
                    fee = _fee(amount)
                    gain = (price - holding.price) * holding.quantity
                    tax = gain * 154 // 1000 if holding.symbol == INVERSE and gain > 0 else 0
                    cash += amount - fee - tax
                    records.fills.append(Fill(stamp, holding.symbol, "sell", holding.quantity, price, fee, tax, sell_reason))
                    records.trades.append(
                        Trade(holding.symbol, holding.entry, stamp, holding.quantity, amount - fee - tax - holding.cost + holding.received)
                    )
                    holding, sell_reason = None, None
                    if buy:
                        buy = buy._replace(after_sell=False)
                if buy and not buy.after_sell and _fillable(now[buy.symbol]):
                    bar = now[buy.symbol]
                    price = bar.open if auction[buy.symbol] else _tick_up(bar.open, date)
                    quantity = _quantity(cash, price)
                    amount = price * quantity
                    fee = _fee(amount)
                    cash -= amount + fee
                    records.fills.append(Fill(stamp, buy.symbol, "buy", quantity, price, fee, 0, buy.reason))
                    holding, buy = Holding(buy.symbol, quantity, price, amount + fee, stamp), None

                # 4. 봉 종가로 자산을 평가한다
                equity = cash + (holding.quantity * last_price[holding.symbol] if holding else 0)
                records.equity.append((stamp, equity))

                # 5. 손실 한도. 동시에 닿으면 누적, 한 주, 하루 순서다
                hits = []
                if not cumulative_hit and equity <= INITIAL_CASH * 80 // 100:
                    cumulative_hit = True
                    hits.append("cumulative")
                if not weekly_hit and equity * 100 <= weekly_base * 95:
                    weekly_hit = True
                    hits.append("weekly")
                if not daily_hit and equity * 100 <= daily_base * 98:
                    daily_hit = True
                    hits.append("daily")
                if hits:
                    records.limits.extend((stamp, kind) for kind in hits)
                    if holding:
                        sell_reason = "limit"
                    if buy:
                        records.discarded.append(Discard(stamp, buy.kind, buy.symbol, "entry_blocked"))
                        buy = None

                # 6. 전략
                view = View(
                    date, time, session, base, date in defects, today, past, past_days, holding, bool(sell_reason or buy),
                    days, data.defects, adj_closes,
                )
                order = strategy(view)
                if order:
                    entry = order.kind != "sell"
                    if entry and time == close_time:
                        records.discarded.append(Discard(stamp, order.kind, order.symbol, "after_close"))
                    elif entry and overnight_today and final:
                        records.discarded.append(Discard(stamp, order.kind, order.symbol, "last_day"))
                    elif entry and (cumulative_hit or weekly_hit or daily_hit):
                        records.discarded.append(Discard(stamp, order.kind, order.symbol, "entry_blocked"))
                    elif sell_reason or buy:
                        records.discarded.append(Discard(stamp, order.kind, order.symbol, "pending"))
                    elif order.kind == "buy" and not holding:
                        buy = _Buy(order.symbol, "buy", order.reason, after_sell=False)
                    elif order.kind != "buy" and holding:
                        sell_reason = order.reason
                        if order.kind == "switch":
                            buy = _Buy(order.symbol, "switch", order.reason, after_sell=True)
            records.closes[date] = last_close = equity
        for symbol in symbols:
            past[symbol][date] = data.bars[symbol].get(date, [])
        past_days.append(date)
        adj_closes.append(data.daily[KODEX200][date][1])
    return records


def _fillable(bar: Bar | None) -> bool:
    return bar is not None and bar.volume > 0


def _fee(amount: int) -> int:
    """체결 금액 × 0.0188%, 1원 미만 버림."""
    return amount * 188 // 1_000_000


def _quantity(cash: int, price: int) -> int:
    """수수료를 포함해 cash로 살 수 있는 최대 정수 주."""
    quantity = cash // price
    while quantity * price + _fee(quantity * price) > cash:
        quantity -= 1
    return quantity


def _tick_up(price: int, date: str) -> int:
    """매수: 바로 위 유효 호가."""
    return price + (1 if date >= TICK_CHANGE and price < 2000 else 5)


def _tick_down(price: int, date: str) -> int:
    """매도: 바로 아래 유효 호가. 2023-12-11부터 2,000원 바로 아래는 1원 단위다."""
    return price - (1 if date >= TICK_CHANGE and price <= 2000 else 5)

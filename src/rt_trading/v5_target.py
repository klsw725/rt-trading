"""v5 목표 보유: 끝 없는 기간, 오늘을 더한 시세, 헤드 모의 계좌 다시 돌리기, 상·하한가(v5 SPEC 3절, PRD 02)."""

import csv
import json
from datetime import date as Date
from datetime import timedelta
from pathlib import Path
from typing import NamedTuple

from rt_trading import engine, market_data, sessions, v3_daily, v4_daily, v4_head, v4_paper
from rt_trading.market_data import Bar, MarketData

PERIOD = Path(__file__).with_name("v5_period.json")
SYMBOLS = ("069500", "114800")


class Target(NamedTuple):
    symbol: str | None  # 목표 종목. 현금이면 None
    reason: str | None  # 목표를 정하지 않은 이유. 정했으면 None
    state: dict | None  # 엔진이 알린 그 봉 뒤의 상태


def load_period(period_path: Path, holidays_path: Path, today: str) -> v3_daily.Period:
    """시작일부터 today(시작일 전이면 시작일)의 다음 거래일까지의 거래일. 기간 마지막 날은 늘 오늘 뒤라 어떤 데이터에도 없다."""
    with holidays_path.open(encoding="utf-8") as file:
        holidays = {row["date"] for row in csv.DictReader(file)}
    try:
        start = json.loads(period_path.read_text(encoding="utf-8"))["start"]
        day = Date.fromisoformat(start)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise v3_daily.DailyError("V5_PERIOD_INVALID", f"{period_path.name}의 시작일을 읽을 수 없다({type(error).__name__})") from error
    if not v3_daily.is_trading_day(start, holidays):
        raise v3_daily.DailyError("V5_PERIOD_INVALID", f"시작일 {start}이 주말이거나 휴장일이다")
    end, days = max(today, start), []
    while not days or days[-1] <= end:
        if v3_daily.is_trading_day(day.isoformat(), holidays):
            days.append(day.isoformat())
        day += timedelta(days=1)
    return v3_daily.Period(start, days, holidays)


def previous_trading_day(day: str, holidays: set[str]) -> str:
    current = Date.fromisoformat(day) - timedelta(days=1)
    while not v3_daily.is_trading_day(current.isoformat(), holidays):
        current -= timedelta(days=1)
    return current.isoformat()


def limits(base: int, day: str) -> tuple[int, int]:
    """KRX 가격 제한으로 계산한 (상한가, 하한가). 폭은 기준가 × 30%를 기준가의 호가 단위로 버리고, 상한가는 아래로·하한가는 위로 그 가격의 호가 단위에 맞춘다."""

    def tick(price: int) -> int:
        return 1 if day >= engine.TICK_CHANGE and price < 2000 else 5

    width = base * 3 // 10 // tick(base) * tick(base)
    upper, lower = base + width, base - width
    return upper - upper % tick(upper), lower + -lower % tick(lower)


def base(paths: v4_daily.Paths, period: v3_daily.Period, today: str) -> MarketData | None:
    """정산한 날까지의 시세. 정산한 날이 없고 오늘이 v5 시작일이면 원본 시세의 오늘 전 날까지다. 어느 쪽도 쓸 수 없으면 None이다."""
    settled = v3_daily.settled_days(paths.paper)
    if settled:
        try:
            return v3_daily.settled_data(paths, settled)
        except v3_daily.DailyError:
            return None
    if today != period.start:
        return None
    data, violations = market_data.load(paths.source)
    return None if violations else data._replace(days=[day for day in data.days if day < today])


def with_today(base_data: MarketData, today: str, bars: dict[str, list[Bar]], until: str | None) -> MarketData:
    """정산한 시세에 오늘을 더한다. 오늘 봉은 until까지이고 장 일정 안이거나 거래량이 있는 봉이다. 기준가와 일봉 값은 전 거래일 종가다."""
    session = sessions.session(today, sessions.exceptions())
    previous = base_data.days[-1]
    with market_data.DEFECTS.open(encoding="utf-8") as file:
        defect = today in {row["date"] for row in csv.DictReader(file)}
    kept = {
        symbol: [bar for bar in bars.get(symbol, []) if (until is None or bar.time <= until) and (bar.time in session.bars or bar.volume)]
        for symbol in SYMBOLS
    }
    return MarketData(
        [*base_data.days, today],
        base_data.sessions | {today: session},
        {symbol: base_data.bars[symbol] | {today: kept[symbol]} for symbol in SYMBOLS},
        {symbol: base_data.base[symbol] | {today: base_data.daily[symbol][previous][0]} for symbol in SYMBOLS},
        [*base_data.defects, today] if defect else list(base_data.defects),
        {symbol: base_data.daily[symbol] | {today: base_data.daily[symbol][previous]} for symbol in SYMBOLS},
    )


def target(
    base_data: MarketData | None,
    period: v3_daily.Period,
    decisions: list[dict],
    today: str,
    kind: str,
    bar_time: str | None,
    bars: dict[str, list[Bar]],
    received: dict[str, tuple[int, int]] | None,
    subordinates: dict = v4_paper.SUBORDINATES,
) -> Target:
    """차례(pre_open·intraday·closing)의 목표 보유. base_data는 오늘 전 거래일까지 정산한 시세(시작일은 원본 시세)이고, received는 받은 오늘 상·하한가다.

    목표를 정하지 않는 이유는 settlement_behind, limits_missing, base_changed, bar_missing 순서로 본다(PRD 02).
    """
    previous = previous_trading_day(today, period.holidays)
    if base_data is None or base_data.days[-1] != previous:
        return Target(None, "settlement_behind", None)
    if not received or any(symbol not in received for symbol in SYMBOLS):
        return Target(None, "limits_missing", None)
    if any(limits(base_data.daily[symbol][previous][0], today) != tuple(received[symbol]) for symbol in SYMBOLS):
        return Target(None, "base_changed", None)
    if kind != "pre_open" and any(not any(bar.time == bar_time for bar in bars.get(symbol, [])) for symbol in SYMBOLS):
        return Target(None, "bar_missing", None)
    data = with_today(base_data, today, bars if kind != "pre_open" else {}, bar_time)
    days = [day for day in period.days if day <= today]
    schedule = v4_head.plan(days, decisions)
    state: dict = {}
    v4_paper.simulate(data, v3_daily.calendar(data.days, period), period.start, period.final_day, schedule, subordinates, state)
    if kind == "closing" and not subordinates[v4_paper.closing(v4_paper.effective(data, period.start, schedule)[today])].overnight:
        return Target(None, None, state)
    if state["buy"]:
        return Target(state["buy"][0], None, state)
    if state["sell_reason"]:
        return Target(None, None, state)
    return Target(state["holding"][0] if state["holding"] else None, None, state)

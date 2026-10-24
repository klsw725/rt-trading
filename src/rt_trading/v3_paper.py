"""v3 부하와 계좌 실행(v3 SPEC 4절, PRD 02). 계좌마다 날마다 정한 부하로 엔진을 돌린다."""

from collections.abc import Callable
from typing import NamedTuple

from rt_trading import engine, strategies, v2_strategies, v3_head
from rt_trading.engine import Order, PreOpen, View
from rt_trading.market_data import MarketData

KODEX200, INVERSE = "069500", "114800"


class Subordinate(NamedTuple):
    make: Callable[[], Callable[[View], Order | None]]  # 전략 객체를 만든다
    overnight: bool  # 며칠 보유 방식인가
    hold: str | None  # 개장 전에 살 종목


def _idle() -> Callable[[View], Order | None]:
    """주문하지 않는 전략."""
    return lambda view: None


_REGISTRY = {
    "cash": Subordinate(_idle, True, None),
    "hold_kodex200": Subordinate(_idle, True, KODEX200),
    "hold_inverse": Subordinate(_idle, True, INVERSE),
    "noise_breakout": Subordinate(strategies.NoiseBreakout, False, None),
    **{name: Subordinate(make, v2_strategies.SETTINGS[name][1], None) for name, make in v2_strategies.CANDIDATES.items()},
}
SUBORDINATES = {name: _REGISTRY[name] for name in v3_head.IDS}  # 부하 설명 표와 같은 순서
ACCOUNTS = ["head", "rule_head", *v3_head.IDS]


def effective(data: MarketData, start: str, plan: dict[str, str]) -> dict[str, str]:
    """날짜 → 그날 실제로 쓰는 부하 e(d). 결함일에는 전 거래일의 e이고, 기간 첫날은 plan(첫날)이다."""
    used, previous = {}, None
    for day in data.days[data.days.index(start) :]:
        used[day] = previous if day in data.defects and previous else plan[day]
        previous = used[day]
    return used


def simulate(
    data: MarketData,
    calendar: list[str],
    start: str,
    final_day: str,
    plan: dict[str, str],
    subordinates: dict[str, Subordinate] = SUBORDINATES,
) -> engine.Records:
    """계좌 하나를 기간 첫날부터 데이터 마지막 날까지 돌린다. plan은 날짜 → 그날 정한 부하 id다."""
    used = effective(data, start, plan)
    days = list(used)
    instances = {name: subordinate.make() for name, subordinate in subordinates.items()}  # 부하마다 전략 객체 하나

    def strategy(view: View) -> Order | None:
        return instances[used[view.date]](view)

    def overnight_on(day: str) -> bool:
        # 기간 마지막 거래일은 v2 SPEC 4.1처럼 새로 사지 않고 남은 보유를 종가 동시호가에 판다
        return day == final_day or subordinates[used[day]].overnight

    def pre_open(view: View) -> PreOpen | None:
        if view.defect:
            return None
        index, hold = days.index(view.date), subordinates[used[view.date]].hold
        if index == 0 or used[view.date] != used[days[index - 1]]:
            return PreOpen(cancel=True, sell=True, buy=hold)
        if hold and not view.holding and not view.pending:
            return PreOpen(buy=hold)
        return None

    return engine.run(
        data, strategy, data.days.index(start), calendar=calendar, final_day=final_day, overnight_on=overnight_on, pre_open=pre_open
    )


def head_plan(decisions: list[dict], days: list[str]) -> dict[str, str]:
    """헤드 계좌: 판단 상태가 ok인 날은 그 부하, 아니면(기록이 없는 날 포함) cash."""
    chosen = {row["date"]: row["subordinate"] for row in decisions if row["status"] == "ok"}
    return {day: chosen.get(day, "cash") for day in days}


def rule_choice(adj_closes: list[int]) -> str:
    """규칙 헤드: v2 SPEC 5.2의 추세(200 × 마지막 값 > 마지막 200개 합)가 위면 hold_kodex200, 아니면 cash."""
    window = adj_closes[-v2_strategies.TREND_DAYS :]
    return "hold_kodex200" if v2_strategies.TREND_DAYS * window[-1] > sum(window) else "cash"


def rule_plan(data: MarketData, days: list[str]) -> dict[str, str]:
    """규칙 헤드 계좌: 거래일 d마다 d의 전 거래일까지의 KODEX 200 수정 종가로 고른다."""
    closes = [data.daily[KODEX200][day][1] for day in data.days]
    return {day: rule_choice(closes[: data.days.index(day)]) for day in days}


def run_accounts(data: MarketData, calendar: list[str], start: str, final_day: str, decisions: list[dict]) -> dict[str, engine.Records]:
    """ACCOUNTS 순서의 계좌마다 기간 첫날부터 데이터 마지막 날까지 돌린 결과."""
    days = data.days[data.days.index(start) :]
    plans = {"head": head_plan(decisions, days), "rule_head": rule_plan(data, days)}
    plans |= {name: dict.fromkeys(days, name) for name in v3_head.IDS}
    return {name: simulate(data, calendar, start, final_day, plans[name]) for name in ACCOUNTS}

"""v4 부하와 계좌 실행(v4 SPEC 4절, PRD 03). 계좌마다 날마다 시작 부하와 장중 교체 목록으로 엔진을 돌린다."""

from rt_trading import engine, v2_strategies, v3_paper, v4_head
from rt_trading.engine import Order, PreOpen, View
from rt_trading.market_data import MarketData

KODEX200 = "069500"
Schedule = dict[str, tuple[str, list[tuple[str, str]]]]  # 날짜 → (시작 부하, [(교체 시각, 부하), …])


class Overnight(v2_strategies.OvernightTrend):
    """종가 베팅(v4 SPEC 4.1). v2 OvernightTrend와 같고, 사는 종목이 언제나 KODEX 200이다."""

    def __call__(self, view: View) -> Order | None:
        order = super().__call__(view)
        return order._replace(symbol=KODEX200) if order and order.kind == "buy" else order


_REGISTRY = v3_paper.SUBORDINATES | {"overnight": v3_paper.Subordinate(Overnight, True, None)}
SUBORDINATES = {name: _REGISTRY[name] for name in v4_head.IDS}  # 부하 설명 표와 같은 순서
ACCOUNTS = ["head", "morning", "rule_head", *v4_head.IDS]


def closing(entry: tuple[str, list[tuple[str, str]]]) -> str:
    """그날 마감 때 쓰는 부하."""
    start, switches = entry
    return switches[-1][1] if switches else start


def effective(data: MarketData, start: str, schedule: Schedule) -> Schedule:
    """날짜 → 그날 실제로 쓰는 (시작 부하, 교체 목록). 결함일에는 전 거래일 마감 때의 부하를 교체 없이 쓰고, 기간 첫날은 schedule의 시작 부하다."""
    used, previous = {}, None
    for day in data.days[data.days.index(start) :]:
        used[day] = (previous or schedule[day][0], []) if day in data.defects else schedule[day]
        previous = closing(used[day])
    return used


def simulate(
    data: MarketData,
    calendar: list[str],
    start: str,
    final_day: str,
    schedule: Schedule,
    subordinates: dict[str, v3_paper.Subordinate] = SUBORDINATES,
) -> engine.Records:
    """계좌 하나를 기간 첫날부터 데이터 마지막 날까지 돌린다. 교체 시각 t의 교체는 t 봉에서 하고 t보다 늦은 봉부터 새 부하를 쓴다."""
    used = effective(data, start, schedule)
    days = list(used)
    instances = {name: subordinate.make() for name, subordinate in subordinates.items()}  # 부하마다 전략 객체 하나

    def strategy(view: View) -> Order | None:
        first, switches = used[view.date]
        return instances[next((name for time, name in reversed(switches) if time < view.time), first)](view)

    def overnight_on(day: str) -> bool:
        # 기간 마지막 거래일은 v3처럼 새로 사지 않고 남은 보유를 종가 동시호가에 판다
        return day == final_day or subordinates[closing(used[day])].overnight

    def pre_open(view: View) -> PreOpen | None:
        if view.defect:
            return None
        index, first = days.index(view.date), used[view.date][0]
        hold = subordinates[first].hold
        if index == 0 or first != closing(used[days[index - 1]]):
            return PreOpen(cancel=True, sell=True, buy=hold)
        if hold and not view.holding and not view.pending:
            return PreOpen(buy=hold)
        return None

    def switch(view: View) -> PreOpen | None:
        name = next((name for time, name in used[view.date][1] if time == view.time), None)
        return PreOpen(cancel=True, sell=True, buy=subordinates[name].hold) if name else None  # 결함일은 effective가 교체를 비운다

    return engine.run(
        data,
        strategy,
        data.days.index(start),
        calendar=calendar,
        final_day=final_day,
        overnight_on=overnight_on,
        pre_open=pre_open,
        switch=switch,
    )


def morning_schedule(decisions: list[dict], days: list[str]) -> Schedule:
    """아침 판단 계좌: 그날 pre_open이 ok면 그 부하, 아니면 전 거래일의 이 계좌 시작 부하로 시작하고(기간 첫날은 cash), 장중 교체는 없다."""
    chosen = {row["date"]: row["subordinate"] for row in decisions if row["kind"] == "pre_open" and row["status"] == "ok"}
    schedule, previous = {}, "cash"
    for day in days:
        previous = chosen.get(day, previous)
        schedule[day] = (previous, [])
    return schedule


def run_accounts(data: MarketData, calendar: list[str], start: str, final_day: str, decisions: list[dict]) -> dict[str, engine.Records]:
    """ACCOUNTS 순서의 계좌마다 기간 첫날부터 데이터 마지막 날까지 돌린 결과. decisions는 (날짜, 차례) 순의 판단 행이다."""
    days = data.days[data.days.index(start) :]
    schedules = {
        "head": v4_head.plan(days, decisions),
        "morning": morning_schedule(decisions, days),
        "rule_head": {day: (name, []) for day, name in v3_paper.rule_plan(data, days).items()},
    }
    schedules |= {name: {day: (name, []) for day in days} for name in v4_head.IDS}
    return {name: simulate(data, calendar, start, final_day, schedules[name]) for name in ACCOUNTS}

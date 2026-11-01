"""v4 실행: 파일 위치와 기간, 정산, 계좌 요약, 당일 1분봉 받기, v4-daily, v4-decide(v4 SPEC 5절, PRD 04)."""

import json
import threading
from collections.abc import Callable
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import httpx

from rt_trading import engine, market_data, toss, v3_daily, v3_head, v4_head, v4_paper
from rt_trading.toss import KST

PERIOD = Path(__file__).with_name("v4_period.json")
SYMBOLS = ("069500", "114800")
FETCH_WINDOW = timedelta(minutes=2)  # 시세와 뉴스는 판단 시각 T + 2분까지만 받는다(SPEC 5.1)
News = Callable[[datetime, datetime, Path], tuple[list[dict] | None, list[str]]]  # (T, 받기 마감, 기록 파일) → (news, 오류)
Bars = Callable[[str, float], dict[str, list[market_data.Bar]] | None]  # (날짜, 받을 수 있는 초) → 종목마다 1분봉


class Paths(NamedTuple):
    source: Path  # v1 fetch가 받는 원본 data/market.sqlite3
    copy: Path  # 정산한 날까지의 시세 사본 data/v4/market.sqlite3
    paper: Path  # 판단과 정산한 날 data/v4/paper.sqlite3
    head: Path  # 헤드 기록 폴더 data/v4/head
    news: Path  # 뉴스 기록 폴더 data/v4/news
    us: Path  # v3-us-fetch가 받는 미국 네 종목 일봉 data/us.sqlite3


def mark_missing(paper: Path, day: str) -> None:
    """정산 3단계: 그날의 판단 차례 중 판단 행이 없는 차례마다 missing 행을 넣는다."""
    done = {row["slot"] for row in v4_head.read(paper) if row["date"] == day}
    for slot in v4_head.slots(day):
        if slot.time not in done:
            v4_head.insert(paper, dict.fromkeys(v4_head.COLUMNS, "") | {"date": day, "slot": slot.time, "kind": slot.kind, "status": "missing", "attempts": 0})


def summary(paths: Paths, period: v3_daily.Period, cal: list[str], day: str, decisions: list[dict]) -> dict:
    """판단일 day의 헤드 입력에 넣을 계좌 요약(PRD 02의 accounts). 정산한 날이 없으면 모든 계좌가 시작 상태다."""
    settled = v3_daily.settled_days(paths.paper)
    if not settled:
        zero = {"return_since_start_pct": 0.0, "max_drawdown_pct": 0.0}
        return {
            "subordinates": {name: {"recent_returns_pct": {"1d": 0.0, "5d": 0.0, "20d": 0.0}, "holding": None} for name in v4_head.IDS},
            "head_account": {"subordinate": None, "holding": None, **zero, "limits": {"weekly": False, "cumulative": False}},
            "rule_head": dict(zero),
            "morning_account": dict(zero),
            "head_returns": {},
            "settled_through": None,
        }
    data = v3_daily.settled_data(paths, settled)
    records = v4_paper.run_accounts(data, cal, period.start, period.final_day, decisions)
    last, week = settled[-1], Date.fromisoformat(day).isocalendar()[:2]

    def closes(name: str) -> list[int]:
        return [engine.INITIAL_CASH] + [records[name].closes[date] for date in settled]  # C_0 … C_n

    def change(name: str, back: int) -> float:
        values = closes(name)
        return v3_head.pct(Decimal(values[-1]) / Decimal(values[max(0, len(values) - 1 - back)]) - 1)

    def holding(name: str) -> str | None:
        held = None
        for fill in records[name].fills:
            held = fill.symbol if fill.side == "buy" else None
        return held

    def drawdown(name: str) -> float:
        peak = worst = Decimal(0)
        for _, equity in records[name].equity:  # 첫 값은 시작 자금 C_0
            peak = max(peak, Decimal(equity))
            worst = max(worst, 1 - Decimal(equity) / peak)
        return v3_head.pct(worst)

    def account(name: str) -> dict:
        return {"return_since_start_pct": change(name, len(settled)), "max_drawdown_pct": drawdown(name)}

    limits = records["head"].limits
    used = v4_paper.effective(data, period.start, v4_head.plan(data.days[data.days.index(period.start) :], decisions))
    head = closes("head")
    return {
        "subordinates": {
            name: {"recent_returns_pct": {f"{k}d": change(name, k) for k in (1, 5, 20)}, "holding": holding(name)} for name in v4_head.IDS
        },
        "head_account": {
            "subordinate": v4_paper.closing(used[last]),
            "holding": holding("head"),
            **account("head"),
            "limits": {
                "weekly": any(kind == "weekly" and Date.fromisoformat(time[:10]).isocalendar()[:2] == week for time, kind in limits),
                "cumulative": any(kind == "cumulative" for _, kind in limits),
            },
        },
        "rule_head": account("rule_head"),
        "morning_account": account("morning"),
        "head_returns": {date: v3_head.pct(Decimal(head[i + 1]) / Decimal(head[i]) - 1) for i, date in enumerate(settled)},
        "settled_through": last,
    }


def fetch_bars(root: Path, day: str, seconds: float) -> dict[str, list[market_data.Bar]] | None:
    """토스에서 두 종목의 day 1분봉(원가격)을 seconds초 안에 받는다. 끝나지 않으면 V4_BARS_TIMEOUT, 토스 오류면 v1 코드를 출력하고 None이다.

    받기는 데몬 스레드에서 하고 시간이 지나면 기다리지 않는다. 원본 시세 파일에는 저장하지 않는다.
    """
    result: dict = {}

    def work() -> None:
        try:
            with httpx.Client(base_url=toss.BASE_URL, timeout=30) as http:
                token = toss.issue_token(http, *toss.read_credentials(root))
                tomorrow = (Date.fromisoformat(day) + timedelta(days=1)).isoformat()
                bars = {}
                for symbol in SYMBOLS:
                    rows = toss.minute_rows(symbol, toss.candles(http, token, symbol, "1m", False, day), day, tomorrow)
                    bars[symbol] = [market_data.Bar(time[11:], *values) for _, time, *values in rows]
                result["bars"] = bars
        except toss.TossError as error:
            result["error"] = str(error)
        except KeyError as error:
            result["error"] = f"V1_TOSS_CREDENTIALS_MISSING: .env나 환경 변수에 {error}가 없다"

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        print(f"V4_BARS_TIMEOUT: {seconds:.0f}초 안에 당일 1분봉을 다 받지 못해 멈췄다")
        return None
    if "error" in result:
        print(result["error"])
    return result.get("bars")


def run(
    paths: Paths,
    period_path: Path,
    holidays_path: Path,
    fetch: Callable[[float], int],
    news: News,
    http: httpx.Client,
    credentials: Callable[[], tuple[str, str]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    """v4-daily: v3-daily의 순서로 시세 받기·정산·개장 전 판단을 한다. 하나라도 실패하면 1이다. fetch는 시세 받기에 쓸 수 있는 초를 받는다."""
    try:
        period = v3_daily.load_period(period_path, holidays_path, "V4_PERIOD_INVALID")
    except v3_daily.DailyError as error:
        print(error)
        return 1
    now = clock()
    today = now.date().isoformat()
    if not v3_daily.is_trading_day(today, period.holidays):
        print("휴장일")
        return 0
    failures = []
    first = v4_head.slots(today)[0]
    deadline = _at(today, first.deadline)
    left = (deadline - v3_daily.FETCH_RESERVE - now).total_seconds()
    if left <= 0:
        print("V3_FETCH_SKIPPED: 판단 마감 15분 전이 지나 시세를 받지 않았다")
        failures.append("V3_FETCH_FAILED")
    elif fetch(left) != 0:
        failures.append("V3_FETCH_FAILED")
    before = v3_daily.settled_days(paths.paper)
    problems = v3_daily.settle(paths, period, today, now, mark_missing)
    if problems:
        print("\n".join(problems))
        failures.append("V3_SETTLEMENT_STOPPED")
    settled = v3_daily.settled_days(paths.paper)
    source_days = v3_daily.data_days(paths.source)
    lines = [f"시세: {source_days[0]} ~ {source_days[-1]} {len(source_days)}거래일" if source_days else "시세: 없음"]
    lines.append(f"정산: {', '.join(day for day in settled if day not in before) or '없음'} (마친 마지막 날 {settled[-1] if settled else '없음'})")
    if today < period.start:
        lines.append("기간 전")
    elif today in period.days:
        decisions = v4_head.read(paths.paper)
        row = next((row for row in decisions if (row["date"], row["slot"]) == (today, first.time)), None)
        if row is None and clock() < deadline:
            cutoff = clock()  # 판단 시각 T는 입력을 만드는 시각이다
            found, errors = news(cutoff, min(cutoff + FETCH_WINDOW, deadline), paths.news / today / f"{first.time.replace(':', '')}.json")
            lines.append(f"뉴스: {'; '.join(errors) or '정상'}")
            cal = v3_daily.calendar(source_days, period)
            accounts = summary(paths, period, cal, today, decisions)
            month = [day for day in cal if day[:7] == today[:7]]
            content = v4_head.build_input(today, first.time, period.days, month, paths.source, paths.us, accounts, decisions, None, found)
            row = v4_head.decide(paths.paper, paths.head, today, first.time, content, http, credentials, clock, sleep)
        lines.append(f"판단: {today} {first.time} 마감이 지나 하지 않았다" if row is None else _decision_line(row))
        if row is None or row["status"] != "ok":
            failures.append("V3_DECISION_FAILED")
    if period.final_day in settled:
        lines.append("기간 종료")
    if failures:
        lines.append(f"실패: {', '.join(failures)}")
    print("\n".join(lines))
    return 1 if failures else 0


def decide(
    paths: Paths,
    period_path: Path,
    holidays_path: Path,
    slot: str,
    bars: Bars,
    news: News,
    http: httpx.Client,
    credentials: Callable[[], tuple[str, str]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    """v4-decide --at slot: 장중·마감 전 판단 한 번. 판단이 ok나 capped면 0이다. 시세 사본과 정산은 바꾸지 않는다."""
    try:
        period = v3_daily.load_period(period_path, holidays_path, "V4_PERIOD_INVALID")
    except v3_daily.DailyError as error:
        print(error)
        return 1
    today = clock().date().isoformat()
    if not v3_daily.is_trading_day(today, period.holidays):
        print("휴장일")
        return 0
    if today < period.start or today > period.final_day:
        print("기간 전" if today < period.start else "기간 끝")
        return 0
    if slot not in [item.time for item in v4_head.slots(today) if item.kind != "pre_open"]:
        print(f"V4_SLOT_INVALID: {slot}은 오늘의 장중·마감 전 판단 차례가 아니다")
        return 1
    decisions = v4_head.read(paths.paper)
    row = next((row for row in decisions if (row["date"], row["slot"]) == (today, slot)), None)
    if row:
        print(_decision_line(row))
        return 0 if row["status"] in ("ok", "capped") else 1
    deadline = _at(today, next(item.deadline for item in v4_head.slots(today) if item.time == slot))
    if clock() >= deadline:
        print(f"V4_DEADLINE_PASSED: {slot} 차례의 마감 {deadline.strftime('%H:%M')}이 지나 판단하지 않았다")
        return 1
    limit, missed = _at(today, slot) + FETCH_WINDOW, []
    left = (limit - clock()).total_seconds()
    received = bars(today, left) if left > 0 else None
    if received is None:
        missed.append("당일 1분봉")
    else:
        folder = paths.head / today / slot.replace(":", "")
        folder.mkdir(parents=True, exist_ok=True)
        saved = {symbol: [list(bar) for bar in found] for symbol, found in received.items()}
        (folder / "bars.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    found, errors = news(_at(today, slot), limit, paths.news / today / f"{slot.replace(':', '')}.json")
    if errors:
        missed.append(f"뉴스({'; '.join(errors)})")
    cal = v3_daily.calendar(v3_daily.data_days(paths.source), period)
    accounts = summary(paths, period, cal, today, decisions)
    month = [day for day in cal if day[:7] == today[:7]]
    content = v4_head.build_input(today, slot, period.days, month, paths.source, paths.us, accounts, decisions, received, found)
    row = v4_head.decide(paths.paper, paths.head, today, slot, content, http, credentials, clock, sleep)
    lines = [f"판단: {today} {slot} 마감이 지나 하지 않았다" if row is None else _decision_line(row)]
    plan = v4_head.plan([day for day in period.days if day <= today], v4_head.read(paths.paper))[today]
    lines.append(f"지금 부하: {v4_paper.closing(plan)}, 남은 교체 {v4_head.SWITCH_LIMIT - len(plan[1])}번")
    if missed:
        lines.append(f"받기 실패: {', '.join(missed)}")
    print("\n".join(lines))
    return 0 if row and row["status"] in ("ok", "capped") else 1


def _decision_line(row: dict) -> str:
    return "판단: " + " ".join(part for part in (row["date"], row["slot"], row["status"], row["subordinate"], row["error"]) if part)


def _at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=KST)

"""v3 매일 실행: 달력과 기간, 시세 사본, 정산, 계좌 요약, v3-daily(v3 SPEC 5절, PRD 03)."""

import csv
import json
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import httpx

from rt_trading import engine, market_data, store, v3_head, v3_paper

PERIOD = Path(__file__).with_name("v3_period.json")
HOLIDAYS = Path(__file__).with_name("krx_holidays.csv")
PERIOD_DAYS = 5
FETCH_RESERVE = timedelta(minutes=15)  # 시세 받기는 판단 마감 15분 전까지만 한다(SPEC 5.1)
KODEX200 = "069500"
SETTLED = "date TEXT NOT NULL, settled_at TEXT NOT NULL, PRIMARY KEY (date)"


class Paths(NamedTuple):
    source: Path  # v1 fetch가 받는 원본 data/market.sqlite3
    copy: Path  # 정산한 날까지의 시세 사본 data/v3/market.sqlite3
    paper: Path  # 판단과 정산한 날 data/v3/paper.sqlite3
    head: Path  # 헤드 기록 폴더 data/v3/head


class Period(NamedTuple):
    start: str
    days: list[str]  # 기간의 5거래일
    holidays: set[str]

    @property
    def final_day(self) -> str:
        return self.days[-1]


class DailyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def is_trading_day(day: str, holidays: set[str]) -> bool:
    return Date.fromisoformat(day).weekday() < 5 and day not in holidays


def load_period(period_path: Path, holidays_path: Path) -> Period:
    """기간 시작일과 휴장일 표를 읽어 기간의 5거래일을 정한다."""
    with holidays_path.open(encoding="utf-8") as file:
        holidays = {row["date"] for row in csv.DictReader(file)}
    try:
        start = json.loads(period_path.read_text(encoding="utf-8"))["start"]
        day = Date.fromisoformat(start)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise DailyError("V3_PERIOD_INVALID", f"{period_path.name}의 시작일을 읽을 수 없다({type(error).__name__})") from error
    if not is_trading_day(start, holidays):
        raise DailyError("V3_PERIOD_INVALID", f"시작일 {start}이 주말이거나 휴장일이다")
    days = []
    while len(days) < PERIOD_DAYS:
        if is_trading_day(day.isoformat(), holidays):
            days.append(day.isoformat())
        day += timedelta(days=1)
    return Period(start, days, holidays)


def calendar(data_days: list[str], period: Period) -> list[str]:
    """거래일 달력. 시작일 전은 시세의 거래일이고, 시작일부터 기간 마지막 날의 다음 달 말까지는 주말과 휴장일을 뺀 날이다."""
    final = Date.fromisoformat(period.final_day)
    year, month = (final.year + 1, final.month - 10) if final.month > 10 else (final.year, final.month + 2)
    end = Date(year, month, 1) - timedelta(days=1)
    days, day = [day for day in data_days if day < period.start], Date.fromisoformat(period.start)
    while day <= end:
        if is_trading_day(day.isoformat(), period.holidays):
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def settled_days(paper: Path) -> list[str]:
    if not paper.exists():
        return []
    with closing(sqlite3.connect(paper)) as db, db:
        db.execute(f"CREATE TABLE IF NOT EXISTS settled_days ({SETTLED})")
        return [row[0] for row in db.execute("SELECT date FROM settled_days ORDER BY date")]


def settle(paths: Paths, period: Period, today: str, now: datetime) -> list[str]:
    """기간의 거래일 중 today 전이고 정산하지 않은 날을 오래된 날부터 정산한다. 출력할 문제 줄을 돌려준다."""
    done = set(settled_days(paths.paper))
    pending = [day for day in period.days if day < today and day not in done]
    if not pending:
        return []
    problems, limit = [], None
    _, violations = market_data.load(paths.source)
    if violations:
        problems.append(market_data.describe(violations))
        if not all(violation.date for violation in violations):
            return problems
        limit = min(violation.date for violation in violations)
    source_days = _data_days(paths.source)
    after = [day for day in source_days if day >= period.start]
    expected = [day for day in calendar(source_days, period) if period.start <= day <= (after[-1] if after else "")]
    if after != expected:
        return problems + [f"V3_CALENDAR_MISMATCH 원본 거래일과 휴장일 표가 다른 날: {', '.join(sorted(set(after) ^ set(expected)))}"]
    for day in pending:
        if limit and day >= limit:
            break
        if day not in source_days:
            problems.append(f"V3_DATA_NOT_READY {day} 원본에 이날 시세가 아직 없다")
            break
        violations = _extend(paths.source, paths.copy, day, period.start)
        if violations:
            problems.append(market_data.describe(violations))
            break
        if not any(row["date"] == day for row in v3_head.read(paths.paper)):
            v3_head.insert(paths.paper, dict.fromkeys(v3_head.COLUMNS, "") | {"date": day, "status": "missing", "subordinate": "cash", "attempts": 0})
        with closing(sqlite3.connect(paths.paper)) as db, db:
            db.execute(f"CREATE TABLE IF NOT EXISTS settled_days ({SETTLED})")
            db.execute("INSERT INTO settled_days VALUES (?, ?)", (day, now.isoformat(timespec="seconds")))
    return problems


def summary(paths: Paths, period: Period, cal: list[str], day: str, decisions: list[dict]) -> dict:
    """판단일 day의 헤드 입력에 넣을 계좌 요약(PRD 01의 accounts). 정산한 날이 없으면 모든 계좌가 시작 상태다."""
    settled = settled_days(paths.paper)
    if not settled:
        zero = {"1d": 0.0, "5d": 0.0, "20d": 0.0}
        return {
            "subordinates": {name: {"recent_returns_pct": dict(zero), "holding": None} for name in v3_head.IDS},
            "head_account": {
                "subordinate": None,
                "holding": None,
                "return_since_start_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "limits": {"weekly": False, "cumulative": False},
            },
            "rule_head": {"return_since_start_pct": 0.0, "max_drawdown_pct": 0.0},
            "head_returns": {},
            "settled_through": None,
        }
    data = settled_data(paths, settled)
    records = v3_paper.run_accounts(data, cal, period.start, period.final_day, decisions)
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

    limits = records["head"].limits
    used = v3_paper.effective(data, period.start, v3_paper.head_plan(decisions, data.days[data.days.index(period.start) :]))
    head = closes("head")
    return {
        "subordinates": {
            name: {"recent_returns_pct": {f"{k}d": change(name, k) for k in (1, 5, 20)}, "holding": holding(name)} for name in v3_head.IDS
        },
        "head_account": {
            "subordinate": used[last],
            "holding": holding("head"),
            "return_since_start_pct": change("head", len(settled)),
            "max_drawdown_pct": drawdown("head"),
            "limits": {
                "weekly": any(kind == "weekly" and Date.fromisoformat(time[:10]).isocalendar()[:2] == week for time, kind in limits),
                "cumulative": any(kind == "cumulative" for _, kind in limits),
            },
        },
        "rule_head": {"return_since_start_pct": change("rule_head", len(settled)), "max_drawdown_pct": drawdown("rule_head")},
        "head_returns": {date: v3_head.pct(Decimal(head[i + 1]) / Decimal(head[i]) - 1) for i, date in enumerate(settled)},
        "settled_through": last,
    }


def run(
    paths: Paths,
    period_path: Path,
    holidays_path: Path,
    fetch: Callable[[float], int],
    http: httpx.Client,
    credentials: Callable[[], tuple[str, str]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    """SPEC 5.1의 매일 실행. 시세 받기·정산·판단 중 하나라도 실패하면 1이다. fetch는 시세 받기에 쓸 수 있는 초를 받는다."""
    try:
        period = load_period(period_path, holidays_path)
    except DailyError as error:
        print(error)
        return 1
    now = clock()
    today = now.date().isoformat()
    if not is_trading_day(today, period.holidays):
        print("휴장일")
        return 0
    failures = []
    left = (v3_head.decision_deadline(today) - FETCH_RESERVE - now).total_seconds()
    if left <= 0:
        print("V3_FETCH_SKIPPED: 판단 마감 15분 전이 지나 시세를 받지 않았다")
        failures.append("V3_FETCH_FAILED")
    elif fetch(left) != 0:
        failures.append("V3_FETCH_FAILED")
    before = settled_days(paths.paper)
    problems = settle(paths, period, today, now)
    if problems:
        print("\n".join(problems))
        failures.append("V3_SETTLEMENT_STOPPED")
    settled = settled_days(paths.paper)
    source_days = _data_days(paths.source)
    lines = [f"시세: {source_days[0]} ~ {source_days[-1]} {len(source_days)}거래일" if source_days else "시세: 없음"]
    lines.append(f"정산: {', '.join(day for day in settled if day not in before) or '없음'} (마친 마지막 날 {settled[-1] if settled else '없음'})")
    if today < period.start:
        lines.append("기간 전")
    elif today in period.days:
        decisions = v3_head.read(paths.paper)
        row = next((row for row in decisions if row["date"] == today), None)
        if row is None:
            cal = calendar(source_days, period)
            accounts = summary(paths, period, cal, today, decisions)
            content = v3_head.build_input(today, [day for day in cal if day[:7] == today[:7]], paths.source, accounts, decisions)
            row = v3_head.decide(paths.paper, paths.head, today, content, http, credentials, clock, sleep)
        if row is None:
            lines.append(f"판단: {today} 마감이 지나 하지 않았다")
        else:
            lines.append(f"판단: {today} {row['status']} {row['subordinate']}" + (f" {row['error']}" if row["error"] else ""))
        if row is None or row["status"] != "ok":
            failures.append("V3_DECISION_FAILED")
    if period.final_day in settled:
        lines.append("기간 종료")
    if failures:
        lines.append(f"실패: {', '.join(failures)}")
    print("\n".join(lines))
    return 1 if failures else 0


def settled_data(paths: Paths, settled: list[str]) -> market_data.MarketData:
    """시세 사본을 정산한 마지막 날까지로 잘라 읽는다. 사본에는 검증을 통과한 날만 넣으므로 어긴 것이 있으면 V3_COPY_INVALID다."""
    data, violations = market_data.load(paths.copy)
    if violations:
        raise DailyError("V3_COPY_INVALID", market_data.describe(violations))
    return data._replace(days=[day for day in data.days if day <= settled[-1]])


def _data_days(path: Path) -> list[str]:
    """시세 DB의 KODEX 200 거래일. 파일이 없으면 빈 목록이다."""
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute("SELECT date FROM candles_1d WHERE symbol = ? ORDER BY date", (KODEX200,))]


def _extend(source: Path, copy: Path, day: str, start: str) -> list[market_data.Violation]:
    """사본을 임시 파일로 복사해 day의 시세를 넣고 검증한다. 통과하면 사본을 바꾸고 빈 목록, 아니면 어긴 것을 돌려준다."""
    temp = copy.with_name(copy.name + ".tmp")
    copy.parent.mkdir(parents=True, exist_ok=True)
    if copy.exists():
        shutil.copyfile(copy, temp)
    else:
        temp.unlink(missing_ok=True)
    with closing(sqlite3.connect(temp)) as db:
        db.execute("ATTACH DATABASE ? AS source", (str(source),))
        with db:
            for table, columns in store.TABLES.items():
                db.execute(f"CREATE TABLE IF NOT EXISTS main.{table} ({columns})")
            db.execute(f"PRAGMA main.user_version = {store.SCHEMA_VERSION}")
            if not db.execute("SELECT 1 FROM main.candles_1d LIMIT 1").fetchone():  # 새 사본이면 시작일 전 행을 먼저 넣는다
                db.execute("INSERT OR IGNORE INTO main.candles_1m SELECT * FROM source.candles_1m WHERE substr(time, 1, 10) < ?", (start,))
                db.execute("INSERT OR IGNORE INTO main.candles_1d SELECT * FROM source.candles_1d WHERE date < ?", (start,))
            db.execute("INSERT OR IGNORE INTO main.candles_1m SELECT * FROM source.candles_1m WHERE substr(time, 1, 10) = ?", (day,))
            db.execute("INSERT OR IGNORE INTO main.candles_1d SELECT * FROM source.candles_1d WHERE date = ?", (day,))
            db.execute(
                "INSERT OR IGNORE INTO main.fetch_runs SELECT * FROM source.fetch_runs"
                " WHERE fetched_at = (SELECT MAX(fetched_at) FROM source.fetch_runs)"
            )
    _, violations = market_data.load(temp)
    if violations:
        temp.unlink()
        return violations
    temp.replace(copy)
    return []

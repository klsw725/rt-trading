"""v6 실행과 보고(v6 SPEC 5~7절, PRD 04): 설정과 검증 구간, 장부 잠금, v6-decide, v6-watch, v6-report, 판정과 전략별 성적.

토스·네이버·Codex 호출은 장부 잠금 밖에서 하고, 받은 값은 잠금 안의 트랜잭션 하나로 쓴다.
"""

import csv
import fcntl
import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing, contextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import httpx

from rt_trading import toss, v3_head, v4_news, v6_head, v6_market, v6_paper
from rt_trading.toss import KST

FILL_WAIT = timedelta(seconds=40)  # 체결에 쓸 호가는 체결 시각 + 40초까지 받는다
WATCH_FETCH = timedelta(seconds=30)  # 10분 평가는 시작 + 30초까지 받는다
CALENDAR_FETCH = timedelta(minutes=1)
SLOT = timedelta(minutes=10)
STATUSES = ("ok", "failed", "invalid", "late", "missing")
DECISION_COLUMNS = ("time", "status", "positions", "strategy", "reason", "model", "reported_model", "attempts", "error")
EQUITY_COLUMNS = ("slot", "at", "equity", "krw", "usd", "rate", "stale")


class Paths(NamedTuple):
    root: Path  # data/v6
    reports: Path  # data/reports

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def paper(self) -> Path:
        return self.root / "paper.sqlite3"

    @property
    def lock(self) -> Path:
        return self.root / "paper.lock"

    @property
    def decide_lock(self) -> Path:
        return self.root / "decide.lock"

    @property
    def calendar(self) -> Path:
        return self.root / "calendar.json"

    @property
    def head(self) -> Path:
        return self.root / "head"

    @property
    def news(self) -> Path:
        return self.root / "news"


@contextmanager
def ledger(paths: Paths):
    """paper.lock을 잡고 SQLite 연결 하나의 트랜잭션을 연다. 블록이 오류 없이 끝나면 커밋한다."""
    paths.root.mkdir(parents=True, exist_ok=True)
    with open(paths.lock, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with closing(sqlite3.connect(paths.paper)) as conn, conn:
                for name, columns in v6_paper.TABLES.items():
                    conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({columns})")
                conn.execute(f"CREATE TABLE IF NOT EXISTS decisions ({v6_head.TABLE})")
                conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                yield conn
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def reading(paths: Paths):
    """paper.lock을 공유로 잡고 읽기 전용 SQLite 연결을 준다. 잡은 동안 읽은 장부와 장 운영 정보는 한 시점의 것이다. 파일을 만들거나 바꾸지 않는다."""
    with open(paths.lock) as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        try:
            with closing(sqlite3.connect(f"file:{paths.paper}?mode=ro", uri=True)) as conn:
                yield conn
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# 설정과 검증 구간


def prepare(paths: Paths, http: httpx.Client, token: toss.SharedToken, clock: Callable[[], datetime], events: list[dict]) -> tuple[str, datetime, dict]:
    """설정의 시작일, 끝 시각, 장 운영 정보. 처음이면 끝 시각을 세고 장부를 만든다. 설정이 틀리면 V6_CONFIG_INVALID."""
    start = _config(paths)
    calendar = v6_market.load_calendar(paths.calendar)
    fill = _filler(paths, http, token, clock, events, calendar)
    for market, key in (("KR", start), ("US", start)):
        if key not in calendar.get(market, {}):
            fill(start, market)
    if not v6_market.trading_day(calendar, start) or not calendar["US"].get(start):
        raise v6_market.V6Error("V6_CONFIG_INVALID", f"시작일 {start}은 국내·미국 세션이 모두 있는 국내 거래일이 아니다")
    with ledger(paths) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    if meta:
        if meta["start"] != start:
            raise v6_market.V6Error("V6_CONFIG_INVALID", f"장부의 시작일 {meta['start']}과 설정의 시작일 {start}이 다르다. 새로 하려면 data/v6의 장부를 옮긴다")
        return start, datetime.fromisoformat(meta["end"]), calendar
    end = v6_market.end_time(calendar, start, lambda day: fill(day, "KR"))
    with ledger(paths) as conn:
        if not conn.execute("SELECT 1 FROM meta").fetchone():
            v6_paper.start(conn, start)
            conn.executemany("INSERT INTO meta VALUES (?, ?)", [("start", start), ("end", end.isoformat())])
    return start, end, calendar


def _config(paths: Paths) -> str:
    try:
        start = json.loads(paths.config.read_text(encoding="utf-8"))["start"]
        Date.fromisoformat(start)
        return start
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise v6_market.V6Error("V6_CONFIG_INVALID", f"{paths.config}에 시작일(start, YYYY-MM-DD)이 없다({error!r})") from error


def _filler(paths: Paths, http: httpx.Client, token: toss.SharedToken, clock: Callable[[], datetime], events: list[dict], calendar: dict):
    """그날의 장 운영 정보를 잠금 밖에서 받아 잠금 안에서 파일과 메모리의 장 운영 정보에 합치는 함수."""

    def fill(day: str, market: str) -> None:
        fetched = v6_market.fetch_calendar(http, token, market, day, clock() + CALENDAR_FETCH, clock, events)
        with ledger(paths):
            saved = v6_market.load_calendar(paths.calendar)
            v6_market.save_calendar(paths.calendar, v6_market.merge_calendar(saved, market, fetched))
        v6_market.merge_calendar(calendar, market, fetched)

    return fill


def refresh_calendar(paths: Paths, http: httpx.Client, token: toss.SharedToken, clock: Callable[[], datetime]) -> None:
    """v6-serve가 켜질 때 국내·미국 장 운영 정보를 오늘 날짜로 한 번씩 받아 합친다. 받지 못한 것은 기록만 한다."""
    events: list[dict] = []
    fill = _filler(paths, http, token, clock, events, v6_market.load_calendar(paths.calendar))
    today = clock().astimezone(KST).date().isoformat()
    try:
        for market in ("KR", "US"):
            try:
                fill(today, market)
            except v6_market.V6Error as error:
                print(error)
    finally:
        _save_events(paths, events)


def _today_calendar(paths: Paths, http: httpx.Client, token: toss.SharedToken, clock: Callable[[], datetime], events: list[dict], calendar: dict) -> None:
    """그날 장 운영 정보를 아직 받지 않았으면 국내·미국을 오늘 날짜로 한 번씩 받는다. 받지 못하면 저장한 값을 쓴다."""
    today = clock().astimezone(KST).date().isoformat()
    with ledger(paths) as conn:
        done = conn.execute("SELECT value FROM meta WHERE key = 'calendar_day'").fetchone()
    if done and done[0] == today:
        return
    fill = _filler(paths, http, token, clock, events, calendar)
    for market in ("KR", "US"):
        try:
            fill(today, market)
        except v6_market.V6Error as error:
            print(error)
    with ledger(paths) as conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('calendar_day', ?)", (today,))


# v6-watch


def watch(paths: Paths, http: httpx.Client, token: toss.SharedToken, clock: Callable[[], datetime], sleep: Callable[[float], None]) -> int:
    """시작한 시각이 든 10분 차례의 평가, 기준, 손실 한도, 강제 매도(SPEC 4.3·4.4)."""
    started, events = clock(), []
    try:
        start, end, calendar = prepare(paths, http, token, clock, events)
        if not _midnight(start) <= _floor(started) <= end:
            return 0
        _today_calendar(paths, http, token, clock, events, calendar)
        _watch(paths, http, token, clock, sleep, events, calendar, _midnight(start), end, started)
        return 0
    except v6_market.V6Error as error:
        print(error)
        return 1
    finally:
        _save_events(paths, events)


def _watch(
    paths: Paths,
    http: httpx.Client,
    token: toss.SharedToken,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    events: list[dict],
    calendar: dict,
    begin: datetime,
    end: datetime,
    started: datetime,
) -> None:
    slot_time = _floor(started)
    slot = _name(slot_time)
    if slot_time < begin or slot_time > end:
        return
    with ledger(paths) as conn:
        if _valued(conn, slot):
            return
        ledger_state = v6_paper.state(conn)
    holdings = sorted(ledger_state["positions"])
    needs_rate = bool(ledger_state["usd"]) or any(position["market"] == "US" for position in ledger_state["positions"].values())
    deadline = slot_time + WATCH_FETCH  # 늦게 시작해도 다음 차례의 값을 이 차례에 쓰지 않는다
    received = {"prices": _quiet(lambda: v6_paper.observe_prices(http, token, holdings, deadline, clock, events), {})}
    if needs_rate:
        received["rate"] = _quiet(lambda: v6_paper.observe_rate(http, token, deadline, clock, events), None)
    with ledger(paths) as conn:
        if _valued(conn, slot):
            return
        v6_paper.remember(conn, received)
        total = v6_paper.value_slot(conn, slot, clock(), received, calendar)
        if slot_time == end:
            return
        v6_paper.bases(conn, clock())
        v6_paper.check(conn, total, clock())
        forced = "forced_sale" in v6_paper.blocked_reasons(conn, clock())
        ledger_state, infos = v6_paper.state(conn), v6_paper.instruments(conn)
    if not forced:
        return
    open_at = v6_market.sessions(calendar, slot_time) or {"KR": None, "US": None}
    symbols = [symbol for symbol in ledger_state["positions"] if symbol in infos and v6_market.tradable(infos[symbol], open_at)]
    books = v6_paper.observe_books(http, token, symbols, slot_time, slot_time + FILL_WAIT, clock, sleep, events)
    with ledger(paths) as conn:
        v6_paper.force(conn, slot, slot_time, open_at, books)


# v6-decide


def decide(
    paths: Paths,
    clock_text: str,
    http: httpx.Client,
    token: toss.SharedToken,
    naver: tuple[str, str] | None,
    codex_credentials: Callable[[], tuple[str, str]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    """판단 차례 하나(SPEC 3·4.1·5.1): C까지 받아 판단하고 E 뒤에 체결한다. 같은 차례를 다시 실행해도 판단과 체결이 다시 생기지 않는다."""
    if not re.fullmatch(r"([01]\d|2[0-3]):40", clock_text):
        print(f"V6_SLOT_INVALID: 판단 차례는 매시 40분(HH:40)이다({clock_text})")
        return 1
    paths.root.mkdir(parents=True, exist_ok=True)
    with open(paths.decide_lock, "a") as lock:  # v6-decide는 한 번에 하나만 돈다. 같은 차례를 함께 실행해도 늦은 쪽이 앞의 판단을 덮지 않는다
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _decide(paths, clock_text, http, token, naver, codex_credentials, clock, sleep)


def _decide(
    paths: Paths,
    clock_text: str,
    http: httpx.Client,
    token: toss.SharedToken,
    naver: tuple[str, str] | None,
    codex_credentials: Callable[[], tuple[str, str]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    events = []
    try:
        start, end, calendar = prepare(paths, http, token, clock, events)
        day = clock().astimezone(KST).date().isoformat()
        slot = f"{day} {clock_text}"
        decided, cutoff, _ = v6_head.times(slot)
        if decided < _midnight(start) or decided > end:
            return 0
        kind = dict(v6_market.slots(calendar, day)).get(clock_text)
        if kind is None:
            return 0
        with ledger(paths) as conn:
            row = _decision(conn, slot)
        if row is None and (kind == "missing" or clock() >= cutoff):
            _record_missing(paths, slot, "V6_CALENDAR_MISSING" if kind == "missing" else "V6_STARTED_LATE", clock)
            return 0
        if row is None:
            row = _judge(paths, http, token, naver, codex_credentials, clock, sleep, events, calendar, slot, _midnight(start), end)
        targets = v6_head.targets(row)
        if targets is not None:
            _fill(paths, http, token, clock, sleep, events, calendar, slot, targets)
        return 0
    except v6_market.V6Error as error:
        print(error)
        return 1
    finally:
        _save_events(paths, events)


def _judge(
    paths: Paths,
    http: httpx.Client,
    token: toss.SharedToken,
    naver: tuple[str, str] | None,
    codex_credentials: Callable[[], tuple[str, str]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    events: list[dict],
    calendar: dict,
    slot: str,
    begin: datetime,
    end: datetime,
) -> dict:
    """C까지 받고 C 평가를 쓴 뒤 판단해 기록한 행. 그 차례 10분 평가가 없으면 먼저 한다(평가와 한도가 C 평가보다 먼저다)."""
    decided, cutoff, fill_at = v6_head.times(slot)
    with ledger(paths) as conn:
        valued = _valued(conn, _name(decided))
    if not valued:
        _watch(paths, http, token, clock, sleep, events, calendar, begin, end, clock())
    with ledger(paths) as conn:
        ledger_state, decisions = v6_paper.state(conn), v6_head.read(conn)
    holdings = {symbol: position["quantity"] for symbol, position in ledger_state["positions"].items()}
    open_at = v6_market.sessions(calendar, fill_at) or {"KR": None, "US": None}
    ends = v6_market.session_ends(calendar, fill_at) or {"KR": None, "US": None}
    prices = _quiet(lambda: v6_paper.observe_prices(http, token, sorted(holdings), cutoff, clock, events), {})
    rate = _quiet(lambda: v6_paper.observe_rate(http, token, cutoff, clock, events), None)
    rankings = {}
    for market in ("KR", "US"):
        if open_at[market] and (got := _quiet(lambda: v6_market.fetch_rankings(http, token, market, cutoff, clock, events), None)):
            rankings[market] = got
    symbols = list(dict.fromkeys([item["symbol"] for items, _ in rankings.values() for item in items] + sorted(holdings)))
    infos = _quiet(lambda: v6_market.fetch_instruments(http, token, symbols, cutoff, clock, events), None)
    news_path = paths.news / slot[:10] / f"{slot[11:].replace(':', '')}.json"
    news, _ = v4_news.fetch(http, naver, cutoff, cutoff, clock, news_path)
    news_ready = clock()
    infos_pair = (infos, datetime.fromisoformat(next(iter(infos.values()))["observed_at"])) if infos else None
    with ledger(paths) as conn:
        if existing := _decision(conn, slot):
            return existing
        if infos:
            v6_paper.save_instruments(conn, infos)
        v6_paper.remember(conn, {"prices": prices, "rate": rate})
        v6_paper.bases(conn, clock())
        blocked = v6_paper.blocked_reasons(conn, clock())
        candidates = v6_head.candidates_at(cutoff, rankings, infos_pair, v6_paper.instruments(conn), holdings, open_at)
        day = decided.date().isoformat()
        bases = {"daily": _base(conn, "daily", day), "weekly": _base(conn, "weekly", _monday(day))}
        history = [equity for (equity,) in conn.execute("SELECT equity FROM valuations WHERE kind = 'watch' AND slot <= ? ORDER BY slot", (slot,))]
        try:
            total, used, summary = v6_head.account(v6_paper.state(conn), {"prices": prices, "rate": rate}, cutoff, bases, history, blocked, candidates)
        except v6_head.HeadError as error:
            row = _missing_row(slot, error.code, clock())
            v6_head.record(conn, paths.head, row, None)
            return row
        v6_paper.record_valuation(conn, _name(cutoff), "c", clock(), total, v6_paper.state(conn), used, [])
        saved = v6_paper.instruments(conn)
    content = v6_head.build_input(slot, open_at, ends, summary, candidates, news, decisions)
    observations = {
        "rankings": {market: [_iso(at), at <= cutoff] for market, (_, at) in rankings.items()},
        "instruments": {
            item["symbol"]: [saved[item["symbol"]]["observed_at"], "current" if infos and item["symbol"] in infos else "saved", True]
            for item in candidates
            if item["symbol"] in saved
        },
        "prices": {symbol: [_iso(at), received] for symbol, (_, at, received) in used.items() if symbol != "USDKRW"},
        "rate": [_iso(used["USDKRW"][1]), used["USDKRW"][2]] if "USDKRW" in used else None,
        "news": [_iso(news_ready), news is not None, str(news_path)],
    }
    result = v6_head.decide(paths.head, slot, content, observations, candidates, http, codex_credentials, clock, sleep)
    with ledger(paths) as conn:
        if existing := _decision(conn, slot):
            return existing
        row, raw = result if result else (_missing_row(slot, "V6_STARTED_LATE", clock()), None)
        v6_head.record(conn, paths.head, row, raw)
    return row


def _fill(
    paths: Paths,
    http: httpx.Client,
    token: toss.SharedToken,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    events: list[dict],
    calendar: dict,
    slot: str,
    targets: dict[str, Decimal],
) -> None:
    """E 뒤에 받은 호가로 판단 체결을 하고 rebalanced 사건을 한 트랜잭션으로 쓴다."""
    _, cutoff, fill_at = v6_head.times(slot)
    with ledger(paths) as conn:
        if _rebalanced(conn, slot):
            return
        ledger_state, infos = v6_paper.state(conn), v6_paper.instruments(conn)
    open_at = v6_market.sessions(calendar, fill_at) or {"KR": None, "US": None}
    symbols = [symbol for symbol in set(targets) | set(ledger_state["positions"]) if symbol in infos and v6_market.tradable(infos[symbol], open_at)]
    books = v6_paper.observe_books(http, token, symbols, fill_at, fill_at + FILL_WAIT, clock, sleep, events)
    with ledger(paths) as conn:
        if _rebalanced(conn, slot):
            return
        valuation = conn.execute("SELECT equity, used FROM valuations WHERE slot = ? AND kind = 'c'", (_name(cutoff),)).fetchone()
        rate = json.loads(valuation[1]).get("USDKRW")
        rate_c = Decimal(rate[0]) if rate and rate[2] else None  # 그 차례에 C까지 받은 환율만 쓴다
        blocked = bool(v6_paper.blocked_reasons(conn, fill_at))
        v6_paper.rebalance(conn, slot, fill_at, open_at, targets, valuation[0], rate_c, books, blocked, calendar)
        conn.execute("INSERT INTO events VALUES (?, 'rebalanced', ?)", (_iso(clock()), slot))
    folder = paths.head / slot[:10] / slot[11:].replace(":", "")
    folder.mkdir(parents=True, exist_ok=True)
    saved = {
        symbol: {
            "requested_at": _iso(book["requested_at"]),
            "observed_at": _iso(book["observed_at"]),
            "timestamp": None if book["timestamp"] is None else _iso(book["timestamp"]),
            "asks": [[str(price), volume] for price, volume in book["asks"]],
            "bids": [[str(price), volume] for price, volume in book["bids"]],
        }
        for symbol, book in books.items()
    }
    (folder / "orderbook.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _record_missing(paths: Paths, slot: str, error: str, clock: Callable[[], datetime]) -> None:
    with ledger(paths) as conn:
        if _decision(conn, slot) is None:
            v6_head.record(conn, paths.head, _missing_row(slot, error, clock()), None)


def _missing_row(slot: str, error: str, at: datetime) -> dict:
    stamp = _iso(at)
    return {
        "time": slot, "status": "missing", "positions": "[]", "strategy": "", "reason": "", "notes": "", "model": v6_head.MODEL,
        "reported_model": "", "attempts": 0, "error": error, "started_at": stamp, "finished_at": stamp,
    }


# v6-report


def report(paths: Paths, clock: Callable[[], datetime]) -> int:
    """지금까지의 보고서와, 끝 시각이 지났으면 판정(SPEC 6·7절). 네트워크를 부르지 않는다."""
    try:
        content, tables = build(paths, clock())
    except v6_market.V6Error as error:
        print(error)
        return 1
    folder = paths.reports / f"v6-{clock().astimezone(KST):%Y%m%d-%H%M%S}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, (columns, rows) in tables.items():
        with (folder / name).open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(columns)
            writer.writerows(rows)
    print(f"v6 보고서: {folder} 판정 {content['verdict']}")
    return 0


def build(paths: Paths, now: datetime) -> tuple[dict, dict[str, tuple[tuple, list]]]:
    """보고서 내용과 CSV 표들. 판정은 now가 끝 시각 이후일 때만 넣는다."""
    if not paths.paper.exists() or not paths.lock.exists():
        raise v6_market.V6Error("V6_NOT_STARTED", "v6 장부가 없다")
    with reading(paths) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        if "start" not in meta:
            raise v6_market.V6Error("V6_NOT_STARTED", "v6 장부에 시작일이 없다")
        start, end = meta["start"], datetime.fromisoformat(meta["end"])
        begin = _midnight(start)
        decisions = {row[0]: dict(zip(v6_head.COLUMNS, row)) for row in conn.execute(f"SELECT {', '.join(v6_head.COLUMNS)} FROM decisions")}
        valuations = conn.execute("SELECT slot, kind, at, equity, krw, usd, rate, stale FROM valuations ORDER BY slot").fetchall()
        fills = [dict(zip(("id", *v6_paper.FILL_COLUMNS), row)) for row in conn.execute(f"SELECT id, {', '.join(v6_paper.FILL_COLUMNS)} FROM fills ORDER BY id")]
        positions = conn.execute("SELECT symbol, quantity, avg_price FROM positions ORDER BY symbol").fetchall()
        cash = dict(conn.execute("SELECT currency, amount FROM cash"))
        calendar = v6_market.load_calendar(paths.calendar)
    days = _days(start, end)
    slots = [f"{day} {clock}" for day in days for clock, _ in v6_market.slots(calendar, day)]
    statuses = {slot: decisions[slot]["status"] if slot in decisions else "missing" for slot in slots}
    watch = [row for row in valuations if row[1] == "watch" and begin <= _slot_time(row[0]) <= end]
    in_period = [fill for fill in fills if begin <= datetime.fromisoformat(fill["at"]) <= end]
    last = watch[-1] if watch else None
    final = last[3] if last else v6_paper.START_CASH
    criteria = _criteria(slots, statuses, watch, in_period, days, begin, end)
    ended = now >= end
    verdict = ("통과" if all(item["passed"] for item in criteria.values()) else "불통과") if ended else None
    c_equity = {row[0]: row[3] for row in valuations if row[1] == "c"}
    strategies = _strategies(slots, decisions, c_equity, final)
    realized = {"KR": 0, "US": Decimal(0)}
    for fill in in_period:
        if fill["market"] == "KR":
            realized["KR"] += int(fill["realized"])
        else:
            realized["US"] += Decimal(fill["realized"])
    content = {
        "period": {"start": start, "end": end.isoformat(), "trading_days": [day for day in days if v6_market.trading_day(calendar, day)], "ended": ended},
        "model": v6_head.MODEL,
        "account": {
            "equity": final,
            "return_pct": v3_head.pct(Decimal(final) / v6_paper.START_CASH - 1),
            "max_drawdown_pct": v3_head.pct(criteria["max_drawdown"]["value"]),
            "krw": int(cash.get("KRW", 0)),
            "usd": cash.get("USD", "0"),
            "holdings": [{"symbol": symbol, "quantity": quantity, "avg_price": average} for symbol, quantity, average in positions],
            "realized": {"KR": realized["KR"], "US": str(realized["US"])},
        },
        "decisions": {"slots": len(slots), "by_status": {status: sum(value == status for value in statuses.values()) for status in STATUSES}},
        "criteria": {key: value | {"value": str(value["value"])} if key == "max_drawdown" else value for key, value in criteria.items()},
        "verdict": verdict,
        "generated_at": _iso(now),
    }
    tables = {
        "decisions.csv": (DECISION_COLUMNS, [[slot, statuses[slot], *(decisions.get(slot, {}).get(column, "") for column in DECISION_COLUMNS[2:])] for slot in slots]),
        "fills.csv": (v6_paper.FILL_COLUMNS, [[fill[column] for column in v6_paper.FILL_COLUMNS] for fill in in_period]),
        "equity.csv": (EQUITY_COLUMNS, [[row[0], row[2], row[3], row[4], row[5], row[6], row[7]] for row in watch]),
        "strategies.csv": (("strategy", "decisions", "pnl_krw", "return_share"), strategies),
    }
    return content, tables


def _criteria(
    slots: list[str], statuses: dict[str, str], watch: list[tuple], fills: list[dict], days: list[str], begin: datetime, end: datetime
) -> dict[str, dict]:
    """SPEC 6절 여섯 항목의 값과 통과 여부."""
    failed = sum(status != "ok" for status in statuses.values())
    peak, worst, breached = v6_paper.START_CASH, Decimal(0), False
    for row in watch:
        peak = max(peak, row[3])
        worst = max(worst, 1 - Decimal(row[3]) / peak)
        breached = breached or (peak - row[3]) * 5 >= peak
    expected = int((end - begin) / SLOT) + 1
    recorded = len({row[0] for row in watch})
    names = {row[0] for row in watch}
    midnights = [day for day in days[1:] if f"{day} 00:00" not in names]
    stale = sum(row[7] for row in watch)
    ledger_rules = {
        "negative_cash": sum(Decimal(fill["krw_after"]) < 0 or Decimal(fill["usd_after"]) < 0 for fill in fills),
        "over_five": sum(fill["holdings_after"] > v6_paper.MAX_HOLDINGS for fill in fills),
        "ineligible_buy": sum(fill["side"] == "BUY" and not fill["eligible"] for fill in fills),
        "untradable_fill": sum(not fill["tradable"] for fill in fills),
        "blocked_buy": sum(fill["side"] == "BUY" and fill["blocked"] for fill in fills),
    }
    return {
        "failures": {"failed": failed, "slots": len(slots), "passed": failed * 10 < len(slots)},
        "max_drawdown": {"value": worst, "passed": not breached},
        "valuations": {"recorded": recorded, "expected": expected, "passed": recorded * 10 >= expected * 9},
        "midnight": {"missing": midnights, "passed": not midnights},
        "stale": {"count": stale, "passed": stale == 0},
        "ledger": ledger_rules | {"passed": not any(ledger_rules.values())},
    }


def _strategies(slots: list[str], decisions: dict[str, dict], c_equity: dict[str, int], final: int) -> list[list]:
    """ok 판단마다 그 C 평가부터 다음 ok 판단의 C 평가(마지막은 마지막 자산)까지의 원화 자산 변화를 전략 이름에 붙인다."""
    oks = [slot for slot in slots if decisions.get(slot, {}).get("status") == "ok"]
    totals: dict[str, list] = {}
    for index, slot in enumerate(oks):
        start = c_equity[_name(v6_head.times(slot)[1])]
        stop = c_equity[_name(v6_head.times(oks[index + 1])[1])] if index + 1 < len(oks) else final
        entry = totals.setdefault(decisions[slot]["strategy"], [0, 0])
        entry[0] += 1
        entry[1] += stop - start
    return [[name, count, pnl, str(Decimal(pnl) / v6_paper.START_CASH)] for name, (count, pnl) in totals.items()]


# 도움 함수


def _quiet(call: Callable[[], object], default: object) -> object:
    """받지 못하면(V6Error) 기록하고 default를 돌려준다. 받은 데까지로 계속한다."""
    try:
        return call()
    except v6_market.V6Error as error:
        print(error)
        return default


def _save_events(paths: Paths, events: list[dict]) -> None:
    """그 명령에서 받은 429를 잠금 안에서 events에 toss_429로 쓴다."""
    if not events:
        return
    with ledger(paths) as conn:
        conn.executemany("INSERT INTO events VALUES (?, 'toss_429', ?)", [(event["at"], event["path"]) for event in events])
    events.clear()


def _decision(conn: sqlite3.Connection, slot: str) -> dict | None:
    row = conn.execute(f"SELECT {', '.join(v6_head.COLUMNS)} FROM decisions WHERE time = ?", (slot,)).fetchone()
    return dict(zip(v6_head.COLUMNS, row)) if row else None


def _valued(conn: sqlite3.Connection, slot: str) -> bool:
    return conn.execute("SELECT 1 FROM valuations WHERE slot = ? AND kind = 'watch'", (slot,)).fetchone() is not None


def _rebalanced(conn: sqlite3.Connection, slot: str) -> bool:
    return conn.execute("SELECT 1 FROM events WHERE kind = 'rebalanced' AND detail = ?", (slot,)).fetchone() is not None


def _base(conn: sqlite3.Connection, kind: str, key: str) -> int | None:
    row = conn.execute("SELECT equity FROM baselines WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    return row[0] if row else None


def _days(start: str, end: datetime) -> list[str]:
    """시작일부터 끝 시각 전날까지의 날짜."""
    day, last = Date.fromisoformat(start), end.astimezone(KST).date()
    days = []
    while day < last:
        days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def _floor(moment: datetime) -> datetime:
    moment = moment.astimezone(KST)
    return moment.replace(minute=moment.minute - moment.minute % 10, second=0, microsecond=0)


def _name(moment: datetime) -> str:
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _slot_time(name: str) -> datetime:
    return datetime.fromisoformat(name.replace(" ", "T")).replace(tzinfo=KST)


def _midnight(day: str) -> datetime:
    return datetime.combine(Date.fromisoformat(day), time(0), KST)


def _monday(day: str) -> str:
    value = Date.fromisoformat(day)
    return (value - timedelta(days=value.weekday())).isoformat()


def _iso(moment: datetime) -> str:
    return moment.astimezone(KST).isoformat(timespec="seconds")

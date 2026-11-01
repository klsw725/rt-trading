"""v4 헤드: 판단 차례, 계획 부하, 입력 만들기, 다시 부르기와 마감, 교체 한도, 판단 기록(v4 SPEC 3절, PRD 02)."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import httpx

from rt_trading import codex, market_data, sessions, v3_head
from rt_trading.toss import KST

KODEX200, INVERSE = "069500", "114800"
SUBORDINATES = json.loads(Path(__file__).with_name("v4_subordinates.json").read_text(encoding="utf-8"))
IDS = [row["id"] for row in SUBORDINATES]
PROMPT = Path(__file__).with_name("v4_head_prompt.txt").read_text(encoding="utf-8")
WAITS = {"pre_open": (60, 120, 240), "intraday": (15,), "pre_close": (15,)}  # 실패 뒤 다시 부르기 전에 기다리는 초. 끝의 값을 되풀이한다
AFTER = 5  # 장중·마감 전 판단의 마감과 교체 시각은 차례 + 5분
SWITCH_LIMIT, RECENT = 2, 20
COLUMNS = ("date", "slot", "kind", "status", "subordinate", "reason", "notes", "model", "reported_model", "attempts", "error", "started_at", "finished_at")
TABLE = ", ".join(f"{column} {'INTEGER' if column == 'attempts' else 'TEXT'} NOT NULL" for column in COLUMNS) + ", PRIMARY KEY (date, slot)"


class Slot(NamedTuple):
    time: str  # 차례 HH:MM. pre_open은 마감 시각을 차례 이름으로 쓴다
    kind: str  # pre_open, intraday, pre_close
    deadline: str  # 마감 HH:MM
    switch: str  # 교체 시각 HH:MM. pre_open은 개장(시가 동시호가)이다


def slots(day: str) -> list[Slot]:
    """판단일 day의 판단 차례. 장 일정 표의 개장 o와 마감 c로 정한다."""
    session = sessions.session(day, sessions.exceptions())
    open_at, close_at = _minutes(session.open), _minutes(session.close)
    found = [Slot(_clock(open_at - 10), "pre_open", _clock(open_at - 10), session.open)]
    for start in range(-(-(open_at + 60) // 60) * 60, close_at - 90 + 1, 60):  # o + 60분부터 c − 90분까지 정각마다
        found.append(Slot(_clock(start), "intraday", _clock(start + AFTER), _clock(start + AFTER)))
    last = close_at - 20
    found.append(Slot(_clock(last), "pre_close", _clock(last + AFTER), _clock(last + AFTER)))
    return found


def plan(days: list[str], decisions: list[dict]) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """기간의 거래일 days마다 계획 부하 P (시작 부하, [(교체 시각, 부하), …]). decisions는 (날짜, 차례) 순의 판단 행이다.

    시작 부하는 그날 pre_open이 ok면 그 부하, 아니면 전 거래일 마감 때의 P이고, 기간 첫날은 cash다.
    intraday·pre_close는 ok이고 그때의 P와 다른 부하를 골랐을 때만 차례 + 5분에 교체한다. 결함일인지는 보지 않는다.
    """
    result, held = {}, "cash"
    for day in days:
        rows = [row for row in decisions if row["date"] == day]
        start = next((row["subordinate"] for row in rows if row["kind"] == "pre_open" and row["status"] == "ok"), held)
        held, switches = start, []
        for row in rows:
            if row["kind"] != "pre_open" and row["status"] == "ok" and row["subordinate"] != held:
                switches.append((_clock(_minutes(row["slot"]) + AFTER), row["subordinate"]))
                held = row["subordinate"]
        result[day] = (start, switches)
    return result


def current(days: list[str], decisions: list[dict], day: str, slot: str) -> tuple[str, int]:
    """판단일 day의 차례 slot에서 (지금 부하, 남은 교체 횟수). slot보다 앞선 판단만 본다."""
    earlier = [row for row in decisions if (row["date"], row["slot"]) < (day, slot)]
    start, switches = plan([item for item in days if item <= day], earlier)[day]
    return (switches[-1][1] if switches else start), SWITCH_LIMIT - len(switches)


def build_input(
    day: str,
    slot: str,
    days: list[str],
    month_days: list[str],
    market_path: Path,
    us_path: Path,
    accounts: dict,
    decisions: list[dict],
    bars: dict[str, list[market_data.Bar]] | None,
    news: list[dict] | None,
) -> dict:
    """판단일 day, 차례 slot의 헤드 입력. 가격 자체는 넣지 않는다.

    days는 기간의 거래일, month_days는 day가 속한 달의 거래일, decisions는 (날짜, 차례) 순의 판단 행이다.
    accounts는 PRD 04가 정산 결과로 만든 계좌 요약이다.
      {"subordinates": {id: {"recent_returns_pct", "holding"}}, "head_account": {...}, "rule_head": {...},
       "morning_account": {...}, "head_returns": {날짜: 그날 헤드 계좌 수익률(%)}, "settled_through": 날짜 또는 None}
    bars는 종목마다 d의 1분봉이고 받지 못했으면 None, news는 PRD 01의 값이다.
    """
    info = _slot(day, slot)
    earlier = [row for row in decisions if (row["date"], row["slot"]) < (day, slot)]
    held, left = current(days, decisions, day, slot)
    return {
        "decision_date": day,
        "decision": {"kind": info.kind, "deadline": info.deadline, "current": held, "switches_left": left},
        **v3_head.market_input(day, month_days, market_path, us_path),
        "today_market": None if info.kind == "pre_open" else _today_market(day, slot, market_path, bars),
        "news": news,
        "subordinates": [
            {
                "id": row["id"],
                "description": row["description"],
                "backtest": row["backtest"],
                "recent_returns_pct": accounts["subordinates"][row["id"]]["recent_returns_pct"],
                "holding": accounts["subordinates"][row["id"]]["holding"],
            }
            for row in SUBORDINATES
        ],
        "head_account": accounts["head_account"],
        "rule_head": accounts["rule_head"],
        "morning_account": accounts["morning_account"],
        "recent_decisions": [
            {
                "date": row["date"],
                "slot": row["slot"],
                "kind": row["kind"],
                "status": row["status"],
                "subordinate": row["subordinate"],
                "reason": row["reason"],
                "head_return_pct": accounts["head_returns"].get(row["date"]),
            }
            for row in earlier[-RECENT:]
        ],
        "settled_through": accounts["settled_through"],
        "notes": next((row["notes"] for row in reversed(earlier) if row["status"] in ("ok", "capped")), ""),
    }


def decide(
    db_path: Path,
    head_dir: Path,
    day: str,
    slot: str,
    content: dict,
    http: httpx.Client,
    credentials: Callable[[], tuple[str, str]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> dict | None:
    """판단일 day, 차례 slot의 판단을 정해 기록하고 그 행을 돌려준다. 교체 한도는 입력의 decision으로 본다.

    이미 기록한 차례면 부르지 않고 그 행을 돌려준다. 첫 호출을 시작할 때 이미 마감이면 부르지도 기록하지도 않고 None이다.
    """
    for row in read(db_path):
        if (row["date"], row["slot"]) == (day, slot):
            return row
    info = _slot(day, slot)
    deadline = datetime.fromisoformat(f"{day}T{info.deadline}").replace(tzinfo=KST)
    started = clock()
    if started >= deadline:
        return None
    folder = head_dir / day / slot.replace(":", "")
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(folder / "input.json", content)
    text, waits = json.dumps(content, ensure_ascii=False), WAITS[info.kind]
    attempts, raw, reported, decision, status, error = 0, None, "", None, "failed", ""
    while True:
        attempts += 1
        try:
            reply = codex.ask(http, *credentials(), PROMPT, text)
            raw, reported = reply.text, reply.model or reported
            decision = v3_head.parse(reply.text, IDS)
            break
        except codex.CodexError as failure:
            status, error = "failed", failure.code
            if failure.code == "V3_CODEX_TOKEN_MISSING":
                break
        except v3_head.HeadError as failure:
            status, error = "invalid", failure.code
        wait = waits[min(attempts, len(waits)) - 1]  # 기다리는 시간은 앞 호출이 끝난 때부터 센다
        if clock() + timedelta(seconds=wait) >= deadline:
            break
        sleep(wait)
        if clock() >= deadline:  # 늦게 깨어났으면 마감 뒤에 부르지 않는다
            break
    finished = clock()
    if decision:
        status, error = ("ok" if finished < deadline else "late"), ""
        given = content["decision"]
        if status == "ok" and info.kind != "pre_open" and decision["subordinate"] != given["current"] and given["switches_left"] == 0:
            status = "capped"
    answered = status in ("ok", "capped")
    row = {
        "date": day,
        "slot": slot,
        "kind": info.kind,
        "status": status,
        "subordinate": decision["subordinate"] if answered or status == "late" else "",
        "reason": decision["reason"] if answered else "",
        "notes": decision["notes"] if answered else "",
        "model": codex.MODEL,
        "reported_model": reported,
        "attempts": attempts,
        "error": error,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
    }
    insert(db_path, row)
    if raw is not None:
        (folder / "response.txt").write_text(raw, encoding="utf-8")
    _write_json(folder / "decision.json", row)
    return row


def read(db_path: Path) -> list[dict]:
    """decisions 표의 행을 (날짜, 차례) 순으로 읽는다. 파일이 없으면 빈 목록이다."""
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute(f"CREATE TABLE IF NOT EXISTS decisions ({TABLE})")
        rows = db.execute(f"SELECT {', '.join(COLUMNS)} FROM decisions ORDER BY date, slot").fetchall()
    return [dict(zip(COLUMNS, row)) for row in rows]


def insert(db_path: Path, row: dict) -> None:
    """판단 한 행을 넣는다. PRD 04의 missing 행도 이것으로 넣는다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute(f"CREATE TABLE IF NOT EXISTS decisions ({TABLE})")
        db.execute(f"INSERT INTO decisions VALUES ({', '.join('?' * len(COLUMNS))})", [row[column] for column in COLUMNS])


def _today_market(day: str, until: str, market_path: Path, bars: dict[str, list[market_data.Bar]] | None) -> dict | None:
    """오늘 시세 요약. 장 일정 봉 시각이고 until보다 늦지 않은 봉만 쓴다. 두 종목 모두 쓸 봉이 없으면 None이다."""
    if bars is None:
        return None
    session = sessions.session(day, sessions.exceptions())
    summary = {}
    with closing(sqlite3.connect(market_path)) as db:
        for name, symbol in (("kodex200", KODEX200), ("inverse", INVERSE)):
            used = [bar for bar in bars[symbol] if bar.time in session.bars and bar.time <= until]
            previous = db.execute("SELECT close FROM candles_1d WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT 1", (symbol, day))
            summary[name] = _summary(used, previous.fetchone()[0], session.open) if used else None
    return summary if any(summary.values()) else None


def _summary(used: list[market_data.Bar], previous_close: int, opening: str) -> dict:
    """한 종목의 오늘 시세 요약. previous_close는 d보다 앞선 마지막 날 일봉의 원가격 종가다."""
    last, first, by_time = used[-1], market_data.open_bar(used), {bar.time: bar for bar in used}
    traded = [bar for bar in used if bar.volume]
    high, low = (max(bar.high for bar in traded), min(bar.low for bar in traded)) if traded else (0, 0)
    back = by_time.get(_clock(_minutes(last.time) - 30))
    returns, base, end = [], Decimal(first.open) if first else None, _minutes(opening) + 30
    while end <= _minutes(last.time):  # 개장부터 30분씩 끊은 구간. 첫 구간은 시가 봉의 시가로 나눈다
        bar = by_time.get(_clock(end))
        returns.append(v3_head.pct(Decimal(bar.close) / base - 1) if bar and base else None)
        base, end = (Decimal(bar.close) if bar else None), end + 30
    return {
        "data_through": last.time,
        "vs_prev_close_pct": v3_head.pct(Decimal(last.close) / Decimal(previous_close) - 1),
        "vs_open_pct": v3_head.pct(Decimal(last.close) / Decimal(first.open) - 1) if first else None,
        "range_position_pct": v3_head.pct(Decimal(last.close - low) / Decimal(high - low)) if high > low else None,
        "last_30m_pct": v3_head.pct(Decimal(last.close) / Decimal(back.close) - 1) if back else None,
        "returns_30m_pct": returns,
    }


def _slot(day: str, slot: str) -> Slot:
    return next(item for item in slots(day) if item.time == slot)


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

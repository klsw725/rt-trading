"""v3 헤드: 입력 만들기, 응답 검증, 다시 부르기와 판단 마감, 판단 기록(v3 SPEC 3절, PRD 01)."""

import csv
import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx

from rt_trading import codex, sessions, v3_us
from rt_trading.toss import KST

KODEX200, INVERSE = "069500", "114800"
SUBORDINATES = json.loads(Path(__file__).with_name("v3_subordinates.json").read_text(encoding="utf-8"))
IDS = [row["id"] for row in SUBORDINATES]
PROMPT = Path(__file__).with_name("v3_head_prompt.txt").read_text(encoding="utf-8")
WAITS = (60, 120, 240)  # 실패 뒤 다시 부르기 전에 기다리는 초. 그 뒤로는 마지막 값을 되풀이한다
DEADLINE_BEFORE_OPEN = timedelta(minutes=10)
REASON_LIMIT, NOTES_LIMIT, RECENT = 500, 2000, 20
COLUMNS = ("date", "status", "subordinate", "reason", "notes", "model", "reported_model", "attempts", "error", "started_at", "finished_at")
TABLE = ", ".join(f"{column} {'INTEGER' if column == 'attempts' else 'TEXT'} NOT NULL" for column in COLUMNS) + ", PRIMARY KEY (date)"
FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)  # 응답 전체를 감싼 코드 블록 하나


class HeadError(Exception):
    """응답 형식 오류."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def build_input(day: str, month_days: list[str], market_path: Path, us_path: Path, accounts: dict, decisions: list[dict]) -> dict:
    """판단일 day의 헤드 입력. 시세는 day 전의 일봉 수정 종가로만 계산하고, 가격 자체는 넣지 않는다.

    month_days는 day가 속한 달의 거래일, us_path는 미국 네 종목 일봉 파일, accounts는 PRD 03이 정산 결과로 만든 계좌 요약이다.
      {"subordinates": {id: {"recent_returns_pct", "holding"}}, "head_account": {...}, "rule_head": {...},
       "head_returns": {날짜: 그날 헤드 계좌 수익률(%)}, "settled_through": 날짜 또는 None}
    decisions는 decisions 표의 행이다.
    """
    with closing(sqlite3.connect(market_path)) as db:
        rows = db.execute("SELECT symbol, date, adj_close FROM candles_1d WHERE date < ? ORDER BY symbol, date", (day,)).fetchall()
    closes = {KODEX200: [], INVERSE: []}
    for symbol, date, adj_close in rows:
        if symbol in closes:
            closes[symbol].append((date, Decimal(adj_close)))
    prices = [price for _, price in closes[KODEX200]]
    returns = _daily_returns(prices)
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    session = sessions.session(day, sessions.exceptions())
    index = month_days.index(day)
    earlier = [row for row in decisions if row["date"] < day]
    return {
        "decision_date": day,
        "session": {"open": session.open, "close": session.close, "note": _note(day)},
        "month_position": {"from_start": index + 1, "from_end": len(month_days) - index},
        "data_through": closes[KODEX200][-1][0],
        "market": {
            "kodex200": {
                "daily_returns_pct": [pct(r) for r in returns],
                "returns_pct": {f"{k}d": pct(prices[-1] / prices[-1 - k] - 1) for k in (1, 5, 20, 60)},
                "vs_average_pct": {f"{k}d": pct(prices[-1] / (sum(prices[-k:]) / k) - 1) for k in (5, 20, 60, 200)},
                "volatility_20d_pct": pct(variance.sqrt() * Decimal(252).sqrt()),
                "drawdown_60d_pct": pct(prices[-1] / max(prices[-60:]) - 1),
            },
            "inverse": {"daily_returns_pct": [pct(r) for r in _daily_returns([price for _, price in closes[INVERSE]])]},
        },
        "us_market": _us_market(us_path, day),
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
        "recent_decisions": [
            {
                "date": row["date"],
                "subordinate": row["subordinate"],
                "reason": row["reason"],
                "status": row["status"],
                "head_return_pct": accounts["head_returns"].get(row["date"]),
            }
            for row in earlier[-RECENT:]
        ],
        "rule_head": accounts["rule_head"],
        "settled_through": accounts["settled_through"],
        "notes": next((row["notes"] for row in reversed(earlier) if row["status"] == "ok"), ""),
    }


def parse(text: str) -> dict:
    """응답 텍스트를 검증해 {"subordinate", "reason", "notes"}를 돌려준다. 어기면 V3_HEAD_RESPONSE_INVALID."""
    text = text.strip()
    if fenced := FENCE.fullmatch(text):
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise HeadError("V3_HEAD_RESPONSE_INVALID", f"JSON 객체 하나가 아니다({error.msg})") from error
    if not isinstance(value, dict) or set(value) != {"subordinate", "reason", "notes"}:
        raise HeadError("V3_HEAD_RESPONSE_INVALID", "키가 subordinate, reason, notes 셋이 아니다")
    if not all(isinstance(item, str) for item in value.values()):
        raise HeadError("V3_HEAD_RESPONSE_INVALID", "값이 모두 문자열이 아니다")
    if value["subordinate"] not in IDS:
        raise HeadError("V3_HEAD_RESPONSE_INVALID", f"목록에 없는 부하 {value['subordinate']!r}")
    if len(value["reason"]) > REASON_LIMIT or len(value["notes"]) > NOTES_LIMIT:
        raise HeadError("V3_HEAD_RESPONSE_INVALID", f"reason {len(value['reason'])}자, notes {len(value['notes'])}자로 길이를 넘는다")
    return value


def decision_deadline(day: str) -> datetime:
    """판단일 day의 판단 마감. 그날 개장 10분 전이다."""
    opening = datetime.fromisoformat(f"{day}T{sessions.session(day, sessions.exceptions()).open}").replace(tzinfo=KST)
    return opening - DEADLINE_BEFORE_OPEN


def decide(
    db_path: Path,
    head_dir: Path,
    day: str,
    content: dict,
    http: httpx.Client,
    credentials: Callable[[], tuple[str, str]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> dict | None:
    """판단일 day의 판단을 정해 기록하고 그 행을 돌려준다.

    이미 기록한 날이면 부르지 않고 그 행을 돌려준다. 첫 호출을 시작할 때 이미 마감이 지났으면 부르지도 기록하지도 않고 None이다.
    """
    for row in read(db_path):
        if row["date"] == day:
            return row
    deadline = decision_deadline(day)
    started = clock()
    if started >= deadline:
        return None
    folder = head_dir / day
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(folder / "input.json", content)
    text = json.dumps(content, ensure_ascii=False)
    attempts, waits, raw, reported, decision, status, error = 0, iter(WAITS), None, "", None, "failed", ""
    while True:
        attempts += 1
        try:
            reply = codex.ask(http, *credentials(), PROMPT, text)
            raw, reported = reply.text, reply.model or reported
            decision = parse(reply.text)
            break
        except codex.CodexError as failure:
            status, error = "failed", failure.code
            if failure.code == "V3_CODEX_TOKEN_MISSING":
                break
        except HeadError as failure:
            status, error = "invalid", failure.code
        wait = next(waits, WAITS[-1])  # 기다리는 시간은 앞 호출이 끝난 때부터 센다
        if clock() + timedelta(seconds=wait) >= deadline:
            break
        sleep(wait)
    finished = clock()
    if decision:
        status, error = ("ok" if finished < deadline else "late"), ""
    ok = status == "ok"
    row = {
        "date": day,
        "status": status,
        "subordinate": decision["subordinate"] if ok else "cash",
        "reason": decision["reason"] if ok else "",
        "notes": decision["notes"] if ok else "",
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
    """decisions 표의 행을 날짜순으로 읽는다. 파일이 없으면 빈 목록이다."""
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute(f"CREATE TABLE IF NOT EXISTS decisions ({TABLE})")
        rows = db.execute(f"SELECT {', '.join(COLUMNS)} FROM decisions ORDER BY date").fetchall()
    return [dict(zip(COLUMNS, row)) for row in rows]


def insert(db_path: Path, row: dict) -> None:
    """판단 한 행을 넣는다. PRD 03의 missing 행도 이것으로 넣는다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute(f"CREATE TABLE IF NOT EXISTS decisions ({TABLE})")
        db.execute(f"INSERT INTO decisions VALUES ({', '.join('?' * len(COLUMNS))})", [row[column] for column in COLUMNS])


def _daily_returns(prices: list[Decimal]) -> list[Decimal]:
    """최근 20거래일 일별 수익률, 오래된 것부터."""
    return [prices[i] / prices[i - 1] - 1 for i in range(len(prices) - 20, len(prices))]


def _us_market(path: Path, day: str) -> dict | None:
    """미국 날짜가 day보다 앞선 일봉으로 만든 미국 시세 요약. 수익률은 종목마다 그 종목의 일봉(미국 거래일)으로 센다."""
    if not path.exists():
        return None
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT symbol, date, adj_close FROM us_candles_1d WHERE date < ? ORDER BY symbol, date", (day,)).fetchall()
    closes = {symbol: [Decimal(close) for row_symbol, _, close in rows if row_symbol == symbol] for symbol in v3_us.SYMBOLS}
    return {
        "session_date": max(date for _, date, _ in rows),
        "returns_pct": {symbol: {f"{k}d": pct(prices[-1] / prices[-1 - k] - 1) for k in (1, 5, 20)} for symbol, prices in closes.items()},
    }


def pct(value: Decimal) -> float:
    """% 단위로 소수 둘째 자리에서 반올림한다. −0.00은 0으로 쓴다."""
    return float((value * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) + 0)


def _note(day: str) -> str:
    """장 일정 표의 reason. 정상일은 빈 문자열이다."""
    with sessions.TABLE.open(encoding="utf-8") as file:
        return next((row["reason"] for row in csv.DictReader(file) if row["date"] == day), "")


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

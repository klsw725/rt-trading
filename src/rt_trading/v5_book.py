"""v5 실계좌 저장소 data/v5/live.sqlite3: 그날 설정, 낼 주문과 주문, 장부, 정지, 한도·거부 기록, 차례 기록(v5 PRD 03)."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (date TEXT PRIMARY KEY, mode TEXT NOT NULL, account_seq INTEGER, limits TEXT, commission TEXT, ended INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ledgers (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, account_seq INTEGER, start_cash INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ledger_id INTEGER NOT NULL, time TEXT NOT NULL, reason TEXT NOT NULL,
    cash INTEGER NOT NULL, symbol TEXT, quantity INTEGER NOT NULL, client_order_id TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ledger_id INTEGER, date TEXT NOT NULL, turn TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, tif TEXT NOT NULL, quantity INTEGER NOT NULL, created_at TEXT NOT NULL,
    order_id TEXT, status TEXT NOT NULL, toss_status TEXT, filled_quantity INTEGER NOT NULL DEFAULT 0, filled_amount TEXT,
    average_price TEXT, commission TEXT, tax TEXT, applied_quantity INTEGER NOT NULL DEFAULT 0, applied_amount INTEGER NOT NULL DEFAULT 0,
    applied_commission INTEGER NOT NULL DEFAULT 0, applied_tax INTEGER NOT NULL DEFAULT 0, token_errors INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS live_turn ON orders (date, turn) WHERE kind = 'live';
CREATE TABLE IF NOT EXISTS stops (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL, reason TEXT NOT NULL, cleared_at TEXT);
CREATE TABLE IF NOT EXISTS latches (ledger_id INTEGER NOT NULL, kind TEXT NOT NULL, period TEXT NOT NULL, value INTEGER NOT NULL, PRIMARY KEY (ledger_id, kind, period));
CREATE TABLE IF NOT EXISTS turns (
    time TEXT NOT NULL, turn TEXT NOT NULL, mode TEXT NOT NULL, target TEXT, reason TEXT, action TEXT NOT NULL,
    prices TEXT, cash INTEGER, holdings TEXT, equity INTEGER, real TEXT, matched INTEGER, limits TEXT
);
"""
_ready: set[Path] = set()  # 이 프로세스에서 표를 만든 파일


@contextmanager
def connect(path: Path):
    """WAL 모드로 열고(처음 한 번 표를 만든다) with 블록 하나를 한 트랜잭션으로 한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        if path not in _ready:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            _ready.add(path)
        with db:
            yield db


def read(path: Path, sql: str, params: tuple = ()) -> list[dict]:
    """읽기 전용으로 연다. 파일이나 표가 없으면 빈 목록이다(현황 페이지가 쓴다)."""
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute(sql, params)]
        except sqlite3.OperationalError:
            return []


def day(db: sqlite3.Connection, date: str) -> dict | None:
    row = db.execute("SELECT * FROM days WHERE date = ?", (date,)).fetchone()
    return dict(row) if row else None


def ledger(db: sqlite3.Connection, kind: str) -> dict | None:
    row = db.execute("SELECT * FROM ledgers WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    return dict(row) if row else None


def create_ledger(db: sqlite3.Connection, kind: str, account_seq: int | None, start_cash: int, now: datetime) -> int:
    return db.execute(
        "INSERT INTO ledgers (kind, account_seq, start_cash, created_at) VALUES (?, ?, ?, ?)", (kind, account_seq, start_cash, now.isoformat(timespec="seconds"))
    ).lastrowid


def balance(db: sqlite3.Connection, ledger_id: int, until: str | None = None) -> tuple[int, dict[str, int]]:
    """장부의 (현금, 종목별 수량). until(ISO 시각)을 주면 그 시각까지의 기록만 센다."""
    start = db.execute("SELECT start_cash FROM ledgers WHERE id = ?", (ledger_id,)).fetchone()[0]
    rows = db.execute(
        "SELECT cash, symbol, quantity FROM entries WHERE ledger_id = ?" + (" AND time <= ?" if until else ""),
        (ledger_id, until) if until else (ledger_id,),
    ).fetchall()
    holdings: dict[str, int] = {}
    for _, symbol, quantity in rows:
        if symbol:
            holdings[symbol] = holdings.get(symbol, 0) + quantity
    return start + sum(cash for cash, _, _ in rows), {symbol: quantity for symbol, quantity in holdings.items() if quantity}


def add_intent(db: sqlite3.Connection, row: dict) -> None:
    """낼 주문을 sending으로 쓴다. 같은 (날짜, 차례)의 두 번째 실주문은 sqlite3.IntegrityError다."""
    columns = ("client_order_id", "kind", "ledger_id", "date", "turn", "symbol", "side", "tif", "quantity", "created_at")
    db.execute(f"INSERT INTO orders ({', '.join(columns)}, status) VALUES ({', '.join('?' * len(columns))}, 'sending')", tuple(row[key] for key in columns))


def orders(db: sqlite3.Connection, where: str = "1", params: tuple = ()) -> list[dict]:
    return [dict(row) for row in db.execute(f"SELECT * FROM orders WHERE {where} ORDER BY created_at, client_order_id", params)]


def update_order(db: sqlite3.Connection, client_order_id: str, **fields: object) -> None:
    db.execute(f"UPDATE orders SET {', '.join(f'{key} = ?' for key in fields)} WHERE client_order_id = ?", (*fields.values(), client_order_id))


def apply_fill(db: sqlite3.Connection, order: dict, snapshot: dict, now: datetime) -> bool:
    """상세 조회 snapshot의 체결에서 앞서 반영한 값을 빼 그 차이만 장부에 쓴다. 체결 금액·평균가가 모두 없으면 False(안전 정지할 것)다.

    부르는 쪽의 트랜잭션(connect의 with 블록) 하나 안에서 장부 기록과 반영 값 바꾸기를 함께 한다.
    """
    quantity = snapshot["filled_quantity"]
    if snapshot["filled_amount"] is not None:
        amount = snapshot["filled_amount"]
    elif snapshot["average_price"] is not None:
        amount = snapshot["average_price"] * quantity
    elif quantity:
        return False
    else:
        amount = Decimal(0)
    amount = int(Decimal(amount).to_integral_value(rounding=ROUND_CEILING if order["side"] == "BUY" else ROUND_FLOOR))  # 원 미만은 봇에 불리하게
    commission, tax = int(snapshot["commission"] or 0), int(snapshot["tax"] or 0)
    delta_quantity, delta_amount = quantity - order["applied_quantity"], amount - order["applied_amount"]
    delta_cost = (commission - order["applied_commission"]) + (tax - order["applied_tax"])
    if delta_quantity or delta_amount or delta_cost:
        sign = -1 if order["side"] == "BUY" else 1
        db.execute(
            "INSERT INTO entries (ledger_id, time, reason, cash, symbol, quantity, client_order_id) VALUES (?, ?, 'fill', ?, ?, ?, ?)",
            (order["ledger_id"], now.isoformat(timespec="seconds"), sign * delta_amount - delta_cost, order["symbol"], -sign * delta_quantity, order["client_order_id"]),
        )
    update_order(
        db,
        order["client_order_id"],
        toss_status=snapshot["status"],
        filled_quantity=quantity,
        filled_amount=None if snapshot["filled_amount"] is None else str(snapshot["filled_amount"]),
        average_price=None if snapshot["average_price"] is None else str(snapshot["average_price"]),
        commission=None if snapshot["commission"] is None else str(snapshot["commission"]),
        tax=None if snapshot["tax"] is None else str(snapshot["tax"]),
        applied_quantity=quantity,
        applied_amount=amount,
        applied_commission=commission,
        applied_tax=tax,
    )
    return True


def add_stop(db: sqlite3.Connection, kind: str, mode: str, reason: str, now: datetime) -> int:
    return db.execute(
        "INSERT INTO stops (time, kind, mode, reason) VALUES (?, ?, ?, ?)", (now.isoformat(timespec="seconds"), kind, mode, reason)
    ).lastrowid


def active_stops(db: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in db.execute("SELECT * FROM stops WHERE cleared_at IS NULL ORDER BY id")]


def clear_stops(db: sqlite3.Connection, up_to: int, now: datetime, manual: bool) -> None:
    """순번 up_to까지의 풀리지 않은 정지를 푼다. manual이 거짓이면 manual 정지는 남긴다."""
    db.execute(
        "UPDATE stops SET cleared_at = ? WHERE cleared_at IS NULL AND id <= ?" + ("" if manual else " AND kind != 'manual'"),
        (now.isoformat(timespec="seconds"), up_to),
    )


def latch(db: sqlite3.Connection, ledger_id: int, kind: str, period: str) -> int | None:
    row = db.execute("SELECT value FROM latches WHERE ledger_id = ? AND kind = ? AND period = ?", (ledger_id, kind, period)).fetchone()
    return row[0] if row else None


def set_latch(db: sqlite3.Connection, ledger_id: int, kind: str, period: str, value: int) -> None:
    db.execute("INSERT OR REPLACE INTO latches VALUES (?, ?, ?, ?)", (ledger_id, kind, period, value))


def add_turn(db: sqlite3.Connection, row: dict) -> None:
    columns = ("time", "turn", "mode", "target", "reason", "action", "prices", "cash", "holdings", "equity", "real", "matched", "limits")
    values = tuple(json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), dict | list) else row.get(key) for key in columns)
    db.execute(f"INSERT INTO turns VALUES ({', '.join('?' * len(columns))})", values)

"""시세를 SQLite 파일 data/market.sqlite3에 저장하고 읽는다."""

import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 1
TABLES = {
    "candles_1m": (
        "symbol TEXT NOT NULL, time TEXT NOT NULL, open INTEGER NOT NULL, high INTEGER NOT NULL,"
        " low INTEGER NOT NULL, close INTEGER NOT NULL, volume INTEGER NOT NULL, PRIMARY KEY (symbol, time)"
    ),
    "candles_1d": (
        "symbol TEXT NOT NULL, date TEXT NOT NULL, open INTEGER NOT NULL, high INTEGER NOT NULL,"
        " low INTEGER NOT NULL, close INTEGER NOT NULL, volume INTEGER NOT NULL, adj_close INTEGER NOT NULL,"
        " PRIMARY KEY (symbol, date)"
    ),
    "fetch_runs": (
        "fetched_at TEXT NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL, data_start TEXT NOT NULL,"
        " data_end TEXT NOT NULL, bar_count INTEGER NOT NULL, PRIMARY KEY (fetched_at, symbol, interval)"
    ),
}
ORDER = {"candles_1m": "symbol, time", "candles_1d": "symbol, date", "fetch_runs": "fetched_at, symbol, interval"}


def replace(path: Path, minute: list[tuple], daily: list[tuple], runs: list[tuple]) -> None:
    """한 트랜잭션 안에서 기존 봉을 지우고 새 봉과 수집 기록을 넣는다. 중간에 실패하면 기존 내용이 그대로 남는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        for table, columns in TABLES.items():
            db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.execute("DELETE FROM candles_1m")
        db.execute("DELETE FROM candles_1d")
        db.executemany("INSERT INTO candles_1m VALUES (?, ?, ?, ?, ?, ?, ?)", minute)
        db.executemany("INSERT INTO candles_1d VALUES (?, ?, ?, ?, ?, ?, ?, ?)", daily)
        db.executemany("INSERT INTO fetch_runs VALUES (?, ?, ?, ?, ?, ?)", runs)


def read(path: Path, table: str) -> list[tuple]:
    """표 하나를 기본 키 오름차순으로 읽는다."""
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY {ORDER[table]}").fetchall()

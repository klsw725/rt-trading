"""장 일정. 정상일은 09:00~15:30이고, 예외일만 krx_sessions.csv에 적는다."""

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

TABLE = Path(__file__).with_name("krx_sessions.csv")
NORMAL = ("09:00", "15:30")


class Session(NamedTuple):
    open: str  # 개장 HH:MM
    close: str  # 마감 HH:MM
    bars: list[str]  # 예상 봉 시각. 첫 봉은 시가 동시호가, 마지막 봉은 종가 동시호가 봉이다
    window: list[str]  # 종가 동시호가 접수 구간 봉 시각(마감 9분 전부터 1분 전까지)


def exceptions() -> dict[str, tuple[str, str]]:
    """예외일 표. 날짜 → (개장, 마감)."""
    with TABLE.open(encoding="utf-8") as file:
        return {row["date"]: (row["open"], row["close"]) for row in csv.DictReader(file)}


def session(date: str, table: dict[str, tuple[str, str]]) -> Session:
    """그날의 장 일정. 봉 시각은 봉이 끝나는 시각이라 개장 1분 뒤부터 마감까지 1분 간격이다."""
    open_, close = table.get(date, NORMAL)
    start, end = datetime.strptime(open_, "%H:%M"), datetime.strptime(close, "%H:%M")
    bars = [(start + timedelta(minutes=m)).strftime("%H:%M") for m in range(1, (end - start).seconds // 60 + 1)]
    return Session(open_, close, bars, bars[-10:-1])

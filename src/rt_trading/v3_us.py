"""v3 미국 시세: 미국 네 종목 일봉을 받아 저장한다(v3 SPEC 3.2·5.1, PRD 03)."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from rt_trading import toss
from rt_trading.toss import KST

SYMBOLS = ("SOXX", "QQQ", "SPY", "MU")  # 미국 반도체 ETF, 나스닥100 ETF, S&P500 ETF, 마이크론
TABLE = "symbol TEXT NOT NULL, date TEXT NOT NULL, adj_close TEXT NOT NULL, PRIMARY KEY (symbol, date)"
MIN_ROWS = 21  # 20거래일 수익률에 드는 일봉 수


def run(
    http: httpx.Client, credentials: tuple[str, str], path: Path, clock: Callable[[], datetime], token: toss.SharedToken | None = None
) -> int:
    """종목마다 수정 일봉을 한 번(200개) 받아, 실행한 날 전의 일봉만 한 트랜잭션으로 저장한다.

    하나라도 실패하면 오류 코드를 출력하고 저장하지 않은 채 1을 돌려준다. token을 주지 않으면 토큰을 새로 받는다.
    """
    today = clock().date().isoformat()
    try:
        token = token or toss.issue_token(http, *credentials)
        received = {symbol: toss.candles(http, token, symbol, "1d", True, today) for symbol in SYMBOLS}  # since가 오늘이면 첫 쪽에서 멈춘다
    except toss.TossError as error:
        print(error)
        return 1
    rows = []
    for symbol, candles in received.items():
        try:
            closes = {datetime.fromisoformat(c["timestamp"]).astimezone(KST).date().isoformat(): Decimal(c["closePrice"]) for c in candles}
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            print(f"V3_US_RESPONSE_INVALID: {symbol} 일봉을 읽을 수 없다({error!r})")
            return 1
        kept = sorted((date, close) for date, close in closes.items() if date < today)  # 오늘 날짜는 아직 끝나지 않은 미국 장
        if len(closes) != len(candles) or len(kept) < MIN_ROWS or not all(close.is_finite() and close > 0 for _, close in kept):
            print(f"V3_US_RESPONSE_INVALID: {symbol} 일봉의 날짜가 겹치거나, {MIN_ROWS}개보다 적거나, 종가가 0보다 큰 수가 아니다")
            return 1
        rows += [(symbol, date, str(close)) for date, close in kept]
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"CREATE TABLE IF NOT EXISTS us_candles_1d ({TABLE})")
        db.execute("DELETE FROM us_candles_1d")
        db.executemany("INSERT INTO us_candles_1d VALUES (?, ?, ?)", rows)
    for symbol in SYMBOLS:
        dates = [date for row_symbol, date, _ in rows if row_symbol == symbol]
        print(f"{symbol} 1d {dates[0]} ~ {dates[-1]} {len(dates):,}개")
    print(f"저장: {path}")
    return 0

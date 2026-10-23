"""저장된 시세를 검증하고 기준가를 계산해 검증된 데이터를 만든다."""

import csv
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

from rt_trading import sessions, store

SYMBOLS = ("069500", "114800")
DEFECTS = Path(__file__).with_name("data_defects.csv")


class Violation(NamedTuple):
    code: str
    date: str  # 날짜와 관계없으면 빈 문자열
    symbol: str  # 종목과 관계없으면 빈 문자열
    detail: str


class Bar(NamedTuple):
    time: str  # 봉이 끝나는 시각 HH:MM
    open: int
    high: int
    low: int
    close: int
    volume: int


class MarketData(NamedTuple):
    """검증을 통과한 데이터. PRD 03에 넘긴다."""

    days: list[str]  # 거래일 오름차순
    sessions: dict[str, sessions.Session]  # 날짜 → 장 일정
    bars: dict[str, dict[str, list[Bar]]]  # 종목 → 날짜 → 1분봉(장 일정 밖 거래량 0 봉을 버린 뒤, 시각 오름차순)
    base: dict[str, dict[str, int]]  # 종목 → 날짜 → 기준가. 데이터 첫 거래일(준비일)은 없다
    defects: list[str]  # 데이터 기간의 결함일. 이날은 거래하지 않는다
    daily: dict[str, dict[str, tuple[int, int]]] = {}  # 종목 → 날짜 → (일봉 원가격 종가, 수정 종가)


def load(path: Path) -> tuple[MarketData | None, list[Violation]]:
    """SPEC 3.4대로 검증한다. 어긴 것이 있으면 (None, 어긴 것 모두)를 날짜순으로 돌려준다."""
    violations = _missing(path)
    if violations:
        return None, violations
    table = sessions.exceptions()
    with DEFECTS.open(encoding="utf-8") as file:
        defects = {row["date"] for row in csv.DictReader(file)}
    daily = {symbol: {} for symbol in SYMBOLS}
    for symbol, date, open_, high, low, close, volume, adj_close in store.read(path, "candles_1d"):
        daily[symbol][date] = (close, adj_close)
        if high < max(open_, close) or low > min(open_, close) or volume < 0:
            violations.append(Violation("V1_BAR_MALFORMED", date, symbol, "일봉의 OHLC나 거래량이 맞지 않는다"))
    minute = {symbol: {} for symbol in SYMBOLS}
    for symbol, time, *values in store.read(path, "candles_1m"):
        minute[symbol].setdefault(time[:10], []).append(Bar(time[11:], *values))

    first, second = (set(daily[symbol]) for symbol in SYMBOLS)
    for date in first ^ second:
        absent = SYMBOLS[1] if date in first else SYMBOLS[0]
        violations.append(Violation("V1_TRADING_DAY_MISMATCH", date, absent, "이 종목만 그날 일봉이 없다"))
    for symbol in SYMBOLS:
        for date in set(daily[symbol]) ^ set(minute[symbol]):
            violations.append(Violation("V1_TRADING_DAY_MISMATCH", date, symbol, "일봉과 1분봉의 거래일이 다르다"))
        for date, bars in minute[symbol].items():
            session = sessions.session(date, table)
            # 장 일정 밖의 거래량 0 봉은 토스가 채운 봉이라 버린다
            bars = minute[symbol][date] = [bar for bar in bars if bar.volume or bar.time in session.bars]
            violations += _check_bars(symbol, date, bars, session, date in defects)

    base = {symbol: {} for symbol in SYMBOLS}
    for symbol in SYMBOLS:
        dates = sorted(daily[symbol])
        for prev, date in zip(dates, dates[1:]):  # 데이터 첫 거래일은 준비일이라 계산하지 않는다
            price, problem = base_price(*daily[symbol][prev], *daily[symbol][date])
            if problem:
                violations.append(Violation("V1_BASE_PRICE_INVALID", date, symbol, problem))
            base[symbol][date] = price

    if violations:
        return None, sorted(violations)
    days = sorted(first)
    data = MarketData(days, {date: sessions.session(date, table) for date in days}, minute, base, sorted(defects & first), daily)
    return data, []


def base_price(prev_close: int, prev_adj_close: int, close: int, adj_close: int) -> tuple[int, str]:
    """SPEC 3.3의 기준가. (기준가, 문제)를 돌려주며, 문제가 없으면 빈 문자열이다."""
    # 전날 종가 × r(전날) ÷ r(그날)은 전날 수정 종가 × 그날 종가 ÷ 그날 수정 종가와 같다. 0.5원은 올린다
    price = (2 * prev_adj_close * close + adj_close) // (2 * adj_close)
    if abs(price - prev_close) <= 2:
        return prev_close, ""
    if price > prev_close or price * 100 < prev_close * 97:
        return price, f"기준가 {price}원이 전날 종가 {prev_close}원보다 2원 넘게 높거나 3% 넘게 낮다"
    return price, ""  # 분배락일


def open_bar(bars: list[Bar]) -> Bar | None:
    """시가 봉. 그날 처음 거래량이 있는 봉이다(SPEC 3.3)."""
    return next((bar for bar in bars if bar.volume), None)


def describe(violations: list[Violation]) -> str:
    """어긴 것을 한 줄씩 코드, 날짜, 종목, 내용으로 적는다."""
    return "\n".join(f"{v.code} {v.date or '-'} {v.symbol or '-'} {v.detail}" for v in violations)


def _missing(path: Path) -> list[Violation]:
    """V1_DATA_MISSING: DB 파일, 종목별 1분봉·일봉 행, 가장 늦은 수집 기록 네 행 중 없는 것."""
    if not path.exists():
        return [Violation("V1_DATA_MISSING", "", "", f"{path} 파일이 없다")]
    found = []
    with closing(sqlite3.connect(path)) as db:
        try:
            for table in ("candles_1m", "candles_1d"):
                for symbol in SYMBOLS:
                    if not db.execute(f"SELECT 1 FROM {table} WHERE symbol = ? LIMIT 1", (symbol,)).fetchone():
                        found.append(Violation("V1_DATA_MISSING", "", symbol, f"{table}에 행이 없다"))
            latest = db.execute("SELECT MAX(fetched_at) FROM fetch_runs").fetchone()[0]
            runs = set(db.execute("SELECT symbol, interval FROM fetch_runs WHERE fetched_at = ?", (latest,)))
        except sqlite3.OperationalError as error:
            return [Violation("V1_DATA_MISSING", "", "", f"표를 읽을 수 없다({error})")]
    if {(symbol, interval) for symbol in SYMBOLS for interval in ("1m", "1d")} - runs:
        found.append(Violation("V1_DATA_MISSING", "", "", f"가장 늦은 수집 기록({latest})의 fetch_runs 네 행이 다 있지 않다"))
    return found


def _check_bars(symbol: str, date: str, bars: list[Bar], session: sessions.Session, defect: bool) -> list[Violation]:
    """그날 1분봉을 확인한다. 결함일에는 장 일정, 시가·종가 봉, 접수 구간을 확인하지 않는다."""
    found = []
    times = [bar.time for bar in bars]
    malformed = [
        bar.time
        for bar in bars
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close) or bar.volume < 0
    ]
    if malformed:
        found.append(Violation("V1_BAR_MALFORMED", date, symbol, f"OHLC나 거래량이 맞지 않는 봉: {', '.join(malformed)}"))
    if defect:
        return found
    missing, extra = sorted(set(session.bars) - set(times)), sorted(set(times) - set(session.bars))
    if missing or extra:
        found.append(
            Violation("V1_SESSION_MISMATCH", date, symbol, f"빠진 봉 {len(missing)}개 {missing[:3]}, 그 밖의 봉 {len(extra)}개 {extra[:3]}")
        )
    volume = {bar.time: bar.volume for bar in bars}
    if open_bar(bars) is None:
        found.append(Violation("V1_AUCTION_VOLUME_ZERO", date, symbol, "거래량이 있는 봉이 없어 시가 봉이 없다"))
    elif volume.get(session.bars[-1]) == 0:
        found.append(Violation("V1_AUCTION_VOLUME_ZERO", date, symbol, f"종가 동시호가 봉 {session.bars[-1]}의 거래량이 0이다"))
    traded = [time for time in session.window if volume.get(time, 0) > 0]
    if traded:
        found.append(Violation("V1_AUCTION_WINDOW_TRADED", date, symbol, f"접수 구간 봉 {', '.join(traded)}에 거래량이 있다"))
    return found

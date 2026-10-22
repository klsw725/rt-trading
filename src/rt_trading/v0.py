"""v0: 토스 일봉으로 종가 베팅과 갭 되돌림을 빠르게 확인한다."""

import json
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import httpx

from rt_trading import toss

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "v0.sqlite3"
REPORT_DIR = ROOT / "data" / "reports"
KST = timezone(timedelta(hours=9))

KODEX200 = "069500"
INVERSE = "114800"
C = 0.000188  # 매수·매도마다 수수료
TAX = 0.154  # 인버스 매매 이익에 매기는 세율
TICK_CHANGE = "2023-12-11"  # 이날부터 2,000원 미만 ETF 호가 단위가 1원


class Day(NamedTuple):
    date: str
    open: float
    close: float
    adj_open: float
    adj_close: float


class Result(NamedTuple):
    """거래일 하나의 결과. 그날 끝난 매매가 없으면 buy, net, gross가 None이다."""

    date: str
    buy: str | None
    net: float | None  # 수수료·호가 차이·세금을 뺀 수익률
    gross: float | None  # 비용 전 수익률


class DataError(Exception):
    pass


def merge(symbol: str, raw: list[dict], adjusted: list[dict], today: str) -> list[Day]:
    """원가격과 수정 가격 일봉을 날짜로 합치고 검사한다. today와 그 뒤 봉은 버린다."""

    def by_date(candles: list[dict]) -> dict[str, dict]:
        dates = [candle["timestamp"][:10] for candle in candles]
        if len(set(dates)) != len(dates):
            raise DataError(f"{symbol}: 일봉 날짜가 중복된다")
        return {date: candle for date, candle in zip(dates, candles) if date < today}

    raw_by_date, adjusted_by_date = by_date(raw), by_date(adjusted)
    if raw_by_date.keys() != adjusted_by_date.keys():
        raise DataError(f"{symbol}: 원가격과 수정 가격의 날짜가 다르다")
    days = []
    for date in sorted(raw_by_date):
        r, a = raw_by_date[date], adjusted_by_date[date]
        prices = [float(r["openPrice"]), float(r["closePrice"]), float(a["openPrice"]), float(a["closePrice"])]
        if min(prices) <= 0:
            raise DataError(f"{symbol} {date}: 가격이 양수가 아니다")
        days.append(Day(date, *prices))
    return days


def store(data: dict[str, list[Day]]) -> None:
    """daily_candles 표를 받은 일봉으로 통째로 바꾼다."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS daily_candles (symbol TEXT NOT NULL, date TEXT NOT NULL,"
            " open REAL NOT NULL, close REAL NOT NULL, adj_open REAL NOT NULL, adj_close REAL NOT NULL,"
            " PRIMARY KEY (symbol, date))"
        )
        db.execute("DELETE FROM daily_candles")
        db.executemany(
            "INSERT INTO daily_candles VALUES (?, ?, ?, ?, ?, ?)",
            [(symbol, *day) for symbol, days in data.items() for day in days],
        )


def tick_up_ratio(price: float, date: str) -> float:
    """원가격에서 호가 한 단계 위까지의 비율 s."""
    tick = 1 if date >= TICK_CHANGE and price < 2000 else 5
    return tick / price


def same_day_return(day: Day, inverse: bool) -> tuple[float, float]:
    """시가보다 호가 한 단계 위에 사서 종가 동시호가에 판 (비용 뺀 수익률, 비용 전 수익률). 원가격으로 계산한다."""
    g = day.close / (day.open * (1 + tick_up_ratio(day.open, day.date)))
    tax = TAX * max(0, g - 1) if inverse else 0
    return (g * (1 - C) - tax) / (1 + C) - 1, day.close / day.open - 1


def overnight(k200: list[Day]) -> list[Result]:
    """종가 베팅. t 종가에 사서 t+1 시가에 판 매매는 t+1의 결과다."""
    results = [Result(k200[0].date, None, None, None)]
    for prev, day in zip(k200, k200[1:]):
        g = day.adj_open / prev.adj_close
        results.append(Result(day.date, KODEX200, g * (1 - C) / (1 + C) - 1, g - 1))
    return results


def gap(k200: list[Day], i: int) -> float:
    return k200[i].adj_open / k200[i - 1].adj_close - 1


def threshold(k200: list[Day], i: int) -> float:
    """t를 뺀 직전 20거래일 |갭|의 평균."""
    return sum(abs(gap(k200, j)) for j in range(i - 20, i)) / 20


def gap_reversal(k200: list[Day], inverse: list[Day]) -> list[Result]:
    """갭 되돌림. 인버스 첫 거래일과 직전 20거래일 갭이 생기는 날 중 늦은 날부터 KODEX 200 거래일마다 판단한다."""
    inverse_by_date = {day.date: day for day in inverse}
    start = max(21, next(i for i, day in enumerate(k200) if day.date >= inverse[0].date))
    results = []
    for i in range(start, len(k200)):
        date, size = k200[i].date, threshold(k200, i)
        if gap(k200, i) >= size:
            symbol, day = INVERSE, inverse_by_date.get(date)
        elif gap(k200, i) <= -size:
            symbol, day = KODEX200, k200[i]
        else:
            symbol, day = None, None
        if day is None:
            results.append(Result(date, None, None, None))
        else:
            results.append(Result(date, symbol, *same_day_return(day, symbol == INVERSE)))
    return results


def metrics(results: list[Result]) -> dict:
    """5절 지표와 통과 판정. 자산은 매매가 끝난 날에만 바뀐다."""
    n = len(results)
    equity = gross = peak = 1.0
    mdd = 0.0
    curve, year_end = [], {}
    for result in results:
        if result.net is not None:
            equity *= 1 + result.net
            gross *= 1 + result.gross
        peak = max(peak, equity)
        mdd = max(mdd, 1 - equity / peak)
        curve.append(equity)
        year_end[result.date[:4]] = equity
    yearly, previous = {}, 1.0
    for year, end in year_end.items():
        yearly[year] = end / previous - 1
        previous = end
    half = curve[n // 2 - 1]
    trades = [result.net for result in results if result.net is not None]
    total, first, second = equity - 1, half - 1, equity / half - 1
    return {
        "days": n,
        "total": total,
        "annualized": equity ** (252 / n) - 1,
        "max_drawdown": mdd,
        "first_half": first,
        "second_half": second,
        "yearly": yearly,
        "trades": len(trades),
        "win_rate": sum(net > 0 for net in trades) / len(trades),
        "avg_trade": sum(trades) / len(trades),
        "gross_total": gross - 1,
        "passed": total > 0 and mdd < 0.2 and first > 0 and second > 0,
    }


def check() -> int:
    """일봉을 받아 저장하고 두 후보를 계산해 보고서를 만든다."""
    run_at = datetime.now(KST)
    try:
        client_id, client_secret = toss.read_credentials(ROOT)
        with httpx.Client(base_url=toss.BASE_URL, timeout=30) as http:
            token = toss.issue_token(http, client_id, client_secret)
            data = {
                symbol: merge(
                    symbol,
                    toss.candles(http, token, symbol, "1d", adjusted=False),
                    toss.candles(http, token, symbol, "1d", adjusted=True),
                    run_at.date().isoformat(),
                )
                for symbol in (KODEX200, INVERSE)
            }
    except (toss.TossError, DataError, httpx.HTTPError) as error:
        print(f"실패: {error}")
        return 1
    store(data)
    candidates = {
        "overnight": overnight(data[KODEX200]),
        "gap_reversal": gap_reversal(data[KODEX200], data[INVERSE]),
    }
    report = {
        "run_at": run_at.isoformat(timespec="seconds"),
        "data": {symbol: {"first": days[0].date, "last": days[-1].date, "days": len(days)} for symbol, days in data.items()},
        "candidates": {
            name: {"start": results[0].date, "end": results[-1].date, **metrics(results)}
            for name, results in candidates.items()
        },
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"v0-{run_at:%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_report(report)
    print(f"보고서: {path.relative_to(ROOT)}")
    return 0


def print_report(report: dict) -> None:
    """후보별 지표와 판정 표, 연도별 수익 표를 출력한다."""
    names, ms = list(report["candidates"]), list(report["candidates"].values())
    summary = [
        ["지표", *names],
        ["기간", *(f"{m['start']} ~ {m['end']}" for m in ms)],
        ["거래일 수", *(f"{m['days']:,}" for m in ms)],
        ["전체 수익", *(f"{m['total']:+.2%}" for m in ms)],
        ["연환산 수익", *(f"{m['annualized']:+.2%}" for m in ms)],
        ["최대 낙폭", *(f"{m['max_drawdown']:.2%}" for m in ms)],
        ["앞 기간 수익", *(f"{m['first_half']:+.2%}" for m in ms)],
        ["뒤 기간 수익", *(f"{m['second_half']:+.2%}" for m in ms)],
        ["거래 수", *(f"{m['trades']:,}" for m in ms)],
        ["승률", *(f"{m['win_rate']:.1%}" for m in ms)],
        ["거래당 평균 수익률", *(f"{m['avg_trade']:+.3%}" for m in ms)],
        ["비용 전 전체 수익", *(f"{m['gross_total']:+.2%}" for m in ms)],
        ["판정", *("통과" if m["passed"] else "불통과" for m in ms)],
    ]
    years = sorted({year for m in ms for year in m["yearly"]})
    yearly = [["연도", *names]] + [
        [year, *(f"{m['yearly'][year]:+.2%}" if year in m["yearly"] else "-" for m in ms)] for year in years
    ]
    for rows in (summary, yearly):
        widths = [max(_width(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print("  ".join(cell + " " * (w - _width(cell)) for cell, w in zip(row, widths)).rstrip())
        print()


def _width(text: str) -> int:
    """터미널에서 한글은 두 칸을 차지한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)

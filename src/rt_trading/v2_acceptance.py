"""v2 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing
from datetime import date as Date
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from rt_trading import v2_stage1

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "docs" / "specs" / "v2" / "fixtures"
STAGE1_CANDIDATES = ("turn_of_month", "trend_200", "overnight_trend")


def _matches(actual: object, expected: object) -> bool:
    """JSON 값이 같은지 본다. 소수는 1e-12 안의 차이만 허용한다."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(_matches(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(map(_matches, actual, expected))
    if isinstance(expected, float):
        return isinstance(actual, float) and abs(actual - expected) < 1e-12
    return type(actual) is type(expected) and actual == expected


# PRD 01 1단계 일봉 시험


def _prd01() -> dict:
    return json.loads((FIXTURES / "prd01-stage1.json").read_text(encoding="utf-8"))


@contextlib.contextmanager
def _temp_db() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "v0.sqlite3"


def _build(fixture: dict, path: Path, variant: dict | None = None) -> None:
    """시장 spec으로 v0와 같은 표 구조의 SQLite 파일을 만든다. variant는 확인 실패 사례의 변형이다."""
    variant = variant or {}
    market = fixture["market"]
    bars = {date: dict(symbols) for date, symbols in market["bars"].items()}
    for date, symbols in variant.get("bars", {}).items():
        bars.setdefault(date, {}).update(symbols)
    inverse_from = variant.get("inverse_from", market["inverse_from"])
    omit = {tuple(pair) for pair in variant.get("omit_rows", [])}
    days, day, holidays = [], Date.fromisoformat(market["start"]), set(market["holidays"])
    while len(days) < market["days"]:
        if day.weekday() < 5 and day.isoformat() not in holidays:
            days.append(day.isoformat())
        day += timedelta(days=1)
    rows = []
    for symbol, first in market["prices"].items():
        raw = adj = first  # 적지 않은 날은 전 거래일 원가격 종가와 수정 종가를 그날 시가·종가로 쓴다
        for date in days:
            if symbol == v2_stage1.INVERSE and date < inverse_from:
                continue
            bar = bars.get(date, {}).get(symbol) or [raw, raw, adj, adj]
            bar = bar if len(bar) == 4 else [*bar, *bar]
            if (symbol, date) not in omit:
                rows.append((symbol, date, *bar))
            raw, adj = bar[1], bar[3]
    names = ("symbol", "date", "open", "close", "adj_open", "adj_close")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(fixture["schemas"][variant.get("schema", "valid")])
        # 표 모양만 다른 사례도 행을 넣어, 표 모양 확인이 아니면 실패하지 않게 한다
        columns = [name for _, name, *_ in db.execute("PRAGMA table_info(daily_candles)")]
        if columns:  # 이름이 바뀐 열에는 정상 표의 같은 자리 값을 넣는다
            where = [names.index(column) if column in names else position for position, column in enumerate(columns)]
            sql = f"INSERT INTO daily_candles ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
            db.executemany(sql, [tuple(row[index] for index in where) for row in rows])


def _load(fixture: dict, variant: dict | None = None) -> v2_stage1.Data:
    with _temp_db() as db:
        _build(fixture, db, variant)
        return v2_stage1.load(db)


def case_stage1_data() -> None:
    fixture = _prd01()
    _load(fixture)  # 인버스 첫 거래일 전 KODEX 200 거래일이 정확히 200일이면 통과한다
    for variant in fixture["invalid"]:
        try:
            _load(fixture, variant)
        except v2_stage1.Stage1Error as error:
            assert error.code == "V2_STAGE1_DATA_INVALID", (variant["name"], str(error))
        else:
            raise AssertionError(f"{variant['name']}: 실패하지 않았다")


def case_turn_of_month_dates() -> None:
    fixture = _prd01()
    trades = v2_stage1.turn_of_month(_load(fixture)).trades
    assert [[trade.buy, trade.sell] for trade in trades] == fixture["turn_of_month"]["trades"], trades


def case_trend_selection() -> None:
    fixture = _prd01()
    data = _load(fixture)
    trends, expected = v2_stage1.trends(data), fixture["trends"]["values"]
    assert {date: trends[date] for date in expected} == expected, trends
    for candidate in ("trend_200", "overnight_trend"):
        trades = getattr(v2_stage1, candidate)(data).trades
        assert [[trade.symbol, trade.buy, trade.sell] for trade in trades] == fixture[candidate]["trades"], (candidate, trades)


def case_stage1_returns() -> None:
    fixture = _prd01()
    data = _load(fixture)
    for candidate in STAGE1_CANDIDATES:
        result, expected = getattr(v2_stage1, candidate)(data), fixture[candidate]
        assert result.closes[0][0] == expected["start"], (candidate, result.closes[0])
        closes = {date: float(equity) for date, equity in result.closes}
        assert _matches({date: closes[date] for date in expected["closes"]}, expected["closes"]), (candidate, closes)
        assert _matches([float(trade.net) for trade in result.trades], expected["nets"]), (candidate, result.trades)


def case_stage1_lookahead() -> None:
    fixture = _prd01()
    cut, data, changed = fixture["lookahead"]["date"], _load(fixture), _load(fixture, fixture["lookahead"])
    for candidate in STAGE1_CANDIDATES:
        before, after = getattr(v2_stage1, candidate)(data).closes, getattr(v2_stage1, candidate)(changed).closes
        assert [item for item in before if item[0] < cut] == [item for item in after if item[0] < cut], candidate
        assert before != after, f"{candidate}: 바꾼 미래 일봉이 결과에 닿지 않아 확인이 되지 않는다"


def case_stage1_metrics() -> None:
    fixture = _prd01()
    data = _load(fixture)
    for candidate, expected in fixture["metrics"].items():
        if candidate == "notes":
            continue
        summary = v2_stage1.summary(candidate, getattr(v2_stage1, candidate)(data))
        assert _matches({key: summary[key] for key in expected}, expected), (candidate, summary)
    exact = fixture["exact_drawdown"]
    closes = [(date, Decimal(equity)) for date, equity in exact["closes"]]
    criteria = v2_stage1.summary("trend_200", v2_stage1.Result(closes, []))["criteria"]
    assert criteria["drawdown_below_limit"] is exact["drawdown_below_limit"], criteria


CASES = {
    "01": [
        ("1단계 일봉 확인: 표·열·기본 키, 0 이하 가격, 인버스 상장 뒤 거래일, 인버스 전 200거래일(정확히 200일은 통과)", case_stage1_data),
        ("①의 사는 날·파는 날(휴장일이 낀 달력), 파는 날이 데이터 밖인 회차 없음", case_turn_of_month_dates),
        ("②·④의 추세(평균과 같으면 아래)와 종목 선택, ②는 목표 종목을 들고 있으면 그대로", case_trend_selection),
        ("매매 수익률, 거래일마다의 종가 자산, 갈아타기, 같은 날 매도 뒤 매수, 마지막 날 청산, 첫 판단일", case_stage1_returns),
        ("미래 일봉을 바꿔도 그 전 거래일까지의 자산이 같다", case_stage1_lookahead),
        ("최종 자산, 최대 낙폭(정확히 20%는 불통과), 앞뒤 기간 수익, 비용 전 총수익과 단계 판정", case_stage1_metrics),
    ],
}


def run(prd: str | None = None) -> int:
    results = []
    for prd_id, cases in CASES.items():
        if prd and prd != prd_id:
            continue
        for name, case in cases:
            try:
                case()
                results.append({"prd": prd_id, "case": name, "passed": True})
            except Exception as error:
                results.append({"prd": prd_id, "case": name, "passed": False, "error": f"{type(error).__name__}: {error}"})
    passed = sum(result["passed"] for result in results)
    summary = {"total": len(results), "passed": passed, "failed": len(results) - passed}
    print(json.dumps({"results": results, "summary": summary}, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1

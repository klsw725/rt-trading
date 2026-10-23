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

from rt_trading import engine, fixtures, market_data, sessions, v2_stage1, v2_strategies

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


# PRD 02 엔진 며칠 보유


def _prd02() -> dict:
    return json.loads((FIXTURES / "prd02-engine.json").read_text(encoding="utf-8"))


def _market(spec: dict) -> market_data.MarketData:
    with _temp_db() as db:
        fixtures.build(spec, db)
        data, violations = market_data.load(db)
    assert data, market_data.describe(violations)
    return data


def _check_view(data: market_data.MarketData, view: engine.View) -> None:
    assert view.calendar == data.days and view.defect_days == data.defects, "달력이나 결함일 표가 다르다"
    seen = data.days[: data.days.index(view.date)]
    assert view.adj_closes == [data.daily[engine.KODEX200][day][1] for day in seen], "전 거래일까지의 수정 종가가 아니다"


def _run_orders(data: market_data.MarketData, orders: list[list], start: int, overnight: bool) -> engine.Records:
    """fixture의 시험용 주문을 그 시각에 내는 전략으로 엔진을 돌린다. 전략이 받은 값도 확인한다."""
    planned = {stamp: engine.Order(kind, symbol, reason) for stamp, kind, symbol, reason in orders}

    def strategy(view: engine.View) -> engine.Order | None:
        _check_view(data, view)
        return planned.get(f"{view.date} {view.time}")

    return engine.run(data, strategy, start, overnight)


def _check_records(records: engine.Records, expected: dict) -> None:
    for key in ("fills", "trades", "limits", "distributions"):
        if key in expected:
            actual = [list(item) for item in getattr(records, key)]
            assert actual == expected[key], (key, actual)
    assert records.closes == expected["closes"], records.closes
    equity = dict(records.equity)
    assert {stamp: equity[stamp] for stamp in expected["equity"]} == expected["equity"]


def _shift_after(data: market_data.MarketData, cut: str) -> market_data.MarketData:
    """cut 뒤 봉의 가격을 모두 100원 올린 데이터."""
    bars = {
        symbol: {
            date: [
                bar._replace(open=bar.open + 100, high=bar.high + 100, low=bar.low + 100, close=bar.close + 100)
                if f"{date} {bar.time}" > cut
                else bar
                for bar in bars
            ]
            for date, bars in days.items()
        }
        for symbol, days in data.bars.items()
    }
    return data._replace(bars=bars)


def case_engine_overnight() -> None:
    case = _prd02()["overnight_case"]
    records = _run_orders(_market(case["market"]), case["orders"], case["start"], case["overnight"])
    _check_records(records, case["expected"])


def case_engine_overnight_limits() -> None:
    case = _prd02()["limits_case"]
    records = _run_orders(_market(case["market"]), case["orders"], case["start"], case["overnight"])
    _check_records(records, case["expected"])


def case_engine_defect() -> None:
    case = _prd02()["defect_case"]
    run = case["runs"]["defect"]
    _check_records(_run_orders(_market(case["market"]), run["orders"], case["start"], run["overnight"]), run["expected"])


def case_engine_view() -> None:
    fixture = _prd02()
    case, view = fixture["overnight_case"], fixture["view_case"]
    data = _market(case["market"])
    changed = data._replace(
        daily={
            symbol: {
                day: (close, view["changed_adj_close"] if symbol == engine.KODEX200 and day >= view["cut"] else adj)
                for day, (close, adj) in days.items()
            }
            for symbol, days in data.daily.items()
        }
    )

    def fills(market: market_data.MarketData) -> list[engine.Fill]:
        def strategy(seen: engine.View) -> engine.Order | None:
            _check_view(market, seen)
            if seen.time != "15:20":
                return None
            if seen.holding:
                return engine.Order("sell", None, "signal")
            return engine.Order("buy", engine.KODEX200, "signal") if seen.adj_closes[-1] >= view["threshold"] else None

        return engine.run(market, strategy, case["start"], True).fills

    before, after = fills(data), fills(changed)
    assert [fill for fill in before if fill.time[:10] <= view["cut"]] == [fill for fill in after if fill.time[:10] <= view["cut"]]
    assert before != after, "바꾼 수정 종가가 주문에 닿지 않아 확인이 되지 않는다"


def case_engine_start_last_day() -> None:
    fixture = _prd02()
    case = fixture["overnight_case"]
    data = _market(case["market"])
    records = _run_orders(data, case["orders"], case["start"], case["overnight"])
    assert records.equity[0] == ("", engine.INITIAL_CASH) and records.equity[1][0].startswith(data.days[case["start"]]), records.equity[:2]
    assert list(records.closes) == data.days[case["start"] :], list(records.closes)
    cut = case["lookahead_cut"]
    shifted = _run_orders(_shift_after(data, cut), case["orders"], case["start"], case["overnight"])
    for key in ("fills", "equity"):
        old, new = getattr(records, key), getattr(shifted, key)
        assert [item for item in old if item[0] <= cut] == [item for item in new if item[0] <= cut], key
        assert old != new, "바꾼 미래 봉이 결과에 닿지 않아 확인이 되지 않는다"
    defect = fixture["defect_case"]
    market = _market(defect["market"])
    for name in ("last_day", "final_entry", "carried_buy"):
        run = defect["runs"][name]
        _check_records(_run_orders(market, run["orders"], defect["start"], run["overnight"]), run["expected"])


def case_engine_intraday() -> None:
    case = _prd02()["defect_case"]
    run = case["runs"]["intraday"]
    _check_records(_run_orders(_market(case["market"]), run["orders"], case["start"], run["overnight"]), run["expected"])


# PRD 03 2단계 후보 전략


def _prd03() -> dict:
    return json.loads((FIXTURES / "prd03-strategies.json").read_text(encoding="utf-8"))


def _given_view(fixture: dict, check: dict) -> engine.View:
    """①②④ 사례의 View. 그 시각 KODEX 200 봉 하나와 사례에 적은 달력·결함일·보유·수정 종가를 넣는다."""
    date, time = check["date"], check["time"]
    bar = market_data.Bar(time, 30000, 30000, 30000, 30000, 100)
    holding = check.get("holding") and engine.Holding(check["holding"][0], 1, 0, 0, check["holding"][1])
    adj_closes = [value for value, count in fixture["adj_closes"][check.get("adj", "up")] for _ in range(count)]
    return engine.View(
        date, time, sessions.session(date, sessions.exceptions()), {engine.KODEX200: 30000, engine.INVERSE: 5000},
        check.get("defect", False), {engine.KODEX200: [bar], engine.INVERSE: [bar]}, {}, [], holding, False,
        fixture["calendars"][check.get("calendar", "sep_oct")], check.get("defect_days", []), adj_closes,
    )


def _market_view(data: market_data.MarketData, check: dict) -> engine.View:
    """엔진이 그 분에 넘기는 것과 같은 View. drop에 적은 시각의 KODEX 200 봉은 뺀다."""
    date, time, drop = check["date"], check["time"], set(check.get("drop", []))
    today = {
        symbol: [bar for bar in days[date] if bar.time <= time and not (symbol == engine.KODEX200 and bar.time in drop)]
        for symbol, days in data.bars.items()
    }
    past_days = data.days[: data.days.index(date)]
    past = {symbol: {day: days[day] for day in past_days} for symbol, days in data.bars.items()}
    base = {symbol: data.base[symbol][date] for symbol in data.bars}
    adj_closes = [data.daily[engine.KODEX200][day][1] for day in past_days]
    return engine.View(
        date, time, data.sessions[date], base, date in data.defects, today, past, past_days, None, False, data.days, data.defects, adj_closes
    )


def _strategy_checks(group: str) -> None:
    fixture = _prd03()
    for check in (check for check in fixture["checks"] if check["group"] == group):
        order = v2_strategies.CANDIDATES[check["candidate"]]()(_given_view(fixture, check))
        assert (order and list(order)) == check["expected"], (check["name"], order)
    market = fixture["last_half_hour"]
    checks = [check for check in market["checks"] if check["group"] == group]
    data = _market(market["market"]) if checks else None
    for check in checks:
        order = v2_strategies.CANDIDATES["last_half_hour"]()(_market_view(data, check))
        assert (order and list(order)) == check["expected"], (check["name"], order)


def case_strategy_turn_of_month() -> None:
    _strategy_checks("turn_of_month")


def case_strategy_trend() -> None:
    _strategy_checks("trend")


def case_strategy_times() -> None:
    _strategy_checks("times")


def case_strategy_last_half_hour() -> None:
    _strategy_checks("last_half_hour")


def case_strategy_defect() -> None:
    _strategy_checks("defect")


CASES = {
    "01": [
        ("1단계 일봉 확인: 표·열·기본 키, 0 이하 가격, 인버스 상장 뒤 거래일, 인버스 전 200거래일(정확히 200일은 통과)", case_stage1_data),
        ("①의 사는 날·파는 날(휴장일이 낀 달력), 파는 날이 데이터 밖인 회차 없음", case_turn_of_month_dates),
        ("②·④의 추세(평균과 같으면 아래)와 종목 선택, ②는 목표 종목을 들고 있으면 그대로", case_trend_selection),
        ("매매 수익률, 거래일마다의 종가 자산, 갈아타기, 같은 날 매도 뒤 매수, 마지막 날 청산, 첫 판단일", case_stage1_returns),
        ("미래 일봉을 바꿔도 그 전 거래일까지의 자산이 같다", case_stage1_lookahead),
        ("최종 자산, 최대 낙폭(정확히 20%는 불통과), 앞뒤 기간 수익, 비용 전 총수익과 단계 판정", case_stage1_metrics),
    ],
    "02": [
        (
            "며칠 보유: 접수 구간 직전 봉 매수·갈아타기의 종가 동시호가 체결, 보유·대기 주문 넘기기, 종가 분 매도의 다음 날 시가 체결,"
            " 분배금, 동시호가·연속 매매 체결가, 수수료, 인버스 세금, 정수 주",
            case_engine_overnight,
        ),
        ("며칠 보유 중 손실 한도: 첫 봉·종가 분 발동의 청산, 기준과 정확히 같은 자산, 진입 막힘과 풀림, 누적 뒤 현금", case_engine_overnight_limits),
        ("결함일: 전 거래일 주문 체결, 보유 넘기기, 봉이 없는 보유의 평가, 종가 동시호가 봉이 없는 날의 마감 자산", case_engine_defect),
        ("전략에 보이는 값: 달력·결함일 표·전 거래일까지의 수정 종가, 수정 종가를 바꿔도 그 전 주문이 같다", case_engine_view),
        ("첫 판단일, 마지막 거래일의 매수 없음과 매도 사유, 미래 봉을 바꿔도 그 전 체결과 자산열이 같다", case_engine_start_last_day),
        ("③: v1처럼 그날 종가 동시호가에 사유 close로 팔고 보유를 남기지 않는다", case_engine_intraday),
    ],
    "03": [
        ("①: 사는 날·파는 날(휴장일이 낀 달력), 파는 날이 결함일이거나 데이터 밖인 회차와 결함일인 사는 날은 사지 않음", case_strategy_turn_of_month),
        ("②·④: 전 거래일 추세(평균과 같으면 아래, 마지막 200개), ②의 갈아타기와 그대로 두기, ④의 종가 동시호가 분 매도", case_strategy_trend),
        ("며칠 보유 후보의 주문 시각: 마감 10분 전 봉(정상일·연초 15:20, 수능일 16:20), ④의 수능일 종가 동시호가 분", case_strategy_times),
        ("③: 개장 30분 뒤 종가가 기준가보다 클 때만 마감 30분 전에 사고, 장 유형별 시각과 세 건너뛰기", case_strategy_last_half_hour),
        ("결함일에는 주문하지 않는다", case_strategy_defect),
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

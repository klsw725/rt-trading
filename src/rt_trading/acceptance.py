"""v1 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import copy
import io
import json
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import httpx

from rt_trading import cli, engine, fixtures, market_data, sessions, store, toss

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "docs" / "specs" / "v1" / "fixtures"
TABLES = ("candles_1m", "candles_1d", "fetch_runs")


# PRD 01 토스 시세 수집


def _prd01() -> dict:
    return json.loads((FIXTURES / "prd01-toss.json").read_text(encoding="utf-8"))


def _key(request: httpx.Request) -> str:
    """요청 종류. 토큰 발급은 token, 캔들 조회는 '종목 주기 수정가격여부'다."""
    if request.url.path == "/oauth2/token":
        return "token"
    params = request.url.params
    return f"{params['symbol']} {params['interval']} {params['adjusted']}"


def _errors(fixture: dict, name: str, times: int = 1) -> list[httpx.Response]:
    error = fixture["errors"][name]
    return [httpx.Response(error["status"], json=error["body"], headers=error.get("headers")) for _ in range(times)]


class Run(NamedTuple):
    code: int
    output: str
    requests: list[httpx.Request]
    waits: list[float]  # 1초 이상 기다린 시간. 호출 간격(1/15초)은 뺀다


def _fetch(fixture: dict, db: Path, queue: dict[str, list] | None = None) -> Run:
    """fixture 응답을 돌려주는 가짜 토스로 fetch를 돌린다. queue에 넣은 응답이나 예외는 그 요청에 먼저 돌려준다."""
    queue = queue or {}
    requests, waits = [], []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        key = _key(request)
        if queue.get(key):
            item = queue[key].pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if key == "token":
            return httpx.Response(200, json=fixture["token"])
        pages = fixture["candles"][key]
        before = request.url.params.get("before")
        index = 0 if before is None else 1 + [page["result"]["nextBefore"] for page in pages].index(before)
        return httpx.Response(200, json=pages[index])

    now = datetime.fromisoformat(fixture["now"])
    output = io.StringIO()
    original, toss._sleep = toss._sleep, waits.append
    try:
        with (
            httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(handle)) as http,
            contextlib.redirect_stdout(output),
        ):
            code = cli.run_fetch(http, tuple(fixture["credentials"]), db, lambda: now)
    finally:
        toss._sleep = original
    return Run(code, output.getvalue(), requests, [wait for wait in waits if wait >= 1])


@contextlib.contextmanager
def _temp_db() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "market.sqlite3"


def _rows(db: Path, table: str) -> list[list]:
    return [list(row) for row in store.read(db, table)]


def case_pagination() -> None:
    fixture = _prd01()
    with _temp_db() as db:
        run = _fetch(fixture, db)
        assert run.code == 0, run.output
        pages = fixture["candles"]["069500 1m false"]
        befores = [r.url.params.get("before") for r in run.requests if _key(r) == "069500 1m false"]
        assert befores == [None, pages[0]["result"]["nextBefore"], pages[1]["result"]["nextBefore"]], befores
        assert _rows(db, "candles_1m") == fixture["expected"]["candles_1m"]


def case_bar_time() -> None:
    with _temp_db() as db:
        _fetch(_prd01(), db)
        times = [row[1] for row in _rows(db, "candles_1m") if row[0] == "069500"]
    # 2026-10-21T09:01:00.000+09:00 봉(09:00:00~09:00:59 체결)은 끝나는 시각 그대로 09:01로 적는다
    assert times == ["2022-11-23 09:01", "2026-10-20 15:30", "2026-10-21 09:01", "2026-10-21 15:30"], times


def case_requests() -> None:
    with _temp_db() as db:
        run = _fetch(_prd01(), db)
    candle_requests = [r for r in run.requests if _key(r) != "token"]
    assert {_key(r) for r in candle_requests} == {
        "069500 1m false",
        "069500 1d false",
        "069500 1d true",
        "114800 1m false",
        "114800 1d false",
        "114800 1d true",
    }
    assert all(r.url.params["count"] == "200" for r in candle_requests)


def case_daily_mismatch() -> None:
    for broken in ("날짜 불일치", "날짜 중복"):
        fixture = _prd01()
        if broken == "날짜 불일치":
            del fixture["candles"]["069500 1d true"][0]["result"]["candles"][2]  # 수정 가격에서 2026-10-20을 뺀다
        else:
            raw = fixture["candles"]["069500 1d false"][0]["result"]["candles"]
            raw.insert(1, dict(raw[1]))  # 원가격에 2026-10-21을 한 번 더 넣는다
        with _temp_db() as db:
            run = _fetch(fixture, db)
            assert run.code == 1 and run.output.startswith("V1_TOSS_RESPONSE_INVALID"), (broken, run.output)
            assert not db.exists(), broken


def case_token_revoked() -> None:
    fixture = _prd01()
    with _temp_db() as db:
        run = _fetch(fixture, db, {"069500 1d false": _errors(fixture, "token_revoked")})
    assert run.code == 1 and run.output.startswith("V1_TOKEN_REVOKED"), run.output
    assert [_key(r) for r in run.requests].count("token") == 1


def case_token_ip() -> None:
    fixture = _prd01()
    with _temp_db() as db:
        run = _fetch(fixture, db, {"token": _errors(fixture, "ip_oauth2")})
    assert run.code == 1 and run.output.startswith("V1_IP_NOT_ALLOWED"), run.output
    assert "허용 IP" in run.output and "requestId" not in run.output, run.output


def case_candle_errors() -> None:
    fixture = _prd01()
    scenarios = [
        ("그 밖의 401", {"069500 1m false": _errors(fixture, "token_invalid")}, "V1_TOKEN_INVALID", []),
        ("403", {"114800 1d true": _errors(fixture, "ip_candles")}, "V1_IP_NOT_ALLOWED", []),
        ("429", {"069500 1m false": _errors(fixture, "rate_limited")}, None, [3]),
        ("5xx 한 번", {"069500 1m false": _errors(fixture, "unavailable")}, None, [1]),
        ("5xx 네 번", {"069500 1m false": _errors(fixture, "unavailable", 4)}, "V1_TOSS_UNAVAILABLE", [1, 2, 4]),
        (
            "네트워크 오류 네 번",
            {"069500 1m false": [httpx.ConnectError("연결 실패") for _ in range(4)]},
            "V1_TOSS_UNAVAILABLE",
            [1, 2, 4],
        ),
        ("result 없음", {"069500 1m false": _errors(fixture, "no_result")}, "V1_TOSS_RESPONSE_INVALID", []),
        ("정수가 아닌 가격", {"114800 1m false": _errors(fixture, "bad_price")}, "V1_TOSS_RESPONSE_INVALID", []),
    ]
    for name, queue, code, waits in scenarios:
        with _temp_db() as db:
            run = _fetch(fixture, db, queue)
            if code is None:
                assert run.code == 0, (name, run.output)
                assert _rows(db, "candles_1m") == fixture["expected"]["candles_1m"], name
            else:
                assert run.code == 1 and run.output.startswith(code), (name, run.output)
            assert run.waits == waits, (name, run.waits)


def case_storage() -> None:
    fixture = _prd01()
    expected = fixture["expected"]
    with _temp_db() as db:
        assert _fetch(fixture, db).code == 0
        for table in TABLES:
            assert _rows(db, table) == expected[table], table
        with closing(sqlite3.connect(db)) as conn:
            for table, columns in expected["schema"].items():
                info = [[name, kind, notnull, pk] for _, name, kind, notnull, _, pk in conn.execute(f"PRAGMA table_info({table})")]
                assert info == columns, (table, info)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def case_rollback() -> None:
    fixture = _prd01()
    with _temp_db() as db:
        assert _fetch(fixture, db).code == 0
        before = {table: _rows(db, table) for table in TABLES}
        # 두 번째 종목 수집이 실패한다
        assert _fetch(fixture, db, {"114800 1m false": _errors(fixture, "ip_candles")}).code == 1
        assert {table: _rows(db, table) for table in TABLES} == before
        # 기존 봉을 지운 뒤 새 봉을 넣다가 기본 키 중복으로 실패한다
        minute = [tuple(row) for row in fixture["expected"]["candles_1m"]]
        try:
            store.replace(db, minute + minute[:1], [], [])
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("기본 키 중복이 실패하지 않았다")
        assert {table: _rows(db, table) for table in TABLES} == before


def case_no_secrets() -> None:
    fixture = _prd01()
    secrets = [*fixture["credentials"], fixture["token"]["access_token"]]
    with _temp_db() as db:
        texts = [_fetch(fixture, db).output, db.read_bytes().decode("utf-8", "replace")]
        for queue in (
            {"069500 1m false": _errors(fixture, "token_revoked")},
            {"token": _errors(fixture, "ip_oauth2")},
            {"069500 1m false": _errors(fixture, "unavailable", 4)},
            {"114800 1m false": _errors(fixture, "bad_price")},
        ):
            texts.append(_fetch(fixture, db, queue).output)
    leaked = [secret for secret in secrets if any(secret in text for text in texts)]
    assert not leaked, leaked


def case_gitignore() -> None:
    paths = ["data/market.sqlite3", "data/reports/report.json"]
    done = subprocess.run(["git", "check-ignore", *paths], cwd=ROOT, capture_output=True, text=True)
    assert done.stdout.split() == paths, done.stdout


# PRD 02 장 일정, 데이터 검증, 기준가


def _prd02() -> dict:
    return json.loads((FIXTURES / "prd02-data.json").read_text(encoding="utf-8"))


def _patched(spec: dict, patches: list[dict]) -> dict:
    """spec의 날짜마다 patches의 bars·ratio를 합치고 omit을 더하고 volume을 바꾼다."""
    spec = copy.deepcopy(spec)
    days = {day["date"]: day for day in spec["days"]}
    for patch in patches:
        day = days[patch["date"]]
        for symbol, bars in patch.get("bars", {}).items():
            day.setdefault("bars", {}).setdefault(symbol, {}).update(bars)
        day.setdefault("ratio", {}).update(patch.get("ratio", {}))
        day.setdefault("omit", []).extend(patch.get("omit", []))
        if "volume" in patch:
            day["volume"] = patch["volume"]
    return spec


def _load(spec: dict) -> tuple[market_data.MarketData | None, list[market_data.Violation]]:
    with _temp_db() as db:
        fixtures.build(spec, db)
        return market_data.load(db)


def case_sessions() -> None:
    table = sessions.exceptions()
    for date, expected in _prd02()["sessions"].items():
        session = sessions.session(date, table)
        actual = {"count": len(session.bars), "first": session.bars[0], "last": session.bars[-1], "window": session.window}
        assert actual == expected, (date, actual)


def case_valid_data() -> None:
    fixture = _prd02()
    expected = fixture["expected"]
    data, violations = _load(fixture["valid"])
    assert data and not violations, market_data.describe(violations)
    assert data.days == expected["days"], data.days
    for symbol in market_data.SYMBOLS:
        assert {date: len(bars) for date, bars in data.bars[symbol].items()} == expected["bar_counts"], symbol
        assert data.base[symbol] == expected["base"][symbol], (symbol, data.base[symbol])
    for symbol, days in expected["open_bar"].items():
        for date, opening in days.items():
            bar = market_data.open_bar(data.bars[symbol][date])
            assert [bar.time, bar.open] == opening, (symbol, date, bar)
    assert data.defects == expected["defects"], data.defects


def case_violations() -> None:
    fixture = _prd02()
    for case in fixture["violations"]:
        data, violations = _load(_patched(fixture["valid"], case["patches"]))
        actual = sorted({(v.code, v.date, v.symbol) for v in violations})
        expected = sorted(tuple(item) for item in case["expected"])
        assert actual == expected, (case["name"], actual)
        assert (data is None) == bool(expected), case["name"]
        text = market_data.describe(violations)
        assert all(date in text for _, date, _ in expected), (case["name"], text)


def case_daily_malformed() -> None:
    with _temp_db() as db:
        fixtures.build(_prd02()["valid"], db)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE candles_1d SET high = close - 1 WHERE symbol = '114800' AND date = '2024-01-04'")
            conn.execute("UPDATE candles_1d SET volume = -5 WHERE symbol = '069500' AND date = '2025-11-13'")
        data, violations = market_data.load(db)
    actual = sorted((v.code, v.date, v.symbol) for v in violations)
    expected = [("V1_BAR_MALFORMED", "2024-01-04", "114800"), ("V1_BAR_MALFORMED", "2025-11-13", "069500")]
    assert data is None and actual == expected, actual


def case_data_missing() -> None:
    fixture = _prd02()
    with _temp_db() as db:
        assert [v.code for v in market_data.load(db)[1]] == ["V1_DATA_MISSING"]  # DB 파일이 없다
    no_minute = [{"date": date, "omit": [["114800", "1m"]]} for date in fixture["expected"]["days"]]
    _, violations = _load(_patched(fixture["valid"], no_minute))
    assert sorted((v.code, v.symbol) for v in violations) == [("V1_DATA_MISSING", ""), ("V1_DATA_MISSING", "114800")]
    with _temp_db() as db:
        fixtures.build(fixture["valid"], db)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("DELETE FROM fetch_runs WHERE symbol = '069500' AND interval = '1d'")
        _, violations = market_data.load(db)
    assert [(v.code, v.symbol) for v in violations] == [("V1_DATA_MISSING", "")], violations


def case_base_price() -> None:
    for item in _prd02()["base_price"]:
        price, problem = market_data.base_price(*item["args"])
        assert [price, bool(problem)] == item["expected"], (item["why"], price, problem)


# PRD 03 시뮬레이션 엔진


def _prd03() -> dict:
    return json.loads((FIXTURES / "prd03-engine.json").read_text(encoding="utf-8"))


def _engine_data(fixture: dict, case: dict) -> market_data.MarketData:
    data, violations = _load(_patched(fixture["market"], case["patches"]))
    assert data, market_data.describe(violations)
    return data


def _run_scripted(data: market_data.MarketData, orders: list[list]) -> tuple[engine.Records, list[str]]:
    """fixture의 시험용 주문 순서대로 주문을 내는 전략으로 엔진을 돌린다. 전략이 불린 시각도 돌려준다."""
    planned = {stamp: engine.Order(kind, symbol, reason) for stamp, kind, symbol, reason in orders}
    calls = []

    def strategy(view: engine.View) -> engine.Order | None:
        assert all(bar.time <= view.time for bars in view.today.values() for bar in bars), "아직 끝나지 않은 봉이 보인다"
        assert all(day < view.date for day in view.past_days), "지난 거래일이 아닌 날이 보인다"
        calls.append(f"{view.date} {view.time}")
        return planned.get(calls[-1])

    return engine.run(data, strategy), calls


def _check_records(records: engine.Records, expected: dict) -> None:
    for key in ("fills", "trades", "discarded", "limits"):
        actual = [list(item) for item in getattr(records, key)]
        assert actual == expected[key], (key, actual)
    assert records.closes == expected["closes"], records.closes
    equity = dict(records.equity)
    assert {stamp: equity[stamp] for stamp in expected["equity"]} == expected["equity"]


def case_engine_fills() -> None:
    fixture = _prd03()
    case = fixture["fills_case"]
    records, _ = _run_scripted(_engine_data(fixture, case), case["orders"])
    _check_records(records, case["expected"])
    ticks = fixture["ticks_case"]
    records, _ = _run_scripted(_engine_data(fixture, ticks), ticks["orders"])
    assert [list(fill) for fill in records.fills] == ticks["expected"]["fills"], records.fills


def case_engine_lookahead() -> None:
    fixture = _prd03()
    case = fixture["fills_case"]
    data, cut = _engine_data(fixture, case), "2023-12-14 10:30"
    changed = {
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
    before, _ = _run_scripted(data, case["orders"])
    after, _ = _run_scripted(data._replace(bars=changed), case["orders"])
    for key in ("fills", "equity"):
        old, new = getattr(before, key), getattr(after, key)
        assert [item for item in old if item[0] <= cut] == [item for item in new if item[0] <= cut], key
        assert old != new, "바꾼 미래 봉이 결과에 닿지 않아 확인이 되지 않는다"


def case_engine_minutes() -> None:
    fixture = _prd03()
    case = fixture["fills_case"]
    data = _engine_data(fixture, case)
    records, calls = _run_scripted(data, case["orders"])
    minutes = case["expected"]["minutes"]
    assert len(calls) == len(set(calls)) == minutes and len(records.equity) == minutes + 1, (len(calls), len(records.equity))
    swapped = data._replace(bars=dict(reversed(data.bars.items())), base=dict(reversed(data.base.items())))
    assert _run_scripted(swapped, case["orders"])[0] == records


def case_engine_limits() -> None:
    fixture = _prd03()
    case = fixture["limits_case"]
    records, _ = _run_scripted(_engine_data(fixture, case), case["orders"])
    _check_records(records, case["expected"])
    after = case["expected"]["cash_after_cumulative"]
    assert {value for stamp, value in records.equity if stamp >= after["from"]} == {after["equity"]}


CASES = {
    "01": [
        ("페이지네이션: 세 페이지, 2022-11-23 전 봉과 당일 봉 버림, 오름차순 저장", case_pagination),
        ("봉 시각: 응답 시각을 봉이 끝나는 시각으로 저장", case_bar_time),
        ("요청: 1분봉 원가격, 일봉 원가격과 수정 가격", case_requests),
        ("일봉 날짜 불일치·중복은 저장 없이 V1_TOSS_RESPONSE_INVALID", case_daily_mismatch),
        ("401 token-revoked: 토큰 발급 한 번, V1_TOKEN_REVOKED", case_token_revoked),
        ("토큰 발급의 OAuth2 403: V1_IP_NOT_ALLOWED", case_token_ip),
        ("캔들 조회의 401·403·429·5xx·네트워크·형식 오류", case_candle_errors),
        ("저장: 봉 네 계열, fetch_runs 네 행, 표 형식, user_version", case_storage),
        ("실패하면 기존 내용 유지", case_rollback),
        ("출력과 DB에 인증 정보와 토큰 없음", case_no_secrets),
        ("data/는 git에서 제외", case_gitignore),
    ],
    "02": [
        ("장 일정: 정상일 390개, 연초 첫 거래일 330개, 수능일 390개와 종가 동시호가·접수 구간", case_sessions),
        ("검증 통과: 장 밖 빈 봉 버림, 늦은 시가 봉, 결함일 목록, 준비일 뺀 기준가", case_valid_data),
        ("코드마다 어긴 사례가 그 코드로 실패하고 어긴 날짜를 모두 출력", case_violations),
        ("일봉 OHLC 불변식 위반·음수 거래량은 V1_BAR_MALFORMED", case_daily_malformed),
        ("V1_DATA_MISSING: DB 파일, 종목별 행, 가장 늦은 수집 기록", case_data_missing),
        ("기준가: 비율 공식, 0.5원 올림, 2원 이내 무변화, 분배락일, 상승·3% 초과 하락 실패", case_base_price),
    ],
    "03": [
        (
            "체결과 비용: 다음 체결 가능한 봉, 거래량 0 봉, 호가 단위·2,000원 경계, 동시호가 체결가, 수수료, 인버스 세금,"
            " 종목 전환, 정수 주, 대기 주문, 마감 매도, 종가 동시호가까지 밀린 매수 취소",
            case_engine_fills,
        ),
        ("미래 봉을 바꿔도 그 전 체결과 자산열이 같다", case_engine_lookahead),
        ("같은 시각 두 종목 봉은 한 분: 전략 호출은 분마다 한 번, 종목 순서를 바꿔도 같은 결과", case_engine_minutes),
        (
            "손실 한도: 첫날·첫 주 기준 자산, 처리 순서, 청산 우선과 진입 취소, 동시 발동 순서, 한 번 세기,"
            " 다음 날·다음 주 해제, 누적 한도 뒤 현금, 한도 청산의 종가 동시호가 체결",
            case_engine_limits,
        ),
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

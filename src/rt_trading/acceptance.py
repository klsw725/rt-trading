"""v1 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
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

from rt_trading import cli, store, toss

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

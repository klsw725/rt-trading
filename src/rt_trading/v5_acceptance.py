"""v5 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import io
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx

from rt_trading import cli, toss, toss_account, v4_daily
from rt_trading.toss import KST

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "docs" / "specs" / "v5" / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=KST)


# PRD 01 토스 토큰·계좌·주문


class FakeToss:
    """미리 적은 응답을 돌려주는 가짜 토스. 요청을 (방법, 경로, 쿼리, 헤더, 본문)으로 기록한다.

    routes는 (방법, 경로) → 응답 목록이다. 응답은 (상태, 본문[, 헤더]), "network"(연결 오류), 또는 요청을 받아 응답을 돌려주는 함수이고, 마지막 응답을 되풀이한다.
    """

    def __init__(self, routes: dict, issued: str | None = None, expires_in: int = 86400) -> None:
        self.routes, self.issued, self.expires_in = routes, issued, expires_in
        self.requests: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        with self.lock:
            self.requests.append(
                {"method": request.method, "path": request.url.path, "params": dict(request.url.params), "headers": dict(request.headers), "body": body}
            )
        if request.url.path == "/oauth2/token":
            time.sleep(0.2)  # 두 쪽이 동시에 받으려 할 때 앞쪽이 잠금을 쥐고 있게 한다
            return httpx.Response(200, json={"access_token": self.issued, "token_type": "Bearer", "expires_in": self.expires_in})
        responses = self.routes[(request.method, request.url.path)]
        with self.lock:
            response = responses.pop(0) if len(responses) > 1 else responses[0]
        if callable(response):
            response = response(request)
        if response == "network":
            raise httpx.ConnectError("가짜 연결 오류", request=request)
        status, content, *headers = response
        return httpx.Response(status, json=content, headers=headers[0] if headers else None)

    def count(self, path: str) -> int:
        return sum(request["path"] == path for request in self.requests)


def _token_file(path: Path, token: str, expires_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": token, "expires_at": expires_at.isoformat()}), encoding="utf-8")


def _candle(at: str) -> dict:
    return {"timestamp": at, "openPrice": "100", "highPrice": "110", "lowPrice": "90", "closePrice": "105", "volume": "10"}


def _no_secrets(fixture: dict, outputs: list[str], root: Path) -> None:
    """가짜 인증 정보·계좌번호는 출력과 저장 파일 어디에도, 토큰 값은 token.json 말고는 없다. 사례가 넣어 둔 .env는 보지 않는다."""
    secrets = [*fixture["credentials"].values(), fixture["account_no"]]
    tokens = list(fixture["tokens"].values())
    for text in outputs:
        for secret in secrets + tokens:
            assert secret not in text, ("출력", secret)
    for path in root.rglob("*"):
        if path.is_file() and path.name != ".env":
            text = path.read_text(encoding="utf-8", errors="replace")
            for secret in secrets + ([] if path.name == "token.json" else tokens):
                assert secret not in text, (str(path), secret)


def case_client_requests() -> None:
    fixture, outputs = _fixture("prd01-toss.json"), []
    token = fixture["tokens"]["stored"]
    with tempfile.TemporaryDirectory() as name, mock.patch.object(toss, "_sleep", lambda seconds: None):
        for item in fixture["client"]:
            method, path, params, account, body = item["request"]
            fake = FakeToss({(method, path): [[200, item["response"]]]})
            with httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(fake)) as http:
                result = getattr(toss_account, item["call"][0])(http, token, *item["call"][1:])
            (sent,) = fake.requests
            assert (sent["method"], sent["path"], sent["params"]) == (method, path, params), (item["call"], sent)
            assert sent["headers"]["authorization"] == f"Bearer {token}", sent["headers"]
            assert sent["headers"].get("x-tossinvest-account") == account, (item["call"], sent["headers"])
            assert (json.loads(sent["body"]) if sent["body"] else None) == body, (item["call"], sent["body"])
            plain = json.loads(json.dumps(result, default=str))
            assert plain == item["returns"], (item["call"], plain)
            outputs.append(json.dumps(plain, ensure_ascii=False))
        _no_secrets(fixture, outputs, Path(name))


def case_shared_token() -> None:
    fixture = _fixture("prd01-toss.json")
    tokens, now = fixture["tokens"], _time(fixture["now"])
    candles = ("GET", "/api/v1/candles")
    page = [200, {"result": {"candles": [_candle("2026-11-09T09:01:00+09:00")], "nextBefore": None}}]
    with tempfile.TemporaryDirectory() as name, mock.patch.object(toss, "_sleep", lambda seconds: None):
        path = Path(name) / "data" / "toss" / "token.json"
        for case, item in fixture["token_cases"].items():
            _token_file(path, tokens["stored"], now + timedelta(minutes=item["expires_in_minutes"]))
            fake = FakeToss({candles: [page]}, tokens["issued"], fixture["expires_in"])
            with httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(fake)) as http:
                shared = toss.SharedToken(http, tuple(fixture["credentials"].values()), path, lambda: now)
                toss.candles(http, shared, "069500", "1m", False, "2026-11-09")
            assert fake.count("/oauth2/token") == item["issues"], (case, fake.requests)
            used = tokens["stored"] if item["issues"] == 0 else tokens["issued"]
            assert fake.requests[-1]["headers"]["authorization"] == f"Bearer {used}", (case, fake.requests[-1])
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == {"access_token": tokens["issued"], "expires_at": (now + timedelta(seconds=fixture["expires_in"])).isoformat()}, saved
        assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700, (oct(path.stat().st_mode), oct(path.parent.stat().st_mode))

        _token_file(path, tokens["stored"], now)
        fake = FakeToss({candles: [page]}, tokens["issued"], fixture["expires_in"])
        with httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(fake)) as http:
            pair = [toss.SharedToken(http, tuple(fixture["credentials"].values()), path, lambda: now) for _ in range(2)]
            threads = [threading.Thread(target=str, args=(item,)) for item in pair]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        assert fake.count("/oauth2/token") == 1 and [item.value for item in pair] == [tokens["issued"]] * 2, (fake.requests, [item.value for item in pair])

        revoked = [401, {"error": {"code": "token-revoked", "message": "x"}}]
        for case, writes_other, responses, issues, used in (
            ("다른 쪽이 바꿈", True, [revoked, page], 0, tokens["other"]),
            ("같은 토큰", False, [revoked, page], 1, tokens["issued"]),
        ):
            _token_file(path, tokens["stored"], now + timedelta(days=1))

            def first(request: httpx.Request, writes_other: bool = writes_other) -> list:
                if writes_other:
                    _token_file(path, tokens["other"], now + timedelta(days=1))
                return revoked

            fake = FakeToss({candles: [first, page]}, tokens["issued"], fixture["expires_in"])
            with httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(fake)) as http:
                shared = toss.SharedToken(http, tuple(fixture["credentials"].values()), path, lambda: now)
                toss.candles(http, shared, "069500", "1m", False, "2026-11-09")
            sent = [request["headers"]["authorization"] for request in fake.requests if request["path"] == "/api/v1/candles"]
            assert sent == [f"Bearer {tokens['stored']}", f"Bearer {used}"] and fake.count("/oauth2/token") == issues, (case, fake.requests)

        _token_file(path, tokens["stored"], now + timedelta(days=1))
        fake = FakeToss({candles: [revoked]}, tokens["issued"], fixture["expires_in"])
        with httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(fake)) as http:
            shared = toss.SharedToken(http, tuple(fixture["credentials"].values()), path, lambda: now)
            try:
                toss.candles(http, shared, "069500", "1m", False, "2026-11-09")
                raise AssertionError("두 번 이어진 token-revoked가 실패하지 않았다")
            except toss.TossError as error:
                assert error.code == "V1_TOKEN_REVOKED", error
        assert fake.count("/api/v1/candles") == 2 and fake.count("/oauth2/token") == 1, fake.requests


def case_command_paths() -> None:
    fixture = _fixture("prd01-toss.json")
    stored = fixture["tokens"]["stored"]
    days = [f"2026-01-{day:02d}" for day in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20, 21, 22, 23, 26, 27, 28, 29, 30)] + ["2026-02-02"]

    def candles(request: httpx.Request) -> list:
        symbol, interval = request.url.params["symbol"], request.url.params["interval"]
        if interval == "1m":
            rows = [_candle("2026-01-05T09:01:00+09:00")]
        elif symbol in ("069500", "114800"):
            rows = [_candle("2026-01-05T00:00:00+09:00")]
        else:
            rows = [_candle(f"{day}T00:00:00-05:00") for day in reversed(days)]
        return [200, {"result": {"candles": rows, "nextBefore": None}}]

    original = httpx.Client
    with tempfile.TemporaryDirectory() as name, mock.patch.object(toss, "_sleep", lambda seconds: None):
        root = Path(name)
        (root / ".env").write_text("".join(f"{key}={value}\n" for key, value in fixture["credentials"].items()), encoding="utf-8")
        _token_file(root / "data" / "toss" / "token.json", stored, datetime.now(KST) + timedelta(days=1))
        fake = FakeToss({("GET", "/api/v1/candles"): [candles]}, fixture["tokens"]["issued"])
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(fake), **kwargs)),
            mock.patch.object(cli, "ROOT", root),
            mock.patch.object(cli, "DB_PATH", root / "data" / "market.sqlite3"),
            mock.patch.object(cli, "V3_PATHS", cli.V3_PATHS._replace(us=root / "data" / "us.sqlite3")),
            contextlib.redirect_stdout(output),
        ):
            for key in fixture["credentials"]:
                os.environ.pop(key, None)
            codes = [cli.fetch(), cli.fetch_us()]
            bars = v4_daily.fetch_bars(root, "2026-01-05", 30)
        assert codes == [0, 0], (codes, output.getvalue())
        assert bars is not None and set(bars) == {"069500", "114800"}, (bars, output.getvalue())
        assert fake.count("/oauth2/token") == 0, fake.requests
        assert {request["headers"]["authorization"] for request in fake.requests} == {f"Bearer {stored}"}, fake.requests
        _no_secrets(fixture, [output.getvalue()], root)


def case_order_errors() -> None:
    fixture = _fixture("prd01-toss.json")
    token, path = fixture["tokens"]["stored"], ("POST", "/api/v1/orders")
    with mock.patch.object(toss, "_sleep", lambda seconds: None):
        for item in fixture["errors"]:
            fake = FakeToss({path: [item["response"]]})
            with httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(fake)) as http:
                try:
                    toss_account.create_order(http, token, 3, "v5-20261110-0901-069500-B", "069500", "BUY", "DAY", 1)
                    raise AssertionError(f"{item['response']} 응답이 오류가 아니다")
                except toss.TossError as error:
                    assert error.code == item["code"] and toss_account.classify(error) == item["class"], (item, error.code, toss_account.classify(error))
                    assert error.retry_after == item.get("retry_after"), (item, error.retry_after)
            assert fake.count("/api/v1/orders") == 1, (item["response"], fake.requests)

        with tempfile.TemporaryDirectory() as name:
            token_path = Path(name) / "toss" / "token.json"
            now = _time(fixture["now"])
            _token_file(token_path, fixture["tokens"]["stored"], now + timedelta(days=1))
            fake = FakeToss({path: [[401, {"error": {"code": "token-revoked", "message": "x"}}]]}, fixture["tokens"]["issued"])
            with httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(fake)) as http:
                shared = toss.SharedToken(http, tuple(fixture["credentials"].values()), token_path, lambda: now)
                try:
                    toss_account.create_order(http, shared, 3, "v5-20261110-0901-069500-B", "069500", "BUY", "DAY", 1)
                    raise AssertionError("token-revoked가 오류가 아니다")
                except toss.TossError as error:
                    assert toss_account.classify(error) == "retry" and toss_account.classify(error, 1) == "stop", error
            assert fake.count("/api/v1/orders") == 1 and shared.value == fixture["tokens"]["issued"], (fake.requests, shared.value)

        for response in fixture["cancel_responses"]:
            fake = FakeToss({("POST", "/api/v1/orders/ord-1/cancel"): [response]})
            with httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(fake)) as http:
                try:
                    assert toss_account.cancel_order(http, token, 3, "ord-1") is None
                except toss.TossError:
                    pass
        for status, final in fixture["final"].items():
            assert toss_account.is_final(status) is final, (status, final)


CASES = {
    "01": [
        ("계좌·보유·매수 가능 금액·수수료율·상하한가·주문 생성·취소·상세·목록의 주소·쿼리·두 헤더·본문, 계좌번호는 끝 네 자리만, 인증 정보·계좌번호·토큰이 출력과 파일에 없음", case_client_requests),
        (
            "함께 쓰는 토큰: 10분보다 많이 남으면 받지 않고 10분이면 받음, 동시에 만료를 본 두 쪽은 한 번만 받음, token-revoked면 파일의 다른 토큰이나 새로 받은 토큰으로"
            " 한 번 다시 보내고 두 번 이어지면 V1_TOKEN_REVOKED, 파일 600·폴더 700",
            case_shared_token,
        ),
        ("명령 경로: fetch·v3-us-fetch·당일 1분봉 받기가 토큰 발급 없이 저장된 토큰을 씀", case_command_paths),
        (
            "주문 생성은 네트워크·5xx·429·409·토큰 오류에서도 한 번만 보내고 retry·stop·reject로 나뉨(429는 Retry-After, 토큰 오류는 두 번째에 stop),"
            " 취소 응답은 주문을 끝내지 않고 is_final은 FILLED·CANCELED·REJECTED만",
            case_order_errors,
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

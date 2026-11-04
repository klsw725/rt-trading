"""v5 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import fcntl
import functools
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx

from rt_trading import (
    cli,
    codex,
    engine,
    fixtures,
    toss,
    toss_account,
    v3_daily,
    v3_paper,
    v3_us,
    v4_acceptance,
    v4_daily,
    v4_head,
    v4_paper,
    v5_book,
    v5_live,
    v5_report,
    v5_target,
)
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


# PRD 02 목표 보유


def _weekdays(start: str, end: str) -> list[str]:
    days, day = [], Date.fromisoformat(start)
    while day <= Date.fromisoformat(end):
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def _period(tmp: Path, start: str, holidays: list[list[str]], today: str) -> v3_daily.Period:
    (tmp / "v5_period.json").write_text(json.dumps({"start": start}), encoding="utf-8")
    (tmp / "krx_holidays.csv").write_text("date,reason\n" + "".join(f"{day},{reason}\n" for day, reason in holidays), encoding="utf-8")
    return v5_target.load_period(tmp / "v5_period.json", tmp / "krx_holidays.csv", today)


def _rows(items: list[list[str]]) -> list[dict]:
    return [{"date": day, "slot": slot, "kind": kind, "status": status, "subordinate": subordinate} for day, slot, kind, status, subordinate in items]


def _plain(value: object) -> object:
    return json.loads(json.dumps(value))


@functools.cache
def _prd02_markets() -> dict:
    return {name: v4_acceptance._market(spec) for name, spec in _fixture("prd02-target.json")["markets"].items()}


def _prd02_subordinates(fixture: dict) -> dict:
    """v4 부하에 시험용 부하(적은 날짜·시각에 적은 주문을 낸다)를 더한다."""
    subordinates = dict(v4_paper.SUBORDINATES)
    for name, test in fixture["tests"].items():
        script = {(day, clock): engine.Order(kind, symbol, "signal") for day, clock, kind, symbol in test["orders"]}
        subordinates[name] = v3_paper.Subordinate(lambda script=script: lambda view: script.get((view.date, view.time)), test["overnight"], None)
    return subordinates


def _prd02_target(fixture: dict, case: dict, bars: dict | None = None, received: dict | None = None) -> v5_target.Target:
    full = _prd02_markets()[case["market"]]
    base = full._replace(days=[day for day in full.days if day < case["today"]])
    today = bars or {symbol: full.bars[symbol].get(case["today"], []) for symbol in v5_target.SYMBOLS}
    with tempfile.TemporaryDirectory() as name:
        period = _period(Path(name), fixture["start"], fixture["holidays"], case["today"])
    decisions = _rows(fixture["decisions"][case["decisions"]])
    return v5_target.target(
        base, period, decisions, case["today"], case["kind"], case["bar"], today, received or fixture["received"], _prd02_subordinates(fixture)
    )


def case_target_states() -> None:
    fixture = _prd02()
    for case in fixture["cases"]:
        result = _prd02_target(fixture, case)
        assert result.reason is None and result.symbol == case["symbol"] and _plain(result.state) == case["state"], (case["name"], result)
    case = next(case for case in fixture["cases"] if case["name"] == fixture["future"]["case"])
    full = _prd02_markets()[case["market"]]
    changed = {
        symbol: [
            market_bar._replace(**dict(zip(("open", "high", "low", "close", "volume"), fixture["future"]["bars"].get(symbol, {}).get(market_bar.time, list(market_bar)[1:]))))
            for market_bar in full.bars[symbol][case["today"]]
        ]
        for symbol in v5_target.SYMBOLS
    }
    result = _prd02_target(fixture, case, changed)
    assert result.symbol == case["symbol"] and _plain(result.state) == case["state"], ("그 봉 뒤의 시세를 바꿈", result)


def _prd02() -> dict:
    return _fixture("prd02-target.json")


def case_target_sixth_day() -> None:
    fixture = _prd02()["sixth"]
    expected, today = fixture["expected"], fixture["today"]
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        history = _weekdays(fixture["history"]["from"], fixture["history"]["to"])
        settled = [row[0] for row in fixture["decisions"]]
        spec = {"fetched_at": f"{today}T07:00:00+09:00", "volume": 1000, "days": [{"date": history[0], "prices": fixture["history"]["prices"]}]}
        spec["days"] += [{"date": day} for day in history[1:] + settled]
        fixtures.build(spec, tmp / "market.sqlite3")
        with closing(sqlite3.connect(tmp / "us.sqlite3")) as db, db:
            db.execute(f"CREATE TABLE us_candles_1d ({v3_us.TABLE})")
            rows = [(symbol, day, price) for day in _weekdays(fixture["us"]["from"], fixture["us"]["to"]) for symbol, price in fixture["us"]["prices"].items()]
            db.executemany("INSERT INTO us_candles_1d VALUES (?, ?, ?)", rows)
        paths = v4_daily.Paths(tmp / "market.sqlite3", tmp / "market.sqlite3", tmp / "paper.sqlite3", tmp / "head", tmp / "news", tmp / "us.sqlite3")
        for row in _rows(fixture["decisions"]):
            v4_head.insert(paths.paper, dict.fromkeys(v4_head.COLUMNS, "") | {"attempts": 0} | row)
        with closing(sqlite3.connect(paths.paper)) as db, db:
            db.execute(f"CREATE TABLE settled_days ({v3_daily.SETTLED})")
            db.executemany("INSERT INTO settled_days VALUES (?, ?)", [(day, "") for day in settled])
        period = _period(tmp, fixture["start"], [], today)
        decisions = v4_head.read(paths.paper)
        cal = v3_daily.calendar(v3_daily.data_days(paths.source), period)
        accounts = v4_daily.summary(paths, period, cal, today, decisions)
        month = [day for day in cal if day[:7] == today[:7]]
        content = v4_head.build_input(today, "08:50", period.days, month, paths.source, paths.us, accounts, decisions, None, None)
        data = v3_daily.settled_data(paths, settled)
        state: dict = {}
        records = v4_paper.simulate(data, cal, period.start, period.final_day, v4_head.plan([day for day in period.days if day <= settled[-1]], decisions), state=state)
    head = content["head_account"]
    assert {"subordinate": head["subordinate"], "holding": head["holding"]} == expected["head_account"], head
    assert content["decision"] == expected["decision"], content["decision"]
    assert len(content["recent_decisions"]) == expected["recent_decisions"] and content["settled_through"] == expected["settled_through"], content
    ids = [row["id"] for row in json.loads((ROOT / "src" / "rt_trading" / "v4_subordinates.json").read_text(encoding="utf-8"))]
    assert [item["id"] for item in content["subordinates"]] == ids, content["subordinates"]
    assert v4_head.PROMPT == (ROOT / "src" / "rt_trading" / "v4_head_prompt.txt").read_text(encoding="utf-8") and codex.MODEL == expected["model"]
    assert not [fill for fill in records.fills if fill.reason == "close"], records.fills
    assert _plain(state["holding"]) == expected["holding_after_fifth_day"] and settled[-1] not in records.closes, (state, records.closes)


def case_target_base() -> None:
    fixture = _prd02()
    for base, day, expected in fixture["limits"]:
        assert list(v5_target.limits(base, day)) == expected, (base, day, v5_target.limits(base, day))
    case = fixture["cases"][0]
    result = _prd02_target(fixture, case, received=fixture["one_tick_off"])
    assert result == (None, "base_changed", None), result
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        fixtures.build(fixture["markets"]["flat"], tmp / "market.sqlite3")
        paths = v4_daily.Paths(tmp / "market.sqlite3", tmp / "v5" / "market.sqlite3", tmp / "v5" / "paper.sqlite3", tmp / "v5" / "head", tmp / "v5" / "news", tmp / "us.sqlite3")
        start = _period(tmp, fixture["start"], fixture["holidays"], fixture["start"])
        source = v5_target.base(paths, start, fixture["start"])
        assert source is not None and source.days[-1] == "2025-10-29", source and source.days
        bars = {symbol: _prd02_markets()["flat"].bars[symbol][fixture["start"]] for symbol in v5_target.SYMBOLS}
        result = v5_target.target(source, start, _rows(fixture["decisions"][case["decisions"]]), fixture["start"], "intraday", "10:04", bars, fixture["received"])
        assert result.reason is None and result.symbol is None, result
        later = _period(tmp, fixture["start"], fixture["holidays"], "2025-10-31")
        assert v5_target.base(paths, later, "2025-10-31") is None


# PRD 03 실계좌 실행기


class _Broker:
    """가짜 토스 계좌. 같은 clientOrderId는 같은 주문이다. 주문 결과(post)와 체결(fill)은 사례가 정한다.

    post 차례: "ok", "network"(받지 않음), "lost"(받고 응답만 잃음), [상태, 본문](오류 응답).
    """

    def __init__(self, spec: dict, limits: dict, commission: str) -> None:
        self.holdings = {"069500": 0, "114800": 0} | spec.get("holdings", {})
        self.power, self.limits, self.commission = spec.get("power", 1_000_000), limits, commission
        self.script = list(spec.get("post", []))
        self.foreign = list(spec.get("foreign", []))
        self.orders: dict[str, dict] = {}
        self.clients: dict[str, str] = {}
        self.posts: list[list] = []
        self.cancels: list[str] = []
        self.on_post: list = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/api/v1/accounts":
            return httpx.Response(200, json={"result": [{"accountNo": "98765432101", "accountSeq": 3, "accountType": "BROKERAGE"}]})
        if path == "/api/v1/holdings":
            return httpx.Response(200, json={"result": {"items": [{"symbol": symbol, "quantity": str(count)} for symbol, count in self.holdings.items() if count]}})
        if path == "/api/v1/buying-power":
            return httpx.Response(200, json={"result": {"currency": "KRW", "cashBuyingPower": str(self.power)}})
        if path == "/api/v1/commissions":
            return httpx.Response(200, json={"result": [{"marketCountry": "KR", "commissionRate": self.commission}]})
        if path == "/api/v1/price-limits":
            upper, lower = self.limits[request.url.params["symbol"]]
            return httpx.Response(200, json={"result": {"upperLimitPrice": str(upper), "lowerLimitPrice": str(lower), "currency": "KRW"}})
        if path == "/api/v1/orders" and method == "GET":
            symbol = request.url.params["symbol"]
            found = [
                {"orderId": order_id, "symbol": order["symbol"], "side": order["side"], "quantity": str(order["quantity"]), "status": order["status"]}
                for order_id, order in self.orders.items()
                if order["symbol"] == symbol and order["status"] in ("PENDING", "PARTIAL_FILLED", "PENDING_CANCEL")
            ]
            found += [item for item in self.foreign if item["symbol"] == symbol]
            return httpx.Response(200, json={"result": {"orders": found, "nextCursor": None, "hasNext": False}})
        if path == "/api/v1/orders" and method == "POST":
            return self._post(request)
        if path.endswith("/cancel"):
            order_id = path.split("/")[-2]
            self.cancels.append(order_id)
            order = self.orders[order_id]
            if order["status"] in ("PENDING", "PARTIAL_FILLED"):
                order["status"] = "CANCELED"
                return httpx.Response(200, json={"result": {"orderId": order_id}})
            return httpx.Response(409, json={"error": {"code": "already-filled", "message": "x"}})
        order = self.orders[path.split("/")[-1]]
        execution = {key: order[key] for key in ("filledQuantity", "filledAmount", "averageFilledPrice", "commission", "tax")}
        return httpx.Response(200, json={"result": {"orderId": path.split("/")[-1], "symbol": order["symbol"], "side": order["side"], "status": order["status"], "quantity": str(order["quantity"]), "execution": execution}})

    def _post(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.posts.append([body["clientOrderId"], body["symbol"], body["side"], body["timeInForce"], body["quantity"]])
        for check in self.on_post:
            check(body)
        action = self.script.pop(0) if self.script else "ok"
        if action == "network":
            raise httpx.ConnectError("가짜 연결 오류", request=request)
        if isinstance(action, list):
            return httpx.Response(action[0], json=action[1], headers=action[2] if len(action) > 2 else None)
        order_id = self.clients.get(body["clientOrderId"])
        if order_id is None:
            order_id = self.clients[body["clientOrderId"]] = f"ord-{len(self.clients) + 1}"
            self.orders[order_id] = {
                "symbol": body["symbol"], "side": body["side"], "quantity": int(body["quantity"]), "status": "PENDING",
                "filledQuantity": "0", "filledAmount": None, "averageFilledPrice": None, "commission": None, "tax": None,
            }
        if action == "lost":
            raise httpx.ConnectError("가짜 응답 잃음", request=request)
        return httpx.Response(200, json={"result": {"orderId": order_id, "clientOrderId": body["clientOrderId"]}})

    def fill(self, client_order_id: str, status: str, quantity: int, amount: int | None, commission: int | None, tax: int | None) -> None:
        order = self.orders[self.clients[client_order_id]]
        before = int(order["filledQuantity"])
        order |= {
            "status": status, "filledQuantity": str(quantity), "filledAmount": None if amount is None else str(amount),
            "averageFilledPrice": None if not quantity or amount is None else str(amount / quantity), "commission": None if commission is None else str(commission),
            "tax": None if tax is None else str(tax),
        }
        self.holdings[order["symbol"]] += (quantity - before) * (1 if order["side"] == "BUY" else -1)


class _LiveClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _LiveCase:
    """임시 폴더의 v5 저장소와 가짜 토스·시계·목표·봉으로 실행기 하나를 만든다. 차례는 사례가 하나씩 부른다."""

    def __init__(self, fixture: dict, tmp: Path, scenario: dict, config: dict | None = None, broker: dict | None = None) -> None:
        self.fixture, self.tmp, self.day = fixture, tmp, fixture["day"]
        self.prices = dict(fixture["prices"])
        self.broker = _Broker(scenario.get("broker", {}) | (broker or {}), fixture["limits"], fixture["commission"])
        self.clock = _LiveClock(_time(f"{self.day}T08:00:00"))
        self.config = fixture["config"] | (config or {})
        (tmp / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        (tmp / "v5_period.json").write_text(json.dumps({"start": self.day}), encoding="utf-8")
        (tmp / "krx_holidays.csv").write_text("date,reason\n", encoding="utf-8")
        daily = v4_daily.Paths(tmp / "market.sqlite3", tmp / "v5" / "market.sqlite3", tmp / "v5" / "paper.sqlite3", tmp / "v5" / "head", tmp / "v5" / "news", tmp / "us.sqlite3")
        self.paths = v5_live.Paths(daily, tmp / "live.sqlite3", tmp / "config.json", tmp / "live.lock", tmp / "order.lock", tmp / "v5_period.json", tmp / "krx_holidays.csv")
        self.http = httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(self.broker))
        self.goal: v5_target.Target = v5_target.Target(None, None, {})
        self.live = self.executor()
        ledger = scenario.get("ledger")
        with v5_book.connect(self.paths.live) as db:
            if ledger is not None:
                ledger_id = v5_book.create_ledger(db, "live", self.config["account_seq"], self.config["budget"], self.clock())
                spent = self.config["budget"] - ledger["cash"]
                for symbol in v5_target.SYMBOLS:
                    if ledger.get(symbol):
                        db.execute(
                            "INSERT INTO entries (ledger_id, time, reason, cash, symbol, quantity) VALUES (?, ?, 'fill', ?, ?, ?)",
                            (ledger_id, self.clock().isoformat(timespec="seconds"), -spent, symbol, ledger[symbol]),
                        )
        if scenario.get("day_row", True):
            self.open_day(self.day)

    def executor(self) -> v5_live.Live:
        live = v5_live.Live(self.paths, self.http, "token-x", self.clock, self.clock.sleep, target=lambda *args: self.goal, bars=self._bars)
        live.today, live.period = self.day, v5_target.load_period(self.paths.period, self.paths.holidays, self.day)
        live.prices = dict(self.fixture["prices"])
        return live

    def open_day(self, day: str) -> None:
        with v5_book.connect(self.paths.live) as db:
            db.execute(
                "INSERT INTO days (date, mode, account_seq, limits, commission) VALUES (?, ?, ?, ?, ?)",
                (day, "live" if self.config["orders"] else "dry", self.config["account_seq"], json.dumps(self.fixture["limits"]), self.fixture["commission"]),
            )

    def _bars(self, day: str) -> dict[str, list]:
        """지금 시각까지 끝난 장 일정의 봉. 가격은 사례의 가격이다."""
        times = [time for time in v4_acceptance.sessions.session(day, v4_acceptance.sessions.exceptions()).bars if _time(f"{day}T{time}") <= self.clock.now]
        return {symbol: [v4_acceptance.market_data.Bar(time, price, price, price, price, 1000) for time in times] for symbol, price in self.prices.items()}

    def turn(self, name: str) -> list[list]:
        """그 차례를 한 번 하고, 그 사이 토스로 간 주문 생성 요청을 돌려준다."""
        found = v5_live.turns(self.live.today)
        self.current = next(turn for turn in found if turn.name == name or turn.bar == name and turn.name in ("intraday", "closing"))
        self.clock.now = max(self.clock.now, self.current.at)
        before = len(self.broker.posts)
        self.live._turn(self.current)
        return self.broker.posts[before:]

    def step(self, step: dict) -> None:
        if "fill" in step:
            self.broker.fill(*step["fill"])
            return
        if step.get("next_day"):
            self.day = self.fixture["next_day"]
            self.open_day(self.day)
            self.live.today, self.live.period = self.day, v5_target.load_period(self.paths.period, self.paths.holidays, self.day)
        self.goal = v5_target.Target(step.get("target"), step.get("reason"), {})
        posts = self.turn(step["turn"])
        assert posts == step["posts"], (step, posts)

    def balance(self, kind: str = "live") -> tuple[int, dict]:
        with v5_book.connect(self.paths.live) as db:
            return v5_book.balance(db, v5_book.ledger(db, kind)["id"])

    def stops(self) -> list[dict]:
        with v5_book.connect(self.paths.live) as db:
            return v5_book.active_stops(db)


def _prd03() -> dict:
    return _fixture("prd03-live.json")


def _scenario(name: str, config: dict | None = None, broker: dict | None = None) -> _LiveCase:
    fixture, holder = _prd03(), tempfile.TemporaryDirectory()
    case = _LiveCase(fixture, Path(holder.name), fixture["scenarios"][name], config, broker)
    case.holder = holder  # 사례 객체가 사라질 때 임시 폴더도 지운다
    return case


def case_live_schedule() -> None:
    fixture = _prd03()
    for day, expected in fixture["schedule"].items():
        found = v5_live.turns(day)
        intraday = [turn for turn in found if turn.name == "intraday"]
        closing = next(turn for turn in found if turn.name == "closing")
        assert len(found) == expected["count"], (day, len(found))
        assert [f"{found[0].at:%H:%M:%S}", f"{found[1].at:%H:%M:%S}", f"{found[-1].at:%H:%M:%S}"] == [expected["start"], expected["pre_open"], expected["end"]], day
        assert [intraday[0].bar, f"{intraday[0].at:%H:%M:%S}"] == expected["first"] and [intraday[-1].bar, f"{intraday[-1].at:%H:%M:%S}"] == expected["last"], day
        assert [closing.bar, f"{closing.at:%H:%M:%S}"] == expected["closing"], day


def case_live_sell_then_buy() -> None:
    fixture = _prd03()
    for name in ("sell_then_buy_open", "sell_close_buy_next_open", "quantity"):
        case = _scenario(name)
        for step in fixture["scenarios"][name]["steps"]:
            case.step(step)
        cash, held = case.balance()
        expected = fixture["scenarios"][name]["balance"]
        assert [cash, held] == [expected["cash"], expected["holdings"]], (name, cash, held)
        assert not case.stops(), (name, case.stops())
    for variant in ("max_shares", "unlimited"):
        item = fixture["scenarios"]["quantity"][variant]
        case = _scenario("quantity", {"max_shares": item["config"]} if "config" in item else None, {"power": item["power"]} if "power" in item else None)
        case.step({"turn": item["turn"], "target": item["target"], "posts": item["posts"]})


def _orders(case: _LiveCase, where: str = "1") -> list[dict]:
    with v5_book.connect(case.paths.live) as db:
        return v5_book.orders(db, where)


def case_live_duplicates() -> None:
    fixture = _prd03()["scenarios"]
    case = _scenario("lost")
    case.goal = v5_target.Target("069500", None, {})
    assert case.turn("09:01") == [fixture["lost"]["order"]], case.broker.posts
    assert _orders(case)[0]["status"] == "unknown"
    case.clock.now += timedelta(seconds=10)
    case.live._between()
    assert case.broker.posts == [fixture["lost"]["order"]] * 2 and len(case.broker.orders) == 1, case.broker.posts
    assert [order["status"] for order in _orders(case)] == ["open"], _orders(case)

    case = _scenario("lost")
    case.goal = v5_target.Target("069500", None, {})
    case.turn("09:01")
    case.live = case.executor()
    case.live._between()
    assert case.broker.posts == [fixture["lost"]["order"]] * 2 and len(case.broker.orders) == 1 and _orders(case)[0]["status"] == "open", "다시 켜진 뒤"

    for name in ("never", "rate_limited"):
        case = _scenario(name)
        case.goal = v5_target.Target("069500", None, {})
        case.turn("09:01")
        limit = case.clock.now + timedelta(minutes=8)
        while case.clock.now < limit + timedelta(seconds=30):
            case.clock.now += timedelta(seconds=5)
            case.live._between()
        assert len(case.broker.posts) == fixture[name]["posts_in_8_minutes"], (name, len(case.broker.posts))
        assert [stop["kind"] for stop in case.stops()] == ["manual"], (name, case.stops())

    case = _scenario("lost")
    case.goal = v5_target.Target("069500", None, {})
    case.turn("09:01")
    v5_live.stop(case.paths, False, case.clock)
    case.clock.now += timedelta(seconds=10)
    case.live._between()
    assert len(case.broker.posts) == 1 and {stop["kind"] for stop in case.stops()} == {"human", "manual"}, ("다시 보내기 전 정지", case.stops())

    case = _scenario("duplicate_turn")
    case.goal = v5_target.Target("069500", None, {})
    assert case.turn("09:01") == [fixture["duplicate_turn"]["first"]]
    case.broker.fill(*fixture["duplicate_turn"]["fill"])
    case.live = case.executor()
    case.goal = v5_target.Target(None, None, {})
    assert case.turn("09:01") == [] and case.balance()[1] == {"069500": 38}, case.broker.posts

    case = _scenario("foreign")
    case.goal = v5_target.Target("069500", None, {})
    assert case.turn("09:01") == [] and [stop["kind"] for stop in case.stops()] == ["safety"], case.stops()

    case = _scenario("lost")
    with open(case.paths.lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert case.live.run() == 1 and "V5_LIVE_RUNNING" in output.getvalue(), output.getvalue()
        fcntl.flock(held, fcntl.LOCK_UN)

    item = fixture["late_start"]
    case = _scenario("late_start")
    case.clock.now = _time(item["start_at"])
    with contextlib.redirect_stdout(io.StringIO()):
        assert case.live.run() == 0
    with v5_book.connect(case.paths.live) as db:
        done = [row["turn"] for row in db.execute("SELECT turn FROM turns ORDER BY rowid")]
    assert done == item["turns"], done


def case_live_stops() -> None:
    case = _scenario("lost", broker={"post": []})
    case.goal = v5_target.Target("069500", None, {})

    def stop_then_target(*args: object) -> v5_target.Target:
        v5_live.stop(case.paths, False, case.clock)
        return v5_target.Target("069500", None, {})

    case.live.target = stop_then_target
    with contextlib.redirect_stdout(io.StringIO()):
        assert case.turn("09:01") == [], "주문 직전 정지"
    case = _scenario("lost", broker={"post": []})
    case.goal = v5_target.Target("069500", None, {})
    case.turn("09:01")
    with contextlib.redirect_stdout(io.StringIO()):
        v5_live.stop(case.paths, False, case.clock)
    case.clock.now += timedelta(seconds=5)
    case.live._between()
    assert case.broker.cancels == ["ord-1"], ("차례 사이 정지는 5초 안에 봇 주문 취소", case.broker.cancels)
    assert case.turn("09:02") == [], "멈춤 중에는 주문 없음"

    fixture = _prd03()["scenarios"]
    for sell, posts in ((True, fixture["sell_stop"]["posts"]), (False, [])):
        case = _scenario("sell_stop")
        _quiet(v5_live.stop, case.paths, sell, case.clock)
        case.goal = v5_target.Target("069500", None, {})
        assert _quiet(case.turn, "09:01") == posts, ("멈추고 다 팔기" if sell else "멈춤", case.broker.posts)

    case = _scenario("sell_stop", broker={"holdings": {"069500": 9, "114800": 0}})
    case.goal = v5_target.Target(None, None, {})
    assert _quiet(case.turn, "09:01") == [] and [stop["kind"] for stop in case.stops()] == ["safety"], ("실계좌 수량이 장부와 다름", case.stops())

    item = fixture["rejected"]
    case = _scenario("rejected")
    case.goal = v5_target.Target("069500", None, {})
    for index, (turn, count) in enumerate(zip(item["turns"], item["posts"], strict=True)):
        if index == 2:
            case.live = case.executor()
        assert len(_quiet(case.turn, turn)) == count, (turn, case.broker.posts)
    assert [stop["kind"] for stop in case.stops()] == ["safety"], ("거부 세 번", case.stops())


def case_live_fills() -> None:
    fixture = _prd03()["scenarios"]
    item = fixture["partial"]
    case = _scenario("partial")
    case.goal = v5_target.Target("069500", None, {})
    assert case.turn("09:01") == [item["first"]]
    case.broker.fill(*item["fill"])
    assert case.turn("09:02") == [] and case.broker.cancels == ["ord-1"], case.broker.cancels
    assert list(case.balance()) == [item["after_partial"]["cash"], item["after_partial"]["holdings"]], case.balance()
    assert case.turn("09:03") == [] and not case.stops(), case.stops()
    case.broker.orders["ord-1"]["commission"] = str(item["late_fee"])
    with v5_book.connect(case.paths.live) as db:
        entries = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    case.turn("end")
    case.live._apply(_orders(case)[0])
    with v5_book.connect(case.paths.live) as db:
        after = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert list(case.balance()) == [item["after_fee"]["cash"], item["after_fee"]["holdings"]] and after == entries + 1, (case.balance(), entries, after)

    item = fixture["auction"]
    case = _scenario("auction")
    case.goal = v5_target.Target("069500", None, {})
    assert case.turn("pre_open") == [item["pre_open"]]
    case.clock.now = _time(f"{case.day}T08:58:00")
    case.live._between()
    assert case.broker.cancels == [], "개장 전 주문을 개장 전에 취소함"
    case.turn("09:01")
    assert case.broker.cancels == ["ord-1"], "개장 뒤 남은 개장 전 주문을 취소하지 않음"
    case = _scenario("auction")
    case.goal = v5_target.Target("069500", None, {})
    assert case.turn("closing") == [item["closing"]]
    case.clock.now = _time(f"{case.day}T15:25:00")
    case.live._between()
    case.turn("end")
    assert case.broker.cancels == [], "종가 차례 주문을 마감 전에 취소함"

    case = _scenario("replaced")
    case.goal = v5_target.Target("069500", None, {})
    case.turn("09:01")
    case.broker.orders["ord-1"]["status"] = fixture["replaced"]["status"]
    with contextlib.redirect_stdout(io.StringIO()):
        assert case.turn("09:02") == [] and [stop["kind"] for stop in case.stops()] == ["safety"], case.stops()


def case_live_limits() -> None:
    item = _prd03()["scenarios"]["limits"]
    for name in ("daily", "above_daily", "base_changed", "weekly", "cumulative"):
        case = _scenario("limits")
        fixtures.build(item["source"], case.paths.daily.source)
        if "limits" in item[name]:
            with v5_book.connect(case.paths.live) as db:
                db.execute("UPDATE days SET limits = ? WHERE date = ?", (json.dumps(item[name]["limits"]), case.day))
        case.prices["069500"] = item[name]["price"]
        case.goal = v5_target.Target("069500", None, {})
        assert case.turn("09:01") == item[name]["posts"], (name, case.broker.posts)
        with v5_book.connect(case.paths.live) as db:
            latches = [[row[0], row[1]] for row in db.execute("SELECT kind, period FROM latches WHERE kind != 'rejections' ORDER BY kind")]
        assert latches == item[name].get("latches", []), (name, latches)
        assert [[stop["kind"], stop["mode"]] for stop in case.stops()] == ([item[name]["stop"]] if "stop" in item[name] else []), (name, case.stops())
        if name == "daily":
            case.broker.fill(*item["fill"])
            assert case.turn("09:02") == [], "하루 한도 뒤 다시 삼"
            case.live = case.executor()
            assert case.turn("09:03") == [], "다시 켜진 뒤 하루 한도를 잊음"


def _quiet(function: object, *args: object) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return function(*args)


def case_live_resume() -> None:
    case = _scenario("resume")
    with v5_book.connect(case.paths.live) as db:
        v5_book.add_stop(db, "manual", "hold", "수동 확인 필요", case.clock())
    assert _quiet(v5_live.resume, case.paths, case.http, "token-x", case.clock, case.clock.sleep, False) == 1 and case.stops(), "manual은 v5-resume으로 풀리지 않음"

    case = _scenario("resume", broker={"holdings": {"069500": 9, "114800": 0}})
    _quiet(v5_live.stop, case.paths, False, case.clock)
    assert _quiet(v5_live.resume, case.paths, case.http, "token-x", case.clock, case.clock.sleep, False) == 1 and case.stops(), "장부와 실계좌가 달라도 풂"

    case = _scenario("resume")
    _quiet(v5_live.stop, case.paths, False, case.clock)
    original = case.broker.__call__

    def late_stop(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/holdings":
            _quiet(v5_live.stop, case.paths, True, case.clock)
        return original(request)

    case.http = httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(late_stop))
    assert _quiet(v5_live.resume, case.paths, case.http, "token-x", case.clock, case.clock.sleep, False) == 0
    assert [[stop["id"], stop["mode"]] for stop in case.stops()] == [[2, "sell"]], ("풀기 중에 들어온 정지를 풂", case.stops())

    case = _scenario("new_ledger", config=_prd03()["scenarios"]["new_ledger"]["config"])
    with v5_book.connect(case.paths.live) as db:
        v5_book.add_intent(db, {"client_order_id": "v5-x", "kind": "live", "ledger_id": 1, "date": case.day, "turn": "09:01", "symbol": "069500", "side": "BUY", "tif": "DAY", "quantity": 1, "created_at": case.clock().isoformat()})
        v5_book.update_order(db, "v5-x", status="unknown")
        v5_book.add_stop(db, "manual", "hold", "수동 확인 필요", case.clock())
    assert _quiet(v5_live.resume, case.paths, case.http, "token-x", case.clock, case.clock.sleep, True) == 0 and not case.stops(), case.stops()
    assert _orders(case)[0]["status"] == "abandoned" and list(case.balance()) == [300000, {}], (_orders(case), case.balance())
    case.live._between()
    assert case.broker.posts == [], "봉인한 주문을 다시 보냄"
    other = _scenario("new_ledger")
    other.paths.config.write_text(json.dumps(other.config | {"account_seq": 4}), encoding="utf-8")
    assert _quiet(v5_live.resume, other.paths, other.http, "token-x", other.clock, other.clock.sleep, True) == 1, "오늘과 다른 계좌로 새 장부"
    busy = _scenario("new_ledger", broker={"holdings": {"069500": 1, "114800": 0}})
    assert _quiet(v5_live.resume, busy.paths, busy.http, "token-x", busy.clock, busy.clock.sleep, True) == 1, "보유가 있는데 새 장부"


def case_live_practice() -> None:
    item = _prd03()["scenarios"]["practice"]
    case = _scenario("practice", config=item["config"])
    case.goal = v5_target.Target("069500", None, {})
    _quiet(case.turn, "start")
    assert _quiet(case.turn, "09:01") == item["posts"] and case.broker.cancels == [], case.broker.posts
    order = _orders(case)[0]
    assert [order[key] for key in ("client_order_id", "kind", "symbol", "side", "tif", "quantity", "status")] == item["dry_order"], order
    assert list(case.balance("dry")) == [item["dry_balance"]["cash"], item["dry_balance"]["holdings"]], case.balance("dry")
    with v5_book.connect(case.paths.live) as db:
        assert v5_book.ledger(db, "live") is None, "연습 장부가 실장부로 넘어감"
    (case.paths.config).write_text(json.dumps(case.config | {"orders": True}), encoding="utf-8")
    case.live = case.executor()
    assert _quiet(case.turn, "09:02") == [] and case.live._day_record()["mode"] == "dry", "그날 중 설정 바꾸기가 그날 동작을 바꿈"

    case = _scenario("create")
    _quiet(case.turn, "start")
    with v5_book.connect(case.paths.live) as db:
        ledger, record = v5_book.ledger(db, "live"), v5_book.day(db, case.day)
    assert ledger["start_cash"] == 500000 and ledger["account_seq"] == 3 and record["mode"] == "live" and not case.stops(), (ledger, record)
    case = _scenario("create", broker={"holdings": {"069500": 1, "114800": 0}})
    _quiet(case.turn, "start")
    with v5_book.connect(case.paths.live) as db:
        ledger, record = v5_book.ledger(db, "live"), v5_book.day(db, case.day)
    assert ledger is None and record["mode"] == "dry" and [stop["kind"] for stop in case.stops()] == ["safety"], (ledger, record)

    case = _scenario("guard", config=_prd03()["scenarios"]["guard"]["config"])
    _quiet(case.turn, "start")
    assert case.live._day_record()["mode"] == "live" and [stop["kind"] for stop in case.stops()] == ["safety"], "보유가 있는데 연습 운용으로 돌아감"

    case = _scenario("lost", broker={"post": []})
    with open(case.paths.order_lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        case.goal = v5_target.Target("069500", None, {})
        assert _quiet(case.turn, "09:01") == [], "다른 쪽이 주문 잠금을 쥐었는데 주문함"
        assert _quiet(v5_live.resume, case.paths, case.http, "token-x", case.clock, case.clock.sleep, False) == 1
        fcntl.flock(held, fcntl.LOCK_UN)


def _filling(case: _LiveCase, after_buy: Callable[[], None] | None = None) -> httpx.Client:
    """점검 주문(v5c-)을 받자마자 다 체결하는 가짜 토스. after_buy는 매수 체결 뒤에 부른다."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = case.broker(request)
        if request.method == "POST" and request.url.path == "/api/v1/orders":
            body = json.loads(request.content)
            if body["clientOrderId"].startswith("v5c-") and body["timeInForce"] == "DAY":
                case.broker.fill(body["clientOrderId"], "FILLED", 1, 10000, 1, 0)
                if body["side"] == "BUY" and after_buy:
                    after_buy()
        return response

    return httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(handler))


def case_live_check() -> None:
    item = _prd03()["scenarios"]["check"]
    case = _scenario("check", config=item["config"])
    case.clock.now = _time(item["pre_open_at"])
    assert _quiet(v5_live.check, case.paths, case.http, "token-x", case.clock, case.clock.sleep, True) == 0
    assert case.broker.posts == item["pre_open_posts"] and case.broker.cancels == ["ord-1"], (case.broker.posts, case.broker.cancels)
    assert [order["status"] for order in _orders(case)] == ["final"] and _orders(case)[0]["kind"] == "check"

    case = _scenario("check", config=item["config"])
    case.clock.now = _time(item["intraday_at"])
    http = _filling(case)
    assert _quiet(v5_live.check, case.paths, http, "token-x", case.clock, case.clock.sleep, True) == 0
    assert case.broker.posts == item["intraday_posts"] and case.broker.holdings == {"069500": 0, "114800": 0}, case.broker.posts
    with v5_book.connect(case.paths.live) as db:
        assert v5_book.ledger(db, "live") is None and db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0, "점검 주문이 장부에 들어감"

    case = _scenario("check", config=item["config"])
    case.clock.now = _time(item["intraday_at"])
    http = _filling(case, lambda: _quiet(v5_live.stop, case.paths, False, case.clock))
    assert _quiet(v5_live.check, case.paths, http, "token-x", case.clock, case.clock.sleep, True) == 1
    assert [post[2] for post in case.broker.posts] == ["BUY"], ("매도 직전 정지에도 매도함", case.broker.posts)

    for config, broker in ((None, None), (item["config"], {"holdings": {"069500": 1, "114800": 0}})):
        case = _scenario("check", config=config, broker=broker)
        case.clock.now = _time(item["intraday_at"])
        assert _quiet(v5_live.check, case.paths, case.http, "token-x", case.clock, case.clock.sleep, True) == 1 and case.broker.posts == [], (config, broker)

    case = _scenario("check", config=item["config"])
    case.clock.now = _time(item["intraday_at"])
    with open(case.paths.order_lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert _quiet(v5_live.check, case.paths, case.http, "token-x", case.clock, case.clock.sleep, True) == 1 and case.broker.posts == []
        fcntl.flock(held, fcntl.LOCK_UN)


def case_live_reasons() -> None:
    item = _prd03()["scenarios"]["reasons"]

    def prepared(**changes: object) -> _LiveCase:
        case = _scenario("reasons")
        fixtures.build(item["source"], case.paths.daily.source)
        v4_head.insert(case.paths.daily.paper, dict.fromkeys(v4_head.COLUMNS, "") | {"attempts": 0} | _rows([item["decision"]])[0])
        case.live.target = v5_target.target
        for key, value in changes.items():
            setattr(case, key, value)
        return case

    case = prepared()
    assert _quiet(case.turn, "09:01") == item["computed"], ("목표를 정할 수 있는 날", case.broker.posts)

    def reason(case: _LiveCase) -> str:
        with v5_book.connect(case.paths.live) as db:
            return db.execute("SELECT reason FROM turns ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    case = prepared()
    case.paths.period.write_text(json.dumps({"start": item["early_start"]}), encoding="utf-8")
    case.live.period = v5_target.load_period(case.paths.period, case.paths.holidays, case.day)
    assert _quiet(case.turn, "09:01") == [] and reason(case) == "settlement_behind", reason(case)

    case = prepared()
    with v5_book.connect(case.paths.live) as db:
        db.execute("UPDATE days SET limits = NULL WHERE date = ?", (case.day,))
    original = case.broker.__call__
    case.http = case.live.http = httpx.Client(
        base_url=toss.BASE_URL,
        transport=httpx.MockTransport(lambda request: httpx.Response(500, json={}) if request.url.path == "/api/v1/price-limits" else original(request)),
    )
    with mock.patch.object(toss, "_sleep", lambda seconds: None):
        assert _quiet(case.turn, "09:01") == [] and reason(case) == "limits_missing", reason(case)

    case = prepared()
    with v5_book.connect(case.paths.live) as db:
        db.execute("UPDATE days SET limits = ? WHERE date = ?", (json.dumps(item["one_tick_off"]), case.day))
    assert _quiet(case.turn, "09:01") == [] and reason(case) == "base_changed", reason(case)

    case = prepared()
    full = case._bars
    case.live.bars = lambda day: {"069500": full(day)["069500"], "114800": [bar for bar in full(day)["114800"] if bar.time != "09:01"]}
    assert _quiet(case.turn, "09:01") == [] and reason(case) == "bar_missing", reason(case)
    assert case.clock.now >= _time(f"{case.day}T09:01:40"), ("봉을 +40초까지 기다리지 않음", case.clock.now)

    case = _scenario("quantity")
    case.goal = v5_target.Target("069500", None, {})
    _quiet(case.turn, "09:01")
    case.broker.fill("v5-20251112-0901-069500-B", "FILLED", 23, 230115, 35, 0)
    _quiet(case.turn, "09:02")
    with v5_book.connect(case.paths.live) as db:
        row = dict(db.execute("SELECT * FROM turns ORDER BY rowid DESC LIMIT 1").fetchone())
    assert json.loads(row["prices"]) == {"069500": 10000, "114800": 1500} and row["cash"] == 269850 and json.loads(row["holdings"]) == {"069500": 23}, row
    assert row["equity"] == 269850 + 23 * 10000 and json.loads(row["real"]) == {"069500": 23, "114800": 0} and row["matched"] == 1, row
    assert json.loads(row["limits"]) == {"daily": False, "weekly": False}, row


# PRD 04 운영과 보고


@functools.cache
def _run_v5_days() -> tuple[dict, list[dict]]:
    """PRD 04 fixture의 날마다 07:30 v5-daily와 여섯째 날 v5-decide를 끝 없는 기간으로 한 번 돌려 사례들이 함께 쓴다.

    (날짜·판단 → 결과, 실행마다 출력)을 돌려주고, 보고서 사례가 쓰도록 임시 폴더를 남긴다(_RUN_HOLDER).
    """
    fixture = _fixture("prd04-run.json")
    holder = tempfile.TemporaryDirectory()
    _RUN_HOLDER.append(holder)
    tmp = Path(holder.name)
    paths = v4_daily.Paths(tmp / "market.sqlite3", tmp / "v5" / "market.sqlite3", tmp / "v5" / "paper.sqlite3", tmp / "v5" / "head", tmp / "v5" / "news", tmp / "us.sqlite3")
    (tmp / "v5_period.json").write_text(json.dumps({"start": fixture["start"]}), encoding="utf-8")
    (tmp / "krx_holidays.csv").write_text("date,reason\n", encoding="utf-8")
    with closing(sqlite3.connect(paths.us)) as db, db:
        db.execute(f"CREATE TABLE us_candles_1d ({v3_us.TABLE})")
        db.executemany("INSERT INTO us_candles_1d VALUES (?, ?, ?)", [(symbol, day, price) for day in _weekdays(fixture["us"]["from"], fixture["us"]["to"]) for symbol, price in fixture["us"]["prices"].items()])
    (tmp / "token" / "auth").mkdir(parents=True)
    (tmp / "token" / "auth" / "codex.json").write_text(json.dumps(fixture["token"] | {"expires": v4_acceptance.FAR_FUTURE_MS}), encoding="utf-8")
    history = _weekdays(fixture["history"]["from"], fixture["history"]["to"])
    results, outputs = {}, []

    def fetch(left: float, clock: v4_acceptance._Clock) -> int:
        today = clock.now.date().isoformat()
        days = [{"date": history[0], "prices": fixture["history"]["prices"]}] + [{"date": day} for day in history[1:] + [day for day in fixture["days"] if day < today]]
        fixtures.build({"fetched_at": clock.now.isoformat(), "volume": 1000, "days": days}, paths.source)
        return 0

    def bars(day: str, seconds: float) -> dict:
        return v4_acceptance._bars(day, {symbol: {} for symbol in v5_target.SYMBOLS}) | {
            symbol: [v4_acceptance.market_data.Bar(time, price, price, price, price, 1000) for time in v4_acceptance.sessions.session(day, v4_acceptance.sessions.exceptions()).bars if time <= "10:00"]
            for symbol, price in fixture["history"]["prices"].items()
        }

    for day in fixture["days"]:
        steps = [("daily", "07:30:00")] + ([("decide", fixture["decide"]["at"])] if day == fixture["decide"]["day"] else [])
        for command, at in steps:
            clock, output = v4_acceptance._Clock(day, at), io.StringIO()
            fake = v4_acceptance._Codex(fixture, clock, [["ok", 5, fixture["reply"]]])
            period = lambda clock=clock: v5_target.load_period(tmp / "v5_period.json", tmp / "krx_holidays.csv", clock.now.date().isoformat())
            credentials = lambda: codex.credentials(tmp / "token")
            with contextlib.redirect_stdout(output), httpx.Client(transport=httpx.MockTransport(fake.handler)) as http:
                if command == "daily":
                    v4_daily.run(paths, tmp / "v5_period.json", tmp / "krx_holidays.csv", lambda left, clock=clock: fetch(left, clock), lambda *args: ([], []), http, credentials, clock, clock.sleep, period)
                else:
                    v4_daily.decide(paths, tmp / "v5_period.json", tmp / "krx_holidays.csv", fixture["decide"]["slot"], bars, lambda *args: ([], []), http, credentials, clock, clock.sleep, period)
            outputs.append(output.getvalue())
            decisions = {(row["date"], row["slot"]): row["status"] for row in v4_head.read(paths.paper)}
            results[(day, command)] = {"settled": v3_daily.settled_days(paths.paper), "decisions": decisions}
    return {"paths": paths, "tmp": tmp, "results": results}, outputs


_RUN_HOLDER: list = []


def case_run_open_period() -> None:
    fixture = _fixture("prd04-run.json")
    run, outputs = _run_v5_days()
    for day, check in fixture["checks"].items():
        result = run["results"][(day, "daily")]
        first = v4_head.slots(day)[0].time
        assert result["settled"][-1] == check["settled_last"] and result["decisions"].get((day, first)) == check["pre_open"], (day, result)
    decide = fixture["decide"]
    assert run["results"][(decide["day"], "decide")]["decisions"].get((decide["day"], decide["slot"])) == decide["status"], run["results"][(decide["day"], "decide")]
    for output in outputs:
        for word in fixture["absent"]:
            assert word not in output, (word, output)


def case_run_report() -> None:
    fixture = _fixture("prd04-run.json")
    run, _ = _run_v5_days()
    item, tmp = fixture["live"], run["tmp"]
    live_paths = v5_live.Paths(run["paths"], tmp / "v5" / "live.sqlite3", tmp / "v5" / "config.json", tmp / "v5" / "live.lock", tmp / "v5" / "order.lock", tmp / "v5_period.json", tmp / "krx_holidays.csv")
    with v5_book.connect(live_paths.live) as db:
        ledger = v5_book.create_ledger(db, "live", 3, item["ledger"]["start_cash"], datetime.fromisoformat(item["ledger"]["created_at"]))
        for order in item["orders"]:
            v5_book.add_intent(db, {key: order[key] for key in ("client_order_id", "date", "turn", "symbol", "side", "quantity")} | {"kind": "live", "ledger_id": ledger, "tif": "DAY", "created_at": order["at"]})
            v5_book.update_order(db, order["client_order_id"], status=order["status"], order_id=order["order_id"])
            if order["filled"]:
                snapshot = {"status": order["toss_status"], "filled_quantity": order["filled"], "filled_amount": Decimal(order["amount"]), "average_price": None, "commission": Decimal(order["commission"]), "tax": Decimal(order["tax"])}
                v5_book.apply_fill(db, v5_book.orders(db, "client_order_id = ?", (order["client_order_id"],))[0], snapshot, datetime.fromisoformat(order["at"]))
        for kind, mode, reason in item["stops"]:
            v5_book.add_stop(db, kind, mode, reason, datetime.fromisoformat(item["ledger"]["created_at"]))
    period = v5_target.load_period(tmp / "v5_period.json", tmp / "krx_holidays.csv", fixture["days"][-1])
    content, _, _, orders, daily = v5_report.build(live_paths, period)
    live = content["live"]
    for key, value in item["expected"].items():
        same = abs(live[key] - value) < 1e-12 if isinstance(value, float) else live[key] == value
        assert same, (key, live[key], value)
    assert len(daily) == len(item["daily"]), daily
    for row, (day, head, equity, bot) in zip(daily, item["daily"], strict=True):
        assert row["date"] == day and abs(row["head_return"] - head) < 1e-12 and row["close_equity"] == equity and abs(row["bot_return"] - bot) < 1e-12, (row, day)
    assert len(orders) == item["orders_csv"] and content["verdict"] is None and any(account["id"] == "head" for account in content["accounts"]), orders
    assert period.final_day not in json.dumps(content, ensure_ascii=False), ("엔진에 주는 기간 마지막 날이 보고서에 있음", period.final_day)


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
    "02": [
        (
            "목표와 엔진 상태: 장중 교체 전·교체·체결 뒤, 하루 한도 매도와 체결 뒤(한도 알림, 다시 사지 않음), 마감 10분 전 overnight 매수, 하루 안에 끝나는 부하의"
            " 장중 보유와 종가 차례 청산, 개장 전 보유 부하 매수와 교체(오늘 봉 없이), 장중 교체의 판 뒤 삼, 다음 달 거래일이 있어야 사는 월말 turn_of_month,"
            " 그 봉 뒤의 시세를 바꿔도 같음",
            case_target_states,
        ),
        (
            "여섯 번째 거래일 개장 전 입력: 헤드 계좌 부하·보유, 지금 부하·남은 교체, 지난 판단 수, 정산한 마지막 날, 부하 목록·지시문·모델이 v4와 같음,"
            " 다섯 번째 날·데이터 마지막 날 청산 없음, state를 주면 오늘 마감 자산이 closes에 없음",
            case_target_sixth_day,
        ),
        ("상·하한가 계산(1원·5원 호가 경계와 2023-12-11 전), 한 호가 다르면 base_changed, v5 시작일은 원본 시세로 목표를 정하고 다른 날 정산이 없으면 None", case_target_base),
    ],
    "03": [
        ("실행 차례: 정상일·10시 개장일·수능일의 시작 점검·개장 전·첫·마지막 장중·종가·끝 차례 시각과 수", case_live_schedule),
        (
            "판 뒤에만 삼: 개장 전 OPG 매도 → 개장 뒤 DAY 매수, 종가 DAY 매도 → 끝 차례 주문 없음 → 다음 거래일(수능일 09:55) OPG 매수, 매수 수량이 매수 가능 금액·"
            "상한가·수수료율(0.0188%)과 max_shares를 넘지 않고, 목표 종목을 들고 있으면 더 사지 않음",
            case_live_sell_then_buy,
        ),
        (
            "중복 막기: 응답을 잃은 주문은 같은 번호로 다시 보내 주문이 하나(다시 켜져도), 받지 않는 주문은 10초마다 8분까지 48번 보낸 뒤 manual,"
            " 429 Retry-After가 8분을 넘으면 8분에 manual, 다시 보내기 전 정지면 보내지 않고 manual, 같은 차례 두 번째 실주문 없음, 모르는 대기 주문은 안전 정지,"
            " 두 번째 실행기는 바로 끝남, 늦게 켜지면 밀린 차례를 건너뜀",
            case_live_duplicates,
        ),
        (
            "정지: 주문 직전에 들어온 정지는 그 주문을 막고, 차례 사이 정지는 5초 안에 봇 주문을 취소하며, 멈춤 중에는 주문 없음, 멈추고 다 팔기는 다음 차례에 팖,"
            " 실계좌 수량이 장부와 다르면 안전 정지, 거부가 세 번 이어지면 안전 정지(다시 켜져도 센 것이 이어짐)",
            case_live_stops,
        ),
        (
            "체결과 취소: 부분 체결분이 장부에 들어가고 다음 장중 차례에 남은 주문을 취소, 같은 상태를 다시 받아도 한 번, 늦은 수수료는 차이만,"
            " 개장 전 주문은 개장 전에·종가 차례 주문은 마감 전에 취소하지 않음, 정정 상태는 안전 정지",
            case_live_fills,
        ),
        (
            "실계좌 손실 한도: 정확히 −2%면 하루 한도로 팔고 그날 다시 사지 않으며 다시 켜져도 이어짐, 9,805원은 발동 안 함, 기준가가 다른 날은 하루 한도를 보지 않고"
            " 정확히 −5%면 한 주 한도, 정확히 −20%면 누적 한도로 멈추고 다 팖",
            case_live_limits,
        ),
        (
            "풀기: manual과 장부·실계좌 차이에서는 풀지 않고, 풀기 중에 들어온 정지는 남김, --new-ledger는 결과 모르는 주문을 봉인하고 예산으로 새 장부를 만들며"
            " 다른 계좌·보유가 있으면 하지 않음",
            case_live_resume,
        ),
        (
            "연습 운용: 주문 생성·취소 없이 연습 장부에 그 봉 종가로 체결, 실장부로 넘어가지 않음, 그날 중 설정을 바꿔도 그날 방식 그대로, 실장부는 두 종목 0주·대기 없음·"
            "매수 가능 금액 ≥ 예산일 때만, 실장부에 보유가 있으면 연습 운용으로 돌아가지 않음, 다른 쪽이 주문 잠금을 쥐면 주문·풀기 없음",
            case_live_practice,
        ),
        (
            "실주문 점검: 개장 전 OPG 1주 매수 → 취소 확인, 장중 1주 사고팔기(v5c- 번호, 장부에 넣지 않음), 매도 직전 정지면 팔지 않음, 실주문 중·보유가 있으면·"
            "주문 잠금을 못 쥐면 주문하지 않음",
            case_live_check,
        ),
        (
            "목표를 정하지 않는 네 이유(정산 밀림, 상·하한가 없음, 기준가 다름, 차례 봉이 +40초까지 없음)에서 주문 생성 요청이 없고 차례 기록에 이유가 남음,"
            " 차례 기록에 두 종목 가격·봇 현금·보유·자산·실계좌 수량과 대조·한도가 남음",
            case_live_reasons,
        ),
    ],
    "04": [
        ("끝 없는 기간: 여섯째·일곱째 거래일에도 v5-daily가 전 거래일을 정산하고 개장 전 판단을 하며 v5-decide도 판단함, 기간 끝·기간 종료가 나오지 않음", case_run_open_period),
        ("보고서: 실장부의 현금·보유·마감 봇 자산·총수익·수수료·세금·실주문 수·안전 정지 수, 날마다 모의 계좌·봇 자산 수익률, orders.csv(연습 주문 없음), 판정 없음, 엔진 기간 마지막 날 없음", case_run_report),
    ],
}


def run(prd: str | None = None) -> int:
    results = []
    for prd_id, cases in CASES.items():
        if prd and prd != prd_id:
            continue
        for name, case in cases:
            try:
                with contextlib.redirect_stdout(io.StringIO()):  # 실행기가 차례마다 쓰는 줄은 결과 JSON에 섞지 않는다
                    case()
                results.append({"prd": prd_id, "case": name, "passed": True})
            except Exception as error:
                results.append({"prd": prd_id, "case": name, "passed": False, "error": f"{type(error).__name__}: {error}"})
    passed = sum(result["passed"] for result in results)
    summary = {"total": len(results), "passed": passed, "failed": len(results) - passed}
    print(json.dumps({"results": results, "summary": summary}, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1

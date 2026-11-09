"""v6 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import io
import json
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

from rt_trading import cli, codex, toss, v3_serve, v4_news, v6_head, v6_market, v6_paper, v6_run, v6_serve
from rt_trading.toss import KST

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "docs" / "specs" / "v6" / "fixtures"
FAR_FUTURE_MS = 4_102_444_800_000  # 2100-01-01. 가짜 Codex 토큰이 갱신되지 않게 한다


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=KST)


class Clock:
    """가짜 시계. advance로 움직인다."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeToss:
    """미리 적은 응답을 돌려주는 가짜 토스. 요청 열쇠를 기록한다.

    routes는 열쇠(경로, 쿼리에 date·marketCountry·symbol이 있으면 "?이름=값"을 붙인다) → 응답 목록이고 마지막 응답을 되풀이한다.
    응답은 {"status", "body", "headers", "elapsed"}(status 기본 200) 또는 "network"이고, 받을 때마다 시계를 elapsed초만큼 움직인다.
    """

    def __init__(self, routes: dict, clock: Clock) -> None:
        self.routes, self.clock = routes, clock
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path + "".join(f"?{name}={request.url.params[name]}" for name in ("date", "marketCountry", "symbol") if name in request.url.params)
        self.requests.append(key)
        responses = self.routes[key]
        response = responses.pop(0) if len(responses) > 1 else responses[0]
        if response == "network":
            raise httpx.ConnectError("가짜 연결 오류", request=request)
        self.clock.advance(response.get("elapsed", 0))
        return httpx.Response(response.get("status", 200), json=response.get("body"), headers=response.get("headers"))


@contextlib.contextmanager
def _toss(routes: dict, clock: Clock):
    """가짜 토스와 임시 폴더의 유효한 토큰 파일로 (http, token, fake)를 준다. toss의 기다리기는 가짜 시계를 움직인다."""
    fake = FakeToss(routes, clock)
    with tempfile.TemporaryDirectory() as name, mock.patch.object(toss, "_sleep", clock.advance):
        path = Path(name) / "toss" / "token.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"access_token": "v6-fake-token", "expires_at": (clock() + timedelta(days=1)).isoformat()}), encoding="utf-8")
        with httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(fake)) as http:
            yield http, toss.SharedToken(http, ("v6-fake-id", "v6-fake-secret"), path, clock), fake


# PRD 01 시장 세션과 후보 종목


def _calendar_body(fixture: dict, market: str, days: dict[str, str]) -> dict:
    """fixture의 표준 시각·휴장일·바꾼 시각으로 토스 장 운영 정보 응답 본문을 만든다."""
    result = {}
    for key, day in days.items():
        holiday = day in fixture["holidays"][market]
        if market == "KR":
            times = fixture["kr_standard"] | fixture["overrides"]["KR"].get(day, {})
            spans = {field: {"startTime": f"{day}T{start}:00+09:00", "endTime": f"{day}T{end}:00+09:00"} for field, (start, end) in times.items()}
            result[key] = {"date": day, "integrated": None if holiday else spans}
        else:
            times = fixture["us_standard"] | fixture["overrides"]["US"].get(day, {})
            base = Date.fromisoformat(day)
            spans = {
                field: {
                    "startTime": f"{base + timedelta(days=start_day)}T{start}:00+09:00",
                    "endTime": f"{base + timedelta(days=end_day)}T{end}:00+09:00",
                }
                for field, (start_day, start, end_day, end) in times.items()
            }
            result[key] = {"date": day, **{field: None if holiday else span for field, span in spans.items()}}
    return {"result": result}


def case_sessions() -> None:
    fixture = _fixture("prd01-market.json")["sessions"]
    clock = Clock(_time(fixture["now"]))
    routes = {
        f"/api/v1/market-calendar/{item['market']}?date={item['query']}": [{"body": _calendar_body(fixture, item["market"], item["days"])}]
        for item in fixture["responses"]
    }
    calendar = {"KR": {}, "US": {}}
    with _toss(routes, clock) as (http, token, fake):
        for item in fixture["responses"]:
            day = item["query"] if item["market"] == "KR" else (Date.fromisoformat(item["query"]) + timedelta(days=1)).isoformat()
            fetched = v6_market.fetch_calendar(http, token, item["market"], day, clock() + timedelta(minutes=1), clock, [])
            v6_market.merge_calendar(calendar, item["market"], fetched)
        saved = json.dumps(calendar, sort_keys=True)
        fake.routes[f"/api/v1/market-calendar/KR?date={fixture['failing']}"] = [{"status": 500, "body": {"error": {"code": "internal-error"}}}]
        try:
            v6_market.fetch_calendar(http, token, "KR", fixture["failing"], clock() + timedelta(minutes=1), clock, [])
            raise AssertionError("받지 못한 장 운영 정보가 오류가 아니다")
        except v6_market.V6Error as error:
            assert error.code == "V6_TOSS_FAILED", error
        assert json.dumps(calendar, sort_keys=True) == saved, "받지 못했는데 저장한 장 운영 정보가 바뀌었다"
    for at, expected in fixture["expected_sessions"].items():
        assert v6_market.sessions(calendar, _time(at)) == expected, (at, v6_market.sessions(calendar, _time(at)))
    for day, expected in fixture["expected_slots"].items():
        assert [list(item) for item in v6_market.slots(calendar, day)] == expected, (day, v6_market.slots(calendar, day))
    for item in fixture["tradable"]:
        market = item["info"]["market"]
        for session, expected in item["expect"].items():
            open_at = {"KR": None, "US": None} | {market: None if session == "closed" else session}
            assert v6_market.tradable(item["info"], open_at) is expected, (item["info"], session)
    for item in fixture["fill_boundaries"]:
        at_t = v6_market.sessions(calendar, _time(f"{item['day']}T{item['slot']}:00"))[item["info"]["market"]]
        assert at_t == item["at_t"], (item, at_t)
        open_at = v6_market.sessions(calendar, v6_market.fill_time(item["day"], item["slot"]))
        assert v6_market.tradable(item["info"], open_at) is item["tradable_now"], (item, open_at)


def case_candidates() -> None:
    fixture = _fixture("prd01-market.json")["candidates"]
    clock = Clock(_time(fixture["now"]))
    rankings_body = {
        market: {"result": {"rankedAt": clock().isoformat(), "rankings": [{"rank": rank, "symbol": symbol, "price": {"changeRate": rate}} for rank, symbol, rate in items]}}
        for market, items in fixture["rankings"].items()
    }
    stocks = [
        {
            "symbol": symbol,
            "name": name,
            "market": market,
            "securityType": kind,
            "status": status,
            "currency": currency,
            "leverageFactor": leverage,
            "koreanMarketDetail": None
            if detail is None
            else dict(zip(("liquidationTrading", "nxtSupported", "krxTradingSuspended", "nxtTradingSuspended"), detail)),
        }
        for symbol, name, market, kind, status, currency, leverage, detail in fixture["stocks"]
    ]
    routes = {
        "/api/v1/rankings?marketCountry=KR": [{"body": rankings_body["KR"]}],
        "/api/v1/rankings?marketCountry=US": [{"body": rankings_body["US"]}],
        "/api/v1/stocks": [{"body": {"result": stocks}}],
    }
    with _toss(routes, clock) as (http, token, _):
        deadline = clock() + timedelta(minutes=1)
        rankings = {market: v6_market.fetch_rankings(http, token, market, deadline, clock, [])[0] for market in ("KR", "US")}
        symbols = list(dict.fromkeys([item["symbol"] for market in ("KR", "US") for item in rankings[market]] + list(fixture["holdings"])))
        infos = v6_market.fetch_instruments(http, token, symbols, deadline, clock, [])
    result = json.loads(json.dumps(v6_market.candidates(rankings, infos, fixture["holdings"], fixture["open_at"]), ensure_ascii=False))
    assert result == fixture["expected"], json.dumps(result, ensure_ascii=False)


# PRD 02 목표 보유 헤드


class FakeCodex:
    """가짜 Codex. 시도마다 [종류, 걸린 초, 응답 글]을 차례로 돌려주고 시계를 움직이며 요청 본문을 기록한다. 종류는 ok, http(500), network다."""

    def __init__(self, clock: Clock, attempts: list[list]) -> None:
        self.clock, self.attempts, self.requests = clock, list(attempts), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        kind, seconds, *text = self.attempts.pop(0)
        self.clock.advance(seconds)
        if kind == "network":
            raise httpx.ConnectError("가짜 연결 오류", request=request)
        if kind == "http":
            return httpx.Response(500, text="오류")
        events = [{"type": "response.output_text.delta", "delta": text[0]}, {"type": "response.completed", "response": {"model": v6_head.MODEL}}]
        return httpx.Response(200, content="".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode())


def _decide(fixture: dict, tmp: Path, slot: str, start: str, attempts: list[list], content: dict, candidates: list[dict], parse_seconds: float = 0):
    """임시 Codex 토큰, 가짜 Codex, 가짜 시계로 판단 하나를 정한다. 검증이 parse_seconds초 걸린 것으로 한다. (decide의 값, 가짜 Codex)."""
    token_dir = tmp / "codex"
    (token_dir / "auth").mkdir(parents=True, exist_ok=True)
    (token_dir / "auth" / "codex.json").write_text(json.dumps(fixture["token"] | {"expires": FAR_FUTURE_MS}), encoding="utf-8")
    clock = Clock(_time(start))
    fake = FakeCodex(clock, attempts)
    parse = v6_head.parse

    def slow(text: str, candidates: list[dict]) -> dict:
        result = parse(text, candidates)
        clock.advance(parse_seconds)
        return result

    with httpx.Client(transport=httpx.MockTransport(fake)) as http, mock.patch.object(v6_head, "parse", slow):
        result = v6_head.decide(tmp / "head", slot, content, {}, candidates, http, lambda: codex.credentials(token_dir), clock, clock.advance)
    return result, fake


def _received(items: dict[str, list]) -> dict[str, tuple[Decimal, datetime]]:
    return {key: (Decimal(value), datetime.fromisoformat(at)) for key, (value, at) in items.items()}


def _news(items: list[dict], cutoff: datetime, now: datetime, credentials: list[str]) -> list[dict] | None:
    """검색어마다 items를 돌려주는 가짜 NAVER API HUB로 받기 마감 C의 뉴스 값을 만든다."""
    with tempfile.TemporaryDirectory() as name, httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"items": items}))) as http:
        news, _ = v4_news.fetch(http, tuple(credentials), cutoff, cutoff, lambda: now, Path(name) / "news.json")
    return news


def case_head_input() -> None:
    fixture = _fixture("prd02-head.json")
    case = fixture["input"]
    _, cutoff, _ = v6_head.times(case["slot"])
    raw = case["state"]
    state = {
        "krw": raw["krw"],
        "usd": Decimal(raw["usd"]),
        "positions": {symbol: item | {"avg_price": Decimal(item["avg_price"])} for symbol, item in raw["positions"].items()},
        "last": _received(raw["last"]),
    }
    holdings = {symbol: item["quantity"] for symbol, item in raw["positions"].items()}

    def content(after: dict, infos: tuple[dict, datetime]) -> tuple[dict, list[dict]]:
        observed = {"prices": _received(case["prices_before"] | after["prices"]), "rate": _received({"rate": after["rate"]})["rate"]}
        rankings = {
            market: ([{"rank": rank, "symbol": symbol, "change_rate": rate} for rank, symbol, rate in items], datetime.fromisoformat(at))
            for market, (items, at) in (("KR", case["kr_rankings"]), ("US", after["us_rankings"]))
        }
        candidates = v6_head.candidates_at(cutoff, rankings, infos, case["saved_infos"], holdings, case["open_at"])
        _, _, summary = v6_head.account(state, observed, cutoff, case["bases"], case["history"], case["blocked"], candidates)
        news = _news(case["news_items"] + [after["late_news"]], cutoff, _time(case["now"]), fixture["naver"])
        return v6_head.build_input(case["slot"], case["open_at"], case["ends_at"], summary, candidates, news, case["decisions"]), candidates

    current = ({info["symbol"]: info for info in case["infos"]}, datetime.fromisoformat(case["infos_observed"]))
    first, candidates = content(case["after_c"][0], current)
    second, _ = content(case["after_c"][1], current)
    assert first == second, "C 뒤에 받은 랭킹·현재가·환율과 C 뒤 기사를 바꿨더니 입력이 바뀌었다"
    plain = json.loads(json.dumps(first, ensure_ascii=False))
    assert plain == case["expected"], json.dumps(plain, ensure_ascii=False)
    late = [
        content(case["after_c"][0], ({info["symbol"]: info for info in variant}, datetime.fromisoformat(case["infos_after_c_observed"])))[0]
        for variant in case["infos_after_c"]
    ]
    assert late[0] == late[1], "C 뒤에 받은 종목 정보를 바꿨더니 입력이 바뀌었다"
    text = json.dumps(first, ensure_ascii=False)
    for secret in [*fixture["secrets"], *fixture["token"].values(), *case["prices"]]:
        assert secret not in text, ("입력", secret)
    with tempfile.TemporaryDirectory() as name:
        (row, _), fake = _decide(fixture, Path(name), case["slot"], case["now"], [["http", 5], ["ok", 5, fixture["response"]["valid"]]], first, candidates)
    sent = [body["input"][0]["content"][0]["text"] for body in fake.requests]
    assert row["status"] == "ok" and len(sent) == 2, (row, len(sent))
    assert sent[0] == sent[1] == text, "두 호출의 입력이 다르다"


def case_head_response() -> None:
    fixture = _fixture("prd02-head.json")
    case = fixture["response"]
    for name, text in case["invalid"].items():
        try:
            v6_head.parse(text, case["candidates"])
        except v6_head.HeadError as error:
            assert error.code == "V6_HEAD_RESPONSE_INVALID", (name, error)
        else:
            raise AssertionError(f"{name}이 형식 오류가 아니다")
    texts = {"valid": case["valid"]} | case["invalid"]
    for name, run in case["runs"].items():
        attempts = [[kind, seconds, *(texts[key] for key in rest)] for kind, seconds, *rest in run["attempts"]]
        with tempfile.TemporaryDirectory() as tmp:
            (row, _), fake = _decide(
                fixture, Path(tmp), case["slot"], case["start"], attempts, {"사례": name}, case["candidates"], run.get("parse_seconds", 0)
            )
        assert (row["status"], len(fake.requests)) == (run["status"], run["requests"]), (name, row, len(fake.requests))
        expected = None if run["targets"] is None else {symbol: Decimal(weight) for symbol, weight in run["targets"].items()}
        assert v6_head.targets(row) == expected, (name, v6_head.targets(row))


# PRD 03 모의 계좌


def _standard_calendar(spec: dict) -> dict:
    """fixture의 국내·미국 영업일에 2026-11-09 기준 세션 시각을 넣은 장 운영 정보. 영업일이 아닌 날은 휴장일이다."""
    calendar, day, last = {"KR": {}, "US": {}}, Date.fromisoformat(spec["first"]), Date.fromisoformat(spec["last"])
    while day <= last:
        text, after = day.isoformat(), (day + timedelta(days=1)).isoformat()
        calendar["KR"][text] = (
            {"pre": [f"{text}T08:00:00+09:00", f"{text}T09:00:00+09:00"], "regular": [f"{text}T09:00:00+09:00", f"{text}T15:30:00+09:00"], "after": [f"{text}T15:30:00+09:00", f"{text}T20:00:00+09:00"]}
            if text in spec["kr_days"]
            else {}
        )
        calendar["US"][text] = (
            {
                "day": [f"{text}T10:00:00+09:00", f"{text}T18:00:00+09:00"],
                "pre": [f"{text}T18:00:00+09:00", f"{text}T23:30:00+09:00"],
                "regular": [f"{text}T23:30:00+09:00", f"{after}T06:00:00+09:00"],
                "after": [f"{after}T06:00:00+09:00", f"{after}T09:50:00+09:00"],
            }
            if text in spec["us_days"]
            else {}
        )
        day += timedelta(days=1)
    return calendar


def _paper_infos(rows: list[list]) -> dict[str, dict]:
    """[종목, 시장, 유형, 배수, NXT 지원(, 상장 상태)]로 instruments 행을 만든다."""
    infos = {}
    for symbol, market, kind, leverage, nxt, *status in rows:
        infos[symbol] = {
            "symbol": symbol, "name": symbol, "market": market, "security_type": kind, "status": status[0] if status else "ACTIVE",
            "currency": "KRW" if market == "KR" else "USD", "leverage": leverage, "nxt_supported": nxt, "nxt_suspended": 0, "krx_suspended": 0,
            "liquidation": 0, "observed_at": "2026-11-12T09:00:00+09:00",
        }
    return infos


def _paper_ledger(conn: sqlite3.Connection, day: str, raw: dict, infos: dict[str, dict], base: int | None = None) -> None:
    """시작일 day로 장부를 만들고 fixture의 현금·보유와 종목 정보를 넣는다. base가 있으면 기준을 그 값으로 바꾼다."""
    v6_paper.start(conn, day)
    if base is not None:
        conn.execute("UPDATE baselines SET equity = ?", (base,))
    conn.execute("UPDATE cash SET amount = ? WHERE currency = 'KRW'", (str(raw["krw"]),))
    conn.execute("UPDATE cash SET amount = ? WHERE currency = 'USD'", (raw["usd"],))
    for symbol, (quantity, average) in raw["positions"].items():
        conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?)", (symbol, infos[symbol]["market"], infos[symbol]["currency"], quantity, average))
    v6_paper.save_instruments(conn, infos)


def _paper_book(spec: dict, at: datetime) -> dict:
    """체결 시각 at에서 요청·받은·데이터 시각을 초로 적은 호가(기본 +1초, +2초, +0초)."""
    stamp = spec.get("timestamp", 0)
    return {
        "requested_at": at + timedelta(seconds=spec.get("requested", 1)),
        "observed_at": at + timedelta(seconds=spec.get("observed", 2)),
        "timestamp": None if stamp is None else at + timedelta(seconds=stamp),
        "asks": [(Decimal(price), volume) for price, volume in spec.get("asks", [])],
        "bids": [(Decimal(price), volume) for price, volume in spec.get("bids", [])],
    }


def _same_value(actual: object, expected: object) -> bool:
    """수로 읽히는 글자는 수로 비교한다(0.00과 0은 같다)."""
    if isinstance(expected, str) and isinstance(actual, str):
        try:
            return Decimal(actual) == Decimal(expected)
        except ArithmeticError:
            return actual == expected
    return actual == expected


def _paper_rebalance(fixture: dict, case: dict) -> None:
    """판단 체결 하나를 돌려 체결 행·보유·현금을 손 계산과 비교한다."""
    calendar, infos, at = _standard_calendar(fixture["calendar"]), _paper_infos(fixture["instruments"]), _time(case["at"])
    with closing(sqlite3.connect(":memory:")) as conn:
        _paper_ledger(conn, at.date().isoformat(), case["state"], infos, 100000)
        used = {key: (Decimal(value), at, True) for key, value in case["c_used"].items()}
        v6_paper.record_valuation(conn, v6_head.times(case["turn"])[1].strftime("%Y-%m-%d %H:%M"), "c", at, case["a_c"], v6_paper.state(conn), used, [])
        targets = {symbol: Decimal(weight) for symbol, weight in case["targets"].items()}
        rate = None if case["rate_c"] is None else Decimal(case["rate_c"])
        books = {symbol: _paper_book(spec, at) for symbol, spec in case["books"].items()}
        v6_paper.rebalance(conn, case["turn"], at, v6_market.sessions(calendar, at), targets, case["a_c"], rate, books, False, calendar)
        fills = [dict(zip(v6_paper.FILL_COLUMNS, row)) for row in conn.execute(f"SELECT {', '.join(v6_paper.FILL_COLUMNS)} FROM fills ORDER BY id")]
        ledger = v6_paper.state(conn)
    expected = case["expected"]
    assert len(fills) == len(expected["fills"]), (case.get("name"), [(fill["symbol"], fill["side"], fill["quantity"]) for fill in fills])
    for fill, want in zip(fills, expected["fills"]):
        for key, value in want.items():
            actual = json.loads(fill[key]) if key == "levels" else fill[key]
            same = [[Decimal(p), q] for p, q in actual] == [[Decimal(p), q] for p, q in value] if key == "levels" else _same_value(actual, value)
            assert same, (case.get("name"), fill["symbol"], key, actual, value)
    positions = {symbol: [item["quantity"], item["avg_price"]] for symbol, item in ledger["positions"].items()}
    assert positions.keys() == expected["positions"].keys(), (case.get("name"), positions)
    for symbol, (quantity, average) in expected["positions"].items():
        assert positions[symbol][0] == quantity and positions[symbol][1] == Decimal(average), (case.get("name"), symbol, positions[symbol])
    assert (ledger["krw"], ledger["usd"]) == (expected["krw"], Decimal(expected["usd"])), (case.get("name"), ledger["krw"], ledger["usd"])


def case_paper_fills() -> None:
    fixture = _fixture("prd03-paper.json")
    observe = fixture["observe"]
    clock, fill_at = Clock(_time(observe["now"])), _time(observe["fill_at"])
    routes = {"/api/v1/exchange-rate": [{"body": {"result": observe["rate"]}}]}
    routes |= {f"/api/v1/orderbook?symbol={symbol}": [{"elapsed": item["elapsed"], "body": {"result": item["body"]}}] for symbol, item in observe["books"].items()}
    with _toss(routes, clock) as (http, token, _):
        rate, _ = v6_paper.observe_rate(http, token, clock() + timedelta(minutes=1), clock, [])
        books = v6_paper.observe_books(http, token, sorted(observe["books"]), fill_at, fill_at + v6_paper.BOOK_WAIT, clock, clock.advance, [])
    assert rate == Decimal(observe["rate"]["midRate"]), rate
    assert set(books) == {"100001"}, sorted(books)
    book = books["100001"]
    assert book["requested_at"] >= fill_at and v6_paper.usable(book, fill_at), book
    got = {side: [[str(price), volume] for price, volume in book[side]] for side in ("asks", "bids")}
    assert got == observe["expected_100001"], got
    for case in fixture["fills"]:
        _paper_rebalance(fixture, case)


def case_paper_cap() -> None:
    fixture = _fixture("prd03-paper.json")
    _paper_rebalance(fixture, fixture["cap"] | {"name": "닫힌 시장 보유 1종목과 새 목표 5종목"})


def case_paper_limits() -> None:
    fixture = _fixture("prd03-paper.json")
    limits, calendar, infos = fixture["limits"], _standard_calendar(fixture["calendar"]), _paper_infos(fixture["instruments"])
    at = _time("2026-11-17T12:00:00")
    for case in limits["boundaries"]:
        with closing(sqlite3.connect(":memory:")) as conn:
            _paper_ledger(conn, "2026-11-16", {"krw": 100000, "usd": "0", "positions": {"100001": [1, "10000"]}}, infos)
            conn.execute("INSERT INTO baselines VALUES ('daily', '2026-11-17', ?, 'valuation')", (case["daily"],))
            conn.execute("UPDATE baselines SET equity = ? WHERE kind = 'weekly' AND key = '2026-11-16'", (case["weekly"],))
            v6_paper.check(conn, case["equity"], at)
            got = [list(row) for row in conn.execute("SELECT kind, key FROM limits ORDER BY kind")]
            started = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'forced_start'").fetchone()[0]
        assert got == case["limits"] and started == bool(case["limits"]), (case, got, started)
    forced = limits["forced"]
    with closing(sqlite3.connect(":memory:")) as conn:
        _paper_ledger(conn, "2026-11-16", forced["state"], infos)
        conn.execute("INSERT INTO limits VALUES (?, ?, ?, 1)", (*forced["limit"], forced["started"]))
        conn.execute("INSERT INTO events VALUES (?, 'forced_start', 'daily')", (forced["started"],))
        for step in forced["steps"]:
            if "buy" in step:  # 강제 매도 중의 판단: 막힌 매수는 체결되지 않는다
                buy, buy_at = step["buy"], _time(step["buy"]["at"])
                used = {"UST8": (Decimal("10.00"), buy_at, True), "USDKRW": (Decimal("1350.00"), buy_at, True)}
                v6_paper.record_valuation(conn, v6_head.times(buy["turn"])[1].strftime("%Y-%m-%d %H:%M"), "c", buy_at, 0, v6_paper.state(conn), used, [])
                blocked = bool(v6_paper.blocked_reasons(conn, buy_at))
                targets = {symbol: Decimal(weight) for symbol, weight in buy["targets"].items()}
                books = {"US01": _paper_book({"asks": buy["asks"]}, buy_at)}
                v6_paper.rebalance(conn, buy["turn"], buy_at, v6_market.sessions(calendar, buy_at), targets, 1_000_000, Decimal("1350.00"), books, blocked, calendar)
                bought = conn.execute("SELECT COUNT(*) FROM fills WHERE side = 'BUY'").fetchone()[0]
                assert blocked and bought == 0 and "US01" not in v6_paper.state(conn)["positions"], (blocked, bought)
                continue
            step_at = _time(step["at"])
            used = {"UST8": (Decimal("10.00"), step_at, True), "USDKRW": (Decimal("1350.00"), step_at, True)}
            v6_paper.record_valuation(conn, step["turn"], "watch", step_at, 0, v6_paper.state(conn), used, [])
            before = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
            book = _paper_book({"bids": step["bids"]}, step_at)
            v6_paper.force(conn, step["turn"], step_at, v6_market.sessions(calendar, step_at), {"UST8": book})
            sold = sum(row[0] for row in conn.execute("SELECT quantity FROM fills WHERE rowid > ?", (before,)))
            left = v6_paper.state(conn)["positions"].get("UST8", {}).get("quantity", 0)
            assert (sold, left, "forced_sale" in v6_paper.blocked_reasons(conn, step_at)) == (step["sold"], step["left"], step["forced"]), (step, sold, left)
        assert v6_paper.state(conn)["usd"] == Decimal(forced["usd"]), v6_paper.state(conn)["usd"]
    case = limits["bases"]
    with closing(sqlite3.connect(":memory:")) as conn:
        _paper_ledger(conn, case["start"], {"krw": 500000, "usd": "0", "positions": {}}, infos)
        for slot, equity in case["valuations"].items():
            v6_paper.record_valuation(conn, slot, "watch", _time(slot.replace(" ", "T") + ":00"), equity, v6_paper.state(conn), {}, [])
        for step in case["steps"]:
            step_at = _time(step["at"])
            v6_paper.bases(conn, step_at)
            day = step_at.date().isoformat()
            daily = conn.execute("SELECT equity, source FROM baselines WHERE kind = 'daily' AND key = ?", (day,)).fetchone()
            assert (list(daily) if daily else None) == step["daily"], (step, daily)
            if "weekly" in step:
                weekly = conn.execute("SELECT equity, source FROM baselines WHERE kind = 'weekly' AND key = ?", (day,)).fetchone()
                assert list(weekly) == step["weekly"], (step, weekly)
            if "blocked" in step:
                assert v6_paper.blocked_reasons(conn, _time(step["blocked_at"])) == step["blocked"], (step, v6_paper.blocked_reasons(conn, _time(step["blocked_at"])))


# PRD 04 실행과 보고


def _stocks_body(rows: list[list]) -> dict:
    keys = ("liquidationTrading", "nxtSupported", "krxTradingSuspended", "nxtTradingSuspended")
    return {
        "result": [
            {"symbol": symbol, "name": name, "market": market, "securityType": kind, "status": status, "currency": currency, "leverageFactor": leverage,
             "koreanMarketDetail": None if detail is None else dict(zip(keys, detail))}
            for symbol, name, market, kind, status, currency, leverage, detail in rows
        ]
    }


def _run_paths(tmp: Path, fixture: dict) -> v6_run.Paths:
    """임시 data/v6에 시작일 설정과 표준 장 운영 정보를 둔다."""
    paths = v6_run.Paths(tmp / "data" / "v6", tmp / "data" / "reports")
    paths.root.mkdir(parents=True)
    paths.config.write_text(json.dumps({"start": fixture["start"]}), encoding="utf-8")
    v6_market.save_calendar(paths.calendar, _standard_calendar(fixture["calendar"]))
    return paths


@contextlib.contextmanager
def _run_http(fixture: dict, routes: dict, clock: Clock, attempts: list[list], tmp: Path):
    """토스·Codex·네이버 가짜를 주소로 나눠 쓰는 http와 함께 쓰는 토큰, Codex 인증 함수, (가짜 토스, 가짜 Codex)."""
    toss_fake, codex_fake = FakeToss(routes, clock), FakeCodex(clock, attempts)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "chatgpt.com":
            return codex_fake(request)
        if "naver" in request.url.host:
            return httpx.Response(200, json={"items": []})
        return toss_fake(request)

    token_dir = tmp / "codex"
    (token_dir / "auth").mkdir(parents=True, exist_ok=True)
    (token_dir / "auth" / "codex.json").write_text(json.dumps(fixture["run"]["codex_token"] | {"expires": FAR_FUTURE_MS}), encoding="utf-8")
    token_path = tmp / "toss" / "token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps({"access_token": "v6-run-token", "expires_at": (clock() + timedelta(days=30)).isoformat()}), encoding="utf-8")
    with mock.patch.object(toss, "_sleep", lambda seconds: None), httpx.Client(base_url=toss.BASE_URL, transport=httpx.MockTransport(handler)) as http:
        yield http, toss.SharedToken(http, ("id", "secret"), token_path, clock), lambda: codex.credentials(token_dir), (toss_fake, codex_fake)


def _run_routes(fixture: dict) -> dict:
    run = fixture["run"]
    routes = {
        "/api/v1/exchange-rate": [{"body": {"result": run["rate"]}}],
        "/api/v1/stocks": [{"body": _stocks_body(run["stocks"])}],
        "/api/v1/orderbook?symbol=100001": [{"body": {"result": run["book"]}}],
        "/api/v1/orderbook?symbol=100002": [{"status": 404, "body": {"error": {"code": "not-found"}}}],
    }
    for market, items in run["rankings"].items():
        routes[f"/api/v1/rankings?marketCountry={market}"] = [
            {"body": {"result": {"rankedAt": "2026-11-17T10:40:00+09:00", "rankings": [{"rank": rank, "symbol": symbol, "price": {"changeRate": rate}} for rank, symbol, rate in items]}}}
        ]
    return routes


def _prepare_run(paths: v6_run.Paths, http: httpx.Client, token: toss.SharedToken, clock: Clock, midnight_equity: int = 500000) -> None:
    """장부를 만들고 그날 00:00 평가를 넣어 하루 기준이 평가에서 오게 한다."""
    v6_run.prepare(paths, http, token, clock, [])
    with v6_run.ledger(paths) as conn:
        v6_paper.record_valuation(conn, "2026-11-17 00:00", "watch", _time("2026-11-17T00:00:05"), midnight_equity, v6_paper.state(conn), {}, [])


def case_run_decide() -> None:
    fixture = _fixture("prd04-run.json")
    run = fixture["run"]
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        paths, clock = _run_paths(tmp, fixture), Clock(_time("2026-11-17T10:40:05"))
        with _run_http(fixture, _run_routes(fixture), clock, [["ok", 5, run["reply"]]], tmp) as (http, token, credentials, (_, codex_fake)):
            _prepare_run(paths, http, token, clock)
            assert v6_run.decide(paths, "10:40", http, token, ("id", "secret"), credentials, clock, clock.advance) == 0
            clock.now = _time("2026-11-17T10:45:10")  # 호가를 아직 받을 수 있는 E + 40초 안에 다시 실행한다
            assert v6_run.decide(paths, "10:40", http, token, ("id", "secret"), credentials, clock, clock.advance) == 0
            clock.now = _time("2026-11-17T11:46:00")
            assert v6_run.decide(paths, "11:40", http, token, ("id", "secret"), credentials, clock, clock.advance) == 0
            assert len(codex_fake.requests) == 1, "같은 차례를 두 번 실행했거나 E가 지난 차례에 Codex를 불렀다"
        with closing(sqlite3.connect(paths.paper)) as conn:
            decisions = dict(conn.execute("SELECT time, status || ':' || error FROM decisions"))
            fills = [dict(zip(v6_paper.FILL_COLUMNS, row)) for row in conn.execute(f"SELECT {', '.join(v6_paper.FILL_COLUMNS)} FROM fills")]
            kinds = [row for row in conn.execute("SELECT slot, kind FROM valuations WHERE slot >= '2026-11-17 10' ORDER BY slot")]
        assert decisions == {"2026-11-17 10:40": "ok:", "2026-11-17 11:40": "missing:V6_STARTED_LATE"}, decisions
        assert len(fills) == 1 and all(_same_value(fills[0][key], value) for key, value in run["expected_fill"].items()), fills
        assert kinds == [("2026-11-17 10:40", "watch"), ("2026-11-17 10:42", "c")], kinds
        assert (paths.head / "2026-11-17" / "1040" / "orderbook.json").exists()
    with tempfile.TemporaryDirectory() as name:  # 같은 차례를 두 프로세스가 함께 실행해도 판단·C 평가·체결이 하나씩이다
        tmp = Path(name)
        paths, clock = _run_paths(tmp, fixture), Clock(_time("2026-11-17T10:40:05"))
        with _run_http(fixture, _run_routes(fixture), clock, [["ok", 5, run["reply"]]], tmp) as (http, token, credentials, (_, codex_fake)):
            _prepare_run(paths, http, token, clock)
            errors = []

            def both() -> None:
                try:
                    v6_run.decide(paths, "10:40", http, token, ("id", "secret"), credentials, clock, clock.advance)
                except Exception as error:
                    errors.append(repr(error))

            workers = [threading.Thread(target=both) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        with closing(sqlite3.connect(paths.paper)) as conn:
            counts = [conn.execute(query).fetchone()[0] for query in ("SELECT COUNT(*) FROM decisions", "SELECT COUNT(*) FROM valuations WHERE kind = 'c'", "SELECT COUNT(*) FROM fills")]
        assert not errors and counts == [1, 1, 1] and len(codex_fake.requests) == 1, (errors, counts, len(codex_fake.requests))


def case_run_limit_first() -> None:
    fixture = _fixture("prd04-run.json")
    run, limit = fixture["run"], fixture["run"]["limit"]
    routes = _run_routes(fixture) | {"/api/v1/prices": [{"body": {"result": [{"symbol": "100002", "lastPrice": limit["price"], "currency": "KRW"}]}}]}
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        paths, clock = _run_paths(tmp, fixture), Clock(_time("2026-11-17T10:40:05"))
        with _run_http(fixture, routes, clock, [["ok", 5, run["reply"]]], tmp) as (http, token, credentials, _):
            _prepare_run(paths, http, token, clock)
            with v6_run.ledger(paths) as conn:
                v6_paper.save_instruments(conn, v6_market.fetch_instruments(http, token, ["100002"], clock() + timedelta(minutes=1), clock, []))
                conn.execute("UPDATE cash SET amount = '0' WHERE currency = 'KRW'")
                conn.execute("INSERT INTO positions VALUES ('100002', 'KR', 'KRW', ?, ?)", (limit["quantity"], limit["average"]))
                conn.execute("INSERT INTO last_values VALUES ('100002', ?, '2026-11-17T10:30:00+09:00')", (limit["average"],))
            assert v6_run.decide(paths, "10:40", http, token, ("id", "secret"), credentials, clock, clock.advance) == 0
        with closing(sqlite3.connect(paths.paper)) as conn:
            buys = conn.execute("SELECT COUNT(*) FROM fills WHERE side = 'BUY'").fetchone()[0]
            watch_at, c_at = (conn.execute("SELECT at FROM valuations WHERE slot = ?", (slot,)).fetchone()[0] for slot in ("2026-11-17 10:40", "2026-11-17 10:42"))
        blocked = json.loads((paths.head / "2026-11-17" / "1040" / "input.json").read_text(encoding="utf-8"))["input"]["account"]["buy_blocked"]
        assert buys == 0 and watch_at <= c_at and blocked == limit["reasons"], (buys, watch_at, c_at, blocked)


def case_run_serialized() -> None:
    fixture = _fixture("prd04-run.json")
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        paths, clock = _run_paths(tmp, fixture), Clock(_time("2026-11-17T10:46:00"))
        routes = _run_routes(fixture) | {"/api/v1/prices": [{"body": {"result": []}}]}
        with _run_http(fixture, routes, clock, [], tmp) as (http, token, _, _):
            _prepare_run(paths, http, token, clock)
            with v6_run.ledger(paths) as conn:
                v6_paper.save_instruments(conn, v6_market.fetch_instruments(http, token, ["100001", "100002"], clock() + timedelta(minutes=1), clock, []))
                conn.execute("INSERT INTO positions VALUES ('100002', 'KR', 'KRW', 1, '5000')")
                conn.execute("INSERT INTO last_values VALUES ('100002', '5000', '2026-11-17T10:46:00+09:00')")
                conn.execute("INSERT INTO events VALUES ('2026-11-17T10:30:00+09:00', 'forced_start', 'daily')")
                v6_paper.record_valuation(conn, "2026-11-17 10:42", "c", clock(), 500000, v6_paper.state(conn), {}, [])
            spans, calendar = [], v6_market.load_calendar(paths.calendar)

            def slow(original):
                def wrapped(*args, **kwargs):
                    begun = time.monotonic()
                    time.sleep(0.3)
                    original(*args, **kwargs)
                    spans.append((begun, time.monotonic()))

                return wrapped

            with mock.patch.object(v6_paper, "rebalance", slow(v6_paper.rebalance)), mock.patch.object(v6_paper, "force", slow(v6_paper.force)):
                workers = [
                    threading.Thread(target=v6_run._fill, args=(paths, http, token, clock, lambda seconds: None, [], calendar, "2026-11-17 10:40", {"100001": Decimal("0.1")})),
                    threading.Thread(target=v6_run._watch, args=(paths, http, token, clock, lambda seconds: None, [], calendar, _time("2026-11-16T00:00:00"), _time("2026-12-01T00:00:00"), _time("2026-11-17T10:50:00"))),
                ]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()
        assert len(spans) == 2, spans
        (first_start, first_end), (second_start, _) = sorted(spans)
        assert first_end <= second_start, "강제 매도와 판단 체결이 장부를 동시에 바꿨다"


def case_run_rate_limit() -> None:
    clock = Clock(_time("2026-11-17T10:40:05"))
    limited = {"status": 429, "headers": {"Retry-After": "2"}, "body": {"error": {"code": "rate-limit-exceeded"}}}
    for responses, ok in (([limited, limited, {"body": {"result": []}}], False), ([limited, {"body": {"result": []}}], True)):
        with _toss({"/api/v1/prices": list(responses)}, clock) as (http, token, fake):
            try:
                v6_market.request(http, token, "/api/v1/prices", {"symbols": "100001"}, clock() + timedelta(minutes=1), clock, [])
                got = True
            except v6_market.V6Error:
                got = False
            assert (got, len(fake.requests)) == (ok, 2), (ok, got, len(fake.requests))


def _period_record(paths: v6_run.Paths, fixture: dict, change: Callable[[dict], None] | None = None) -> None:
    """시작일부터 끝 시각까지 모든 판단 ok, 모든 10분 평가 500,000원, 정상 체결 몇 개인 장부를 만들고 change로 바꿀 곳만 바꾼다."""
    verdict, calendar = fixture["verdict"], v6_market.load_calendar(paths.calendar)
    names = fixture["verdict"]["strategies"]["names"]
    record = {"decisions": {}, "c": {}, "watch": {}, "fills": [], "calendar": calendar}
    day = Date.fromisoformat(fixture["start"])
    end = datetime.fromisoformat(fixture["end"])
    index = 0
    while day < end.date():
        for clock_text, _ in v6_market.slots(calendar, day.isoformat()):
            slot = f"{day} {clock_text}"
            record["decisions"][slot] = {"status": "ok", "strategy": names[index % 2]}
            record["c"][v6_head.times(slot)[1].strftime("%Y-%m-%d %H:%M")] = 500000 + verdict["strategies"]["step"] * index
            index += 1
        day += timedelta(days=1)
    moment = _time(f"{fixture['start']}T00:00:00")
    while moment <= end:
        record["watch"][moment.strftime("%Y-%m-%d %H:%M")] = [500000, 0]
        moment += timedelta(minutes=10)
    record["watch"][verdict["after_end_slot"]] = [500000, 0]
    for item in verdict["fills"]:
        record["fills"].append(
            {
                "at": item["at"], "origin": "decision", "turn": item["turn"], "symbol": "100001", "market": "KR", "session": "regular", "side": "BUY",
                "quantity": 1, "levels": "[]", "amount": "10000", "currency": "KRW", "commission": "1", "tax": "0", "risk_cost": "0", "sec_fee": "0",
                "fx_usd": "0", "fx_krw": "0", "fx_rate": "", "fx_cost_rate": "", "realized": "0", "eligible": 1, "tradable": 1, "blocked": 0,
                "krw_after": "489999", "usd_after": "0", "holdings_after": 1,
            }
        )
    if change:
        change(record)
    v6_market.save_calendar(paths.calendar, record["calendar"])
    with v6_run.ledger(paths) as conn:
        v6_paper.start(conn, fixture["start"])
        conn.executemany("INSERT INTO meta VALUES (?, ?)", [("start", fixture["start"]), ("end", fixture["end"])])
        for slot, item in record["decisions"].items():
            row = {column: "" for column in v6_head.COLUMNS} | {"time": slot, "positions": "[]", "attempts": 1} | item
            conn.execute(f"INSERT INTO decisions VALUES ({', '.join('?' * len(v6_head.COLUMNS))})", [row[column] for column in v6_head.COLUMNS])
        conn.executemany("INSERT INTO valuations VALUES (?, 'c', ?, ?, '0', '0', '', '{}', 0, '')", [(slot, slot, equity) for slot, equity in record["c"].items()])
        conn.executemany("INSERT INTO valuations VALUES (?, 'watch', ?, ?, '0', '0', '', '{}', ?, '')", [(slot, slot, equity, stale) for slot, (equity, stale) in record["watch"].items()])
        for fill in record["fills"]:
            conn.execute(f"INSERT INTO fills ({', '.join(v6_paper.FILL_COLUMNS)}) VALUES ({', '.join('?' * len(v6_paper.FILL_COLUMNS))})", [fill[column] for column in v6_paper.FILL_COLUMNS])


def _report(fixture: dict, change: Callable[[dict], None] | None = None, now: str | None = None) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory() as name:
        paths = _run_paths(Path(name), fixture)
        _period_record(paths, fixture, change)
        return v6_run.build(paths, _time(now or fixture["verdict"]["now_after"]))


def case_run_period() -> None:
    fixture = _fixture("prd04-run.json")
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        paths, clock = _run_paths(tmp, fixture), Clock(_time("2026-11-16T09:00:00"))
        with _run_http(fixture, {}, clock, [], tmp) as (http, token, _, _):
            start, end, _ = v6_run.prepare(paths, http, token, clock, [])
            assert end == datetime.fromisoformat(fixture["end"]), end
            with v6_run.ledger(paths) as conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('calendar_day', '2026-12-01')")
            clock.now = _time("2026-12-01T00:00:00")
            assert v6_run.watch(paths, http, token, clock, clock.advance) == 0
            clock.now = _time("2026-12-01T00:10:00")
            assert v6_run.watch(paths, http, token, clock, clock.advance) == 0
            assert v6_run.decide(paths, "00:40", http, token, None, lambda: ("a", "b"), clock, clock.advance) == 0
        with closing(sqlite3.connect(paths.paper)) as conn:
            slots = [slot for (slot,) in conn.execute("SELECT slot FROM valuations WHERE slot >= '2026-12-01 00:00'")]
            decided = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        assert slots == ["2026-12-01 00:00"] and decided == 0, ("끝 시각 평가만 있어야 한다", slots, decided)
    content, tables = _report(fixture)
    assert content["period"]["trading_days"] == fixture["trading_days"] and content["decisions"]["slots"] == fixture["slots"], content["period"]
    assert content["criteria"]["valuations"]["expected"] == fixture["watch_slots"] and content["verdict"] == "통과", content["criteria"]
    fills, equity = tables["fills.csv"][1], tables["equity.csv"][1]
    assert len(fills) == fixture["verdict"]["in_period_fills"] and fills[-1][0] == fixture["verdict"]["fills"][1]["at"], fills
    assert equity[-1][0] == "2026-12-01 00:00" and all(row[0] != fixture["verdict"]["after_end_slot"] for row in equity), equity[-1]


def case_run_verdict() -> None:
    fixture = _fixture("prd04-run.json")
    verdict = fixture["verdict"]
    slots = list(_report(fixture)[0]["decisions"]["by_status"])

    def fail_first(count: int):
        def change(record: dict) -> None:
            for slot in list(record["decisions"])[:count]:
                record["decisions"][slot] = {"status": "failed", "strategy": ""}

        return change

    def drawdown(equity: int):
        return lambda record: record["watch"].__setitem__("2026-11-20 12:00", [equity, 0])

    def drop(count: int):
        def change(record: dict) -> None:
            removable = [slot for slot in record["watch"] if slot.endswith(":10")][:count]
            for slot in removable:
                del record["watch"][slot]

        return change

    checks = [
        ("판단 실패 정확히 10%", fail_first(verdict["failures_fail"]), "failures", False),
        ("판단 실패 10%보다 적음", fail_first(verdict["failures_pass"]), "failures", True),
        ("최대 낙폭 정확히 20%", drawdown(verdict["drawdown_fail"]), "max_drawdown", False),
        ("최대 낙폭 20%보다 작음", drawdown(verdict["drawdown_pass"]), "max_drawdown", True),
        ("10분 평가 90% 이상", drop(fixture["watch_slots"] - verdict["valuations_pass"]), "valuations", True),
        ("10분 평가 90%보다 하나 적음", drop(fixture["watch_slots"] - verdict["valuations_fail"]), "valuations", False),
        ("00:00 평가가 빠진 날", lambda record: record["watch"].pop(verdict["missing_midnight"]), "midnight", False),
        ("stale 평가", lambda record: record["watch"].__setitem__("2026-11-25 03:20", [500000, 1]), "stale", False),
    ]
    for rule, values in verdict["ledger"].items():
        checks.append((f"장부 규칙 {rule}", lambda record, values=values: record["fills"][0].update(values), "ledger", False))
    for name, change, key, passed in checks:
        content, _ = _report(fixture, change)
        assert content["criteria"][key]["passed"] is passed, (name, content["criteria"][key])
        assert content["verdict"] == ("통과" if passed else "불통과"), (name, content["verdict"])
    content, _ = _report(fixture, lambda record: record["calendar"]["KR"].pop(verdict["missing_calendar_day"]))
    assert (content["decisions"]["slots"], content["decisions"]["by_status"]["missing"]) == (verdict["missing_calendar_slots"], 24), content["decisions"]
    assert _report(fixture, now=verdict["now_before"])[0]["verdict"] is None, "끝 시각 전에 판정이 있다"
    assert slots == list(v6_run.STATUSES), slots
    mid, expected = _time(verdict["now_mid"]), verdict["mid"]

    def until(record: dict) -> None:
        record["decisions"] = {slot: item for slot, item in record["decisions"].items() if v6_head.times(slot)[2] <= mid}
        record["watch"] = {slot: item for slot, item in record["watch"].items() if _time(slot.replace(" ", "T") + ":00") <= mid}
        record["fills"] = [fill for fill in record["fills"] if datetime.fromisoformat(fill["at"]) <= mid]

    content, _ = _report(fixture, until, now=verdict["now_mid"])
    valuations, midnight = content["criteria"]["valuations"], content["criteria"]["midnight"]
    assert content["verdict"] is None and (content["decisions"]["slots"], content["decisions"]["by_status"]["missing"]) == (expected["slots"], 0), content["decisions"]
    assert (valuations["recorded"], valuations["expected"], midnight["missing"]) == (expected["valuations"], expected["valuations"], []), (valuations, midnight)


def case_run_stale() -> None:
    fixture = _fixture("prd04-run.json")
    stale = fixture["stale"]
    infos = _paper_infos(stale["instruments"])
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        paths, clock = _run_paths(tmp, fixture), Clock(_time("2026-11-17T16:40:00"))
        routes = {
            "/api/v1/prices": [
                {"body": {"result": [{"symbol": symbol, "lastPrice": price, "timestamp": at, "currency": "KRW"} for symbol, price, at in run["prices"]]}}
                for run in stale["runs"]
            ],
            "/api/v1/exchange-rate": [{"body": {"result": fixture["run"]["rate"] | {"validFrom": run["validFrom"]}}} for run in stale["runs"]],
        }
        with _run_http(fixture, routes, clock, [], tmp) as (http, token, _, _):
            _prepare_run(paths, http, token, clock)
            with v6_run.ledger(paths) as conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('calendar_day', '2026-11-17')")
                v6_paper.save_instruments(conn, infos)
                conn.execute("UPDATE cash SET amount = ? WHERE currency = 'USD'", (stale["usd"],))
                for symbol, (quantity, average, market) in stale["positions"].items():
                    conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?)", (symbol, market, infos[symbol]["currency"], quantity, average))
                    conn.execute("INSERT INTO last_values VALUES (?, ?, ?)", (symbol, average, stale["last"]))
                conn.execute("INSERT INTO last_values VALUES ('USDKRW', '1350.00', ?)", (stale["last"],))
            for run in stale["runs"]:
                clock.now = _time(run["at"])
                assert v6_run.watch(paths, http, token, clock, clock.advance) == 0
        with closing(sqlite3.connect(paths.paper)) as conn:
            got = [list(row) for row in conn.execute("SELECT stale, stale_detail FROM valuations WHERE slot >= '2026-11-17 16:40' AND kind = 'watch' ORDER BY slot")]
        assert got == [[run["stale"], run["detail"]] for run in stale["runs"]], got


def case_run_strategies() -> None:
    fixture = _fixture("prd04-run.json")
    final = fixture["verdict"]["strategies"]["final"]
    content, tables = _report(fixture, lambda record: record["watch"].__setitem__("2026-12-01 00:00", [final, 0]))
    shares = sum(Decimal(row[3]) for row in tables["strategies.csv"][1])
    assert shares == (Decimal(final) - v6_paper.START_CASH) / v6_paper.START_CASH, (shares, tables["strategies.csv"][1])
    assert content["account"]["equity"] == final, content["account"]


# PRD 05 서버 실행 환경


def case_serve_token() -> None:
    fixture = _fixture("prd05-serve.json")["token"]
    sessions = _fixture("prd01-market.json")["sessions"] | {"holidays": {"KR": [], "US": []}, "overrides": {"KR": {}, "US": {}}}
    issued, headers, lock = [], [], threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            with lock:
                issued.append(request.url.path)
            time.sleep(0.2)  # 두 쪽이 함께 만료를 볼 때 앞쪽이 잠금을 쥐고 있게 한다
            return httpx.Response(200, json={"access_token": fixture["issued"], "token_type": "Bearer", "expires_in": 86400})
        with lock:
            headers.append(request.headers.get("authorization"))
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(200, json={"result": []})
        market, asked = request.url.path.rsplit("/", 1)[1], request.url.params["date"]
        days = fixture["calendar_days"][market]
        return httpx.Response(200, json=_calendar_body(sessions, market, dict(zip(("previousBusinessDay", "today", "nextBusinessDay"), days[asked]))))

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return _time(fixture["now"]).astimezone(tz)

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        (root / ".env").write_text(f"TOSS_INVEST_CLIENT_ID={fixture['client_id']}\nTOSS_INVEST_CLIENT_SECRET={fixture['client_secret']}\n", encoding="utf-8")
        token_path = root / "data" / "toss" / "token.json"
        token_path.parent.mkdir(parents=True)
        token_path.write_text(json.dumps({"access_token": fixture["expired"], "expires_at": (datetime.now(KST) - timedelta(hours=1)).isoformat()}), encoding="utf-8")
        paths = v6_run.Paths(root / "data" / "v6", root / "data" / "reports")
        paths.root.mkdir(parents=True)
        paths.config.write_text(json.dumps({"start": fixture["start"]}), encoding="utf-8")
        v6_market.save_calendar(paths.calendar, _standard_calendar(_fixture("prd04-run.json")["calendar"]))
        original = httpx.Client
        codes, ready = [], threading.Barrier(2)

        def v6_side() -> None:
            ready.wait()
            codes.append(cli.run_v6("v6-watch", None))

        def v5_side() -> None:
            with original(base_url=toss.BASE_URL, transport=httpx.MockTransport(handler)) as http:
                token = toss.SharedToken(http, toss.read_credentials(root), token_path)
                ready.wait()
                toss._send(http, "GET", "/api/v1/accounts", token)

        with (
            mock.patch.object(cli, "ROOT", root),
            mock.patch.object(cli, "V6_PATHS", paths),
            mock.patch.object(cli, "datetime", Fixed),
            mock.patch.object(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)),
            mock.patch.object(toss, "_sleep", lambda seconds: None),
        ):
            workers = [threading.Thread(target=v6_side), threading.Thread(target=v5_side)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        stored = json.loads(token_path.read_text(encoding="utf-8"))["access_token"]
    assert codes == [0], codes
    assert len(issued) == 1, f"토큰을 {len(issued)}번 받았다"
    assert headers and set(headers) == {f"Bearer {fixture['issued']}"} and stored == fixture["issued"], (set(headers), stored)


def _files(root: Path) -> dict[str, tuple]:
    """저장된 파일마다 (크기, 바꾼 시각, 내용)."""
    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes()) for path in sorted(root.rglob("*")) if path.is_file()}


def case_serve_page() -> None:
    fixture = _fixture("prd05-serve.json")["page"]
    run_fixture = _fixture("prd04-run.json")
    now = _time(fixture["now"])
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        paths, clock = _run_paths(tmp, run_fixture), Clock(now)
        with _run_http(run_fixture, {}, clock, [], tmp) as (http, token, _, _):
            v6_run.prepare(paths, http, token, clock, [])
        infos = _paper_infos(fixture["instruments"])
        with v6_run.ledger(paths) as conn:
            v6_paper.save_instruments(conn, infos)
            conn.execute("UPDATE cash SET amount = ? WHERE currency = 'KRW'", (fixture["krw"],))
            conn.execute("UPDATE cash SET amount = ? WHERE currency = 'USD'", (fixture["usd"],))
            for symbol, (quantity, average, price) in fixture["positions"].items():
                conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?)", (symbol, infos[symbol]["market"], infos[symbol]["currency"], quantity, average))
                conn.execute("INSERT INTO last_values VALUES (?, ?, ?)", (symbol, price, fixture["now"] + "+09:00"))
            used = {symbol: (Decimal(price), now, True) for symbol, (_, _, price) in fixture["positions"].items()} | {"USDKRW": (Decimal("1350.00"), now, False)}
            v6_paper.record_valuation(conn, fixture["slot"], "watch", now, fixture["equity"], v6_paper.state(conn), used, ["UST1"])
            for row in fixture["decisions"]:
                v6_head.record(conn, paths.head, {column: "" for column in v6_head.COLUMNS} | {"attempts": 1, "positions": "[]"} | row, None)
                if row["status"] == "ok":  # 실제 실행처럼 ok 판단에는 그 차례 C 평가가 있다
                    v6_paper.record_valuation(conn, v6_head.times(row["time"])[1].strftime("%Y-%m-%d %H:%M"), "c", now, fixture["equity"], v6_paper.state(conn), {}, [])
            conn.execute("INSERT INTO limits VALUES ('daily', ?, ?, 490000)", (fixture["now"][:10], fixture["now"] + "+09:00"))
            conn.execute("INSERT INTO events VALUES (?, 'forced_start', 'daily')", (fixture["now"] + "+09:00",))
            fill = dict.fromkeys(v6_paper.FILL_COLUMNS, "0") | fixture["fill"]
            conn.execute(f"INSERT INTO fills ({', '.join(v6_paper.FILL_COLUMNS)}) VALUES ({', '.join('?' * len(v6_paper.FILL_COLUMNS))})", [fill[column] for column in v6_paper.FILL_COLUMNS])
        (paths.root / "run.log").write_text("".join(f"줄 {number:02d}\n" for number in range(1, 51)), encoding="utf-8")
        (paths.root.parent / "toss").mkdir(exist_ok=True)
        (paths.root.parent / "toss" / "token.json").write_text(json.dumps({"access_token": fixture["token"]}), encoding="utf-8")
        (tmp / ".env").write_text(f"TOSS_INVEST_CLIENT_SECRET={fixture['secret']}\n", encoding="utf-8")
        status = {"started_at": fixture["now"], "running": None, "last": {"job": "watch " + fixture["slot"], "started_at": fixture["now"], "finished_at": fixture["now"], "exit": 0}}
        before = _files(tmp)
        httpd = v3_serve.server("127.0.0.1", 0, lambda: v6_serve.page(paths, status, now))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with httpx.Client(trust_env=False) as client:
                response = client.get(f"{base}/")
                assert response.status_code == 200 and response.headers["content-type"].startswith("text/html"), response
                assert client.get(f"{base}/other").status_code == 404 and client.post(f"{base}/").status_code == 501
        finally:
            httpd.shutdown()
            httpd.server_close()
        assert _files(tmp) == before, "페이지를 열었더니 저장된 파일이 바뀌었다"
    text = response.text
    for item in fixture["present"]:
        assert item in text, (item, text)
    for item in [fixture["token"], fixture["secret"], *fixture["absent"]]:
        assert item not in text, (item, text)


CASES = {
    "01": [
        (
            "세션과 거래 가능: 국내 장전·장후는 NXT 지원이고 NXT 거래정지가 아닌 종목만, 정규장은 KRX 거래정지가 아닌 종목, 미국은 네 세션, 닫힌 시각은 거래 불가,"
            " 판단 차례와 tradable_now는 E로 정함(T에 열리고 E에 닫힌 경계, T에 닫히고 E에 열린 경계), 장 운영 정보를 받지 못하면 저장한 값, 없으면 그 차례는 missing",
            case_sessions,
        ),
        (
            "후보: 레버리지 배수 2·−2인 ETF, ETN, ACTIVE가 아닌 종목은 없고 −1 인버스 ETF는 있음, 열린 시장마다 10개까지,"
            " 조건을 어기게 된 보유 종목은 exit_only",
            case_candidates,
        ),
    ],
    "02": [
        (
            "헤드 입력: C 뒤에 받은 랭킹·종목 정보·현재가·환율과 C 뒤에 나온 기사를 바꿔도 입력이 같음(손으로 적은 기대 입력과 같음),"
            " 입력에 가격 자체·계좌 번호·인증 정보가 없음, Codex를 두 번 부르면 두 호출의 입력이 같음",
            case_head_input,
        ),
        (
            "응답: 목록 밖 종목·exit_only 종목·6종목·비중 0.41·비중 합 1.01·겹친 종목은 형식 오류, E 뒤 응답은 late,"
            " 첫 호출이 실패하면 한 번만 더 부름, 실패한 차례는 targets가 None이라 사고팔지 않음",
            case_head_response,
        ),
    ],
    "03": [
        (
            "판단 체결: 판 뒤에 사고 목표 수량은 C 평가 자산 기준의 내린 정수 주, E 전 요청·E + 40초 뒤 받음·데이터 시각 없음·5분 넘게 이른 호가는 쓰지 않음,"
            " 여러 호가 단계를 모의 주문 하나로 모아 수수료·SEC Fee 한 번, 환전 원화(0.05%·0.5%), 미국 수수료 버림, SEC Fee 최저액·반올림, 국내 수수료(정규장·NXT),"
            " 증권거래세, 국내 ETF 위험 비용, 판 미국 돈은 달러로, C까지 환율이 없으면 미국 새 매수·증액·일부 감액 없이 전량 매도·exit_only 매도는 함, validFrom은 보지 않음",
            case_paper_fills,
        ),
        ("닫힌 시장의 보유 1종목과 새 목표 5종목이면 사고 나서 5종목을 넘는 매수를 하지 않음", case_paper_cap),
        (
            "손실 한도가 정확히 −2%·−5%·−20%에서 발동, 일부만 팔린 강제 매도는 날짜·주가 바뀌어도 다 팔 때까지 10분마다 다시 팔고 그동안 사지 않음,"
            " 월요일이 아닌 날의 00:00 평가가 없으면 앞의 하루 기준으로 그날 사지 않고, 월요일 00:00 평가가 없으면 앞의 한 주 기준으로 그 주 내내 사지 않음",
            case_paper_limits,
        ),
    ],
    "04": [
        ("실행: 판단 차례마다 한 번 실행, 같은 차례를 두 번 실행해도 판단·체결이 다시 생기지 않음, E가 지난 차례는 실행하지 않고 missing", case_run_decide),
        ("매시 40분에는 손실 한도 판정 뒤에 C 평가를 만들어 그 한도가 판단의 매수를 막음", case_run_limit_first),
        ("강제 매도와 판단 체결이 장부를 동시에 바꾸지 않음", case_run_serialized),
        ("토스 429는 한 번만 기다려 다시 보내고 두 번째 429 뒤에는 보내지 않음", case_run_rate_limit),
        (
            "검증 구간: 주말·휴장일이 낀 장 운영 정보로 센 10번째 국내 거래일 다음 날 00:00이 끝 시각, 마지막 날 23:45 체결과 끝 시각 평가는 판정에 들어가고"
            " 끝 시각 뒤에는 판단·체결·평가가 새로 생기지 않음",
            case_run_period,
        ),
        (
            "판정 경계: 판단 실패 정확히 10%·최대 낙폭 정확히 20%는 불통과, 10분 평가 90% 이상은 통과·하나 적으면 불통과, 장 운영 정보가 없는 날은 24번 missing,"
            " 00:00 평가 누락·stale·장부 규칙 위반 다섯 가지 각각 불통과, 끝 시각 전에는 판정 없이 지금까지 지난 차례만 셈",
            case_run_verdict,
        ),
        ("stale: 거래할 수 있는 보유의 현재가·필요한 환율을 마지막으로 받은 지 20분이 넘으면 stale, 20분 안에 받았으면 응답 시각이 오래돼도 아님, 거래할 수 없는 보유는 아님", case_run_stale),
        ("전략 이름별 pnl_krw ÷ 500,000의 합이 총수익률과 같음", case_run_strategies),
    ],
    "05": [
        ("v6의 토스 호출(v6-watch 명령 경로)이 v5와 같은 토큰 파일을 쓰고, 두 프로세스가 함께 만료를 보면 토큰을 한 번만 받음", case_serve_token),
        ("현황 웹페이지: GET 요청에만 답하고(다른 경로 404, POST 501), 판단 이유 속 HTML은 글자 그대로, 페이지를 열어도 파일이 바뀌지 않음, 인증 정보와 토큰이 보이지 않음", case_serve_page),
    ],
}


def run(prd: str | None = None) -> int:
    results = []
    for prd_id, cases in CASES.items():
        if prd and prd != prd_id:
            continue
        for name, case in cases:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    case()
                results.append({"prd": prd_id, "case": name, "passed": True})
            except Exception as error:
                results.append({"prd": prd_id, "case": name, "passed": False, "error": f"{type(error).__name__}: {error}"})
    passed = sum(result["passed"] for result in results)
    summary = {"total": len(results), "passed": passed, "failed": len(results) - passed}
    print(json.dumps({"results": results, "summary": summary}, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1

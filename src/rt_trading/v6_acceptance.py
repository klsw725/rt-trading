"""v6 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import io
import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx

from rt_trading import codex, toss, v4_news, v6_head, v6_market, v6_paper
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

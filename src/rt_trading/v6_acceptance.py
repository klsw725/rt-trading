"""v6 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import io
import json
import tempfile
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx

from rt_trading import codex, toss, v4_news, v6_head, v6_market
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

    routes는 열쇠(경로, 쿼리에 date·marketCountry가 있으면 "?이름=값"을 붙인다) → 응답 목록이고 마지막 응답을 되풀이한다.
    응답은 {"status", "body", "headers", "elapsed"}(status 기본 200) 또는 "network"이고, 받을 때마다 시계를 elapsed초만큼 움직인다.
    """

    def __init__(self, routes: dict, clock: Clock) -> None:
        self.routes, self.clock = routes, clock
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path + "".join(f"?{name}={request.url.params[name]}" for name in ("date", "marketCountry") if name in request.url.params)
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

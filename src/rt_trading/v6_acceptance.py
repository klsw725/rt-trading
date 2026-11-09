"""v6 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import io
import json
import tempfile
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx

from rt_trading import toss, v6_market
from rt_trading.toss import KST

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "docs" / "specs" / "v6" / "fixtures"


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

"""v4 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import copy
import csv
import functools
import io
import json
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx

from rt_trading import (
    codex,
    engine,
    fixtures,
    market_data,
    report,
    sessions,
    store,
    v3_daily,
    v3_paper,
    v3_serve,
    v3_us,
    v4_daily,
    v4_head,
    v4_news,
    v4_paper,
    v4_report,
    v4_serve,
)
from rt_trading.toss import KST

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "docs" / "specs" / "v4" / "fixtures"
FAR_FUTURE_MS = 4_102_444_800_000  # 2100-01-01. 가짜 토큰이 갱신되지 않게 한다


# PRD 01 뉴스 검색


def _prd01() -> dict:
    return json.loads((FIXTURES / "prd01-news.json").read_text(encoding="utf-8"))


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=KST)


def _news_case(fixture: dict, name: str) -> None:
    """가짜 NAVER API HUB와 가짜 시계로 뉴스를 받아, 고른 기사·요청·저장 파일과 출력을 손으로 적은 기대와 비교한다.

    가짜 NAVER API HUB는 검색어마다 적은 응답을 돌려주고, 걸린 시간만큼 가짜 시계를 움직이며, 요청을 기록한다.
    """
    case = fixture["cases"][name]
    clock, requests, output = {"now": _time(case["start"])}, [], io.StringIO()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = case["responses"][request.url.params["query"]]
        clock["now"] += timedelta(seconds=response["seconds"])
        if "error" in response:
            raise getattr(httpx, response["error"])("가짜 오류", request=request)
        return httpx.Response(response["status"], json=response["body"])

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, fixture["credentials"] if case["credentials"] else {}):
        if not case["credentials"]:  # 환경 변수와 .env가 모두 없는 임시 루트
            for key in fixture["credentials"]:
                os.environ.pop(key, None)
        path = Path(tmp) / "news" / "1100.json"
        with httpx.Client(transport=httpx.MockTransport(handler)) as http, contextlib.redirect_stdout(output):
            news, errors = v4_news.fetch(
                http, v4_news.read_credentials(Path(tmp)), _time(case["cutoff"]), _time(case["deadline"]), lambda: clock["now"], path
            )
        written = path.read_text(encoding="utf-8")
    assert json.dumps(news, ensure_ascii=False) == json.dumps(case["news"], ensure_ascii=False), (name, news)
    assert [request.url.params["query"] for request in requests] == case["requests"], (name, [str(request.url) for request in requests])
    for request in requests:
        assert str(request.url).partition("?")[0] == fixture["url"], (name, str(request.url))
        assert dict(request.url.params) == {"query": request.url.params["query"]} | fixture["params"], (name, str(request.url))
        assert {key: request.headers.get(key) for key in fixture["headers"]} == fixture["headers"], (name, "인증 헤더가 다르다")
    text = written + json.dumps([news, errors], ensure_ascii=False) + output.getvalue()
    assert not any(secret in text for secret in fixture["credentials"].values()), (name, "인증 정보가 저장 파일이나 출력에 있다")


def case_news_select() -> None:
    fixture = _prd01()
    for name in ("select", "limit"):
        _news_case(fixture, name)


def case_news_failure() -> None:
    fixture = _prd01()
    for name in ("all_failed", "no_credentials", "deadline"):
        _news_case(fixture, name)


# PRD 02 하루 여러 번 판단하는 헤드


def _prd02() -> dict:
    return json.loads((FIXTURES / "prd02-head.json").read_text(encoding="utf-8"))


def _same(actual: object, expected: object) -> None:
    """키 순서까지 같은지 본다."""
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False), (actual, expected)


def _prices(market: dict, future: bool) -> dict[str, dict[str, object]]:
    """v3 PRD 01의 일봉 spec. 평일 중 휴장일이 아닌 날은 기본 수정 종가이고, 바꿀 날만 바꾼다. future면 미래 일봉을 더한다."""
    prices, day = {}, Date.fromisoformat(market["start"])
    while day <= Date.fromisoformat(market["end"]):
        if day.weekday() < 5 and day.isoformat() not in market["holidays"]:
            prices[day.isoformat()] = {**market["base"], **market["changes"].get(day.isoformat(), {})}
        day += timedelta(days=1)
    return prices | market["future"] if future else prices


def _daily_dbs(tmp: Path, fixture: dict, future: bool) -> tuple[Path, Path]:
    """candles_1d만 있는 일봉 DB(원가격도 수정 종가와 같다)와 us_candles_1d만 있는 미국 일봉 DB."""
    market, us = tmp / f"market-{future}.sqlite3", tmp / f"us-{future}.sqlite3"
    rows = [(symbol, day, price, price, price, price, 1000, price) for day, item in _prices(fixture["market"], future).items() for symbol, price in item.items()]
    with closing(sqlite3.connect(market)) as db, db:
        db.execute(f"CREATE TABLE candles_1d ({store.TABLES['candles_1d']})")
        db.executemany("INSERT INTO candles_1d VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    rows = [(symbol, day, price) for day, item in _prices(fixture["us_market"], future).items() for symbol, price in item.items()]
    with closing(sqlite3.connect(us)) as db, db:
        db.execute(f"CREATE TABLE us_candles_1d ({v3_us.TABLE})")
        db.executemany("INSERT INTO us_candles_1d VALUES (?, ?, ?)", rows)
    return market, us


def _bars(day: str, given: dict[str, dict[str, list[int]]], future: dict | None = None) -> dict[str, list[market_data.Bar]]:
    """종목마다 적은 봉(future가 있으면 그 봉으로 더 바꾼다)과, 나머지 장 일정 봉을 직전 봉 종가·거래량 0으로 채운 1분봉."""
    times, bars = sessions.session(day, sessions.exceptions()).bars, {}
    for symbol, changes in given.items():
        changes, close, bars[symbol] = changes | (future or {}).get(symbol, {}), None, []
        for time in sorted(set(changes) | set(times)):
            values = changes.get(time) or [close] * 4 + [0]
            bars[symbol].append(market_data.Bar(time, *values))
            close = values[3]
    return bars


class _Clock:
    """가짜 시계. 기다린 시간을 기록한다."""

    def __init__(self, day: str, clock: str) -> None:
        self.now, self.waits = _time(f"{day}T{clock}"), []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)


class _Codex:
    """가짜 Codex. 시도마다 적은 응답을 돌려주고, 걸린 시간만큼 가짜 시계를 움직이며, 요청을 기록한다."""

    def __init__(self, fixture: dict, clock: _Clock, attempts: list[list]) -> None:
        self.fixture, self.clock, self.attempts, self.requests = fixture, clock, list(attempts), []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        kind, seconds, *text = self.attempts.pop(0)
        self.clock.now += timedelta(seconds=seconds)
        if kind == "network":
            raise httpx.ConnectError("연결 실패", request=request)
        if kind == "http":
            return httpx.Response(500, text="오류")
        reply = text[0] if text else self.fixture["ok_text" if kind == "ok" else "invalid_text"]
        events = [{"type": "response.output_text.delta", "delta": reply}, {"type": "response.completed", "response": {}}]
        return httpx.Response(200, content="".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode())


def _decide(fixture: dict, tmp: Path, day: str, slot: str, start: str, attempts: list[list], content: dict):
    """임시 토큰 파일, 가짜 Codex, 가짜 시계로 판단 하나를 정한다. (기록한 행, 시계, 가짜 Codex)."""
    token_dir = tmp / "token"
    if not token_dir.exists():
        (token_dir / "auth").mkdir(parents=True)
        token = fixture["token"] | {"expires": FAR_FUTURE_MS}
        (token_dir / "auth" / "codex.json").write_text(json.dumps(token), encoding="utf-8")
    clock = _Clock(day, start)
    fake = _Codex(fixture, clock, attempts)
    with httpx.Client(transport=httpx.MockTransport(fake.handler)) as http:
        row = v4_head.decide(
            tmp / "paper.sqlite3", tmp / "head", day, slot, content, http, lambda: codex.credentials(token_dir), clock, clock.sleep
        )
    return row, clock, fake


def _fetched_news(items: list[dict], cutoff: datetime) -> list[dict] | None:
    """검색어마다 items를 돌려주는 가짜 NAVER API HUB로 PRD 01의 뉴스 값을 만든다."""
    with tempfile.TemporaryDirectory() as name, httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"items": items}))) as http:
        news, _ = v4_news.fetch(http, ("id", "secret"), cutoff, cutoff + timedelta(minutes=2), lambda: cutoff, Path(name) / "news.json")
    return news


def case_head_input() -> None:
    fixture = _prd02()
    case = fixture["input"]
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        for future in (False, True):  # T 뒤의 당일 1분봉과 기사, 판단일과 그 뒤의 일봉·미국 일봉을 바꿔도 같다
            market, us = _daily_dbs(tmp, fixture, future)
            bars = _bars(case["day"], case["bars"], case["future_bars"] if future else None)
            news = _fetched_news(case["raw_news"] + case["after_news"][future], _time(f"{case['day']}T{case['slot']}"))
            args = (case["day"], case["slot"], case["days"], fixture["month_days"], market, us, fixture["accounts"], case["decisions"])
            content = v4_head.build_input(*args, bars, news)
            _same(content, case["expected"])
        for item in case["missing"]:
            partial = {symbol: found if symbol in item["bars"] else [] for symbol, found in bars.items()}
            _same(v4_head.build_input(*args, partial, news)["today_market"], item["today_market"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            row, _, fake = _decide(fixture, tmp, case["day"], case["slot"], "11:00:30", [["ok", 5]], content)
        assert row["status"] == "ok", row
        folder = tmp / "head" / case["day"] / case["slot"].replace(":", "")
        assert sorted(path.name for path in folder.iterdir()) == ["decision.json", "input.json", "response.txt"], folder
        stored = [request.content for request in fake.requests] + [(tmp / "paper.sqlite3").read_bytes(), output.getvalue().encode()]
        stored += [path.read_bytes() for path in sorted((tmp / "head").rglob("*")) if path.is_file()]
        for secret in fixture["token"].values():
            assert all(secret.encode() not in blob for blob in stored), secret


def case_head_slots() -> None:
    for day, expected in _prd02()["slots"].items():
        found = v4_head.slots(day)
        assert [list(item) for item in found] == expected, (day, found)
        window = sessions.session(day, sessions.exceptions()).window
        assert all(item.switch < window[0] for item in found), (day, window[0])


def case_head_deadline() -> None:
    fixture = _prd02()
    for case in fixture["deadline_cases"]:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            content = {"decision_date": case["date"], "decision": {"current": "cash", "switches_left": 2}}
            row, clock, fake = _decide(fixture, tmp, case["date"], case["slot"], case["start"], case["attempts"], content)
            expected = case["expected"]
            if expected is None:
                assert row is None and not fake.requests, case["name"]
                assert not (tmp / "paper.sqlite3").exists() and not (tmp / "head").exists(), case["name"]
                continue
            actual = {key: row[key] for key in ("status", "subordinate", "reason", "notes", "attempts", "error", "finished_at")}
            assert actual == {key: expected[key] for key in actual}, (case["name"], actual)
            assert clock.waits == expected["waits"] and len(fake.requests) == expected["attempts"], (case["name"], clock.waits)
            plan = v4_head.plan([case["date"]], v4_head.read(tmp / "paper.sqlite3"))[case["date"]]
            _same(plan, expected["plan"])


def case_head_response() -> None:
    fixture = _prd02()
    for item in fixture["responses"]:
        lengths = item.get("lengths")
        text = item.get("text") or json.dumps(
            {"subordinate": "cash", "reason": "가" * lengths["reason"], "notes": "나" * lengths["notes"]}, ensure_ascii=False
        )
        content = {"decision_date": "2026-01-05", "decision": {"current": "cash", "switches_left": 2}}
        with tempfile.TemporaryDirectory() as name:
            row, _, _ = _decide(fixture, Path(name), "2026-01-05", "11:00", "11:04:50", [["ok", 5, text]], content)
        assert row["status"] == ("ok" if item["valid"] else "invalid"), (item["name"], row)


@functools.cache
def _day_scenario() -> tuple[list[tuple[dict, dict]], dict]:
    """PRD 02 fixture의 판단을 차례로 돌린다. (차례마다 (입력, 기록한 행), 마지막 계획 부하). 사례들이 함께 쓴다."""
    fixture = _prd02()
    case = fixture["day"]
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        market, us = _daily_dbs(tmp, fixture, False)
        steps = []
        for step in case["steps"]:
            decisions = v4_head.read(tmp / "paper.sqlite3")
            content = v4_head.build_input(
                step["date"], step["slot"], case["days"], fixture["month_days"], market, us, fixture["accounts"], decisions, None, None
            )
            row, _, _ = _decide(fixture, tmp, step["date"], step["slot"], step["start"], step["attempts"], content)
            steps.append((content, row))
        return steps, v4_head.plan(case["days"], v4_head.read(tmp / "paper.sqlite3"))


def case_head_capped() -> None:
    case = _prd02()["day"]
    steps, plan = _day_scenario()
    for step, (content, row) in zip(case["steps"], steps, strict=True):
        assert content["decision"] == step["decision"] and row["status"] == step["status"], (step["slot"], content["decision"], row)
    _same(plan, case["plan"])


def case_head_plan() -> None:
    case = _prd02()["plan"]
    _same(v4_head.plan(case["days"], case["decisions"]), case["expected"])


def case_head_notes() -> None:
    case = _prd02()["day"]
    steps, _ = _day_scenario()
    for step, (content, _) in zip(case["steps"], steps, strict=True):
        assert content["notes"] == step["notes"], (step["date"], step["slot"], content["notes"])


# PRD 03 장중 교체 엔진


def _prd03() -> dict:
    return json.loads((FIXTURES / "prd03-paper.json").read_text(encoding="utf-8"))


def _market(spec: dict, omit_after: dict[str, str] | None = None) -> market_data.MarketData:
    """v1 fixtures.build spec으로 시세 DB를 만들어 v1 검증으로 읽는다. omit_after의 날은 그 시각 뒤 봉을 뺀다."""
    spec = copy.deepcopy(spec)
    for day in spec["days"]:
        if after := (omit_after or {}).get(day["date"]):
            later = [time for time in sessions.session(day["date"], sessions.exceptions()).bars if time > after]
            for symbol in fixtures.SYMBOLS:
                day.setdefault("bars", {}).setdefault(symbol, {}).update(dict.fromkeys(later))
    with tempfile.TemporaryDirectory() as name:
        fixtures.build(spec, Path(name) / "market.sqlite3")
        data, violations = market_data.load(Path(name) / "market.sqlite3")
    assert not violations, market_data.describe(violations)
    return data


def _check_records(name: str, records: engine.Records, expected: dict) -> None:
    fills = [[f.time, f.symbol, f.side, f.quantity, f.price, f.fee, f.tax, f.reason] for f in records.fills]
    assert fills == expected["fills"], (name, fills)
    discarded = [[d.time, d.kind, d.symbol, d.reason] for d in records.discarded]
    assert discarded == expected["discarded"], (name, discarded)
    assert records.closes == expected["closes"], (name, records.closes)
    if "limits" in expected:  # 손실 한도 사례만 적는다
        assert [list(limit) for limit in records.limits] == expected["limits"], (name, records.limits)


def _paper_case(key: str) -> None:
    """시험용 부하(적은 날짜·시각에 적은 주문을 낸다. 결함일에도)를 더해 계좌마다 schedule로 돌린다."""
    case = _prd03()["cases"][key]
    data, subordinates = _market(case["market"], case.get("omit_after")), dict(v4_paper.SUBORDINATES)
    for name, test in case.get("tests", {}).items():
        script = {(day, clock): engine.Order(kind, symbol, "signal") for day, clock, kind, symbol in test["orders"]}
        subordinates[name] = v3_paper.Subordinate(lambda script=script: lambda view: script.get((view.date, view.time)), test["overnight"], None)
    for name, account in case["accounts"].items():
        records = v4_paper.simulate(data, data.days, case["start"], case["final_day"], account["schedule"], subordinates)
        _check_records(name, records, account["expected"])


def case_paper_switch() -> None:
    _paper_case("switch")


def case_paper_late_switch() -> None:
    _paper_case("late_switch")


def case_paper_overnight() -> None:
    _paper_case("overnight")


def case_paper_morning() -> None:
    case = _prd03()["morning"]
    data = _market(case["market"])
    records = v4_paper.run_accounts(data, data.days, case["start"], case["final_day"], case["decisions"])
    for name, expected in case["expected"].items():
        _check_records(name, records[name], expected)


def case_paper_defect() -> None:
    _paper_case("defect")


def case_paper_limits() -> None:
    _paper_case("limits")


def case_paper_after_switch() -> None:
    _paper_case("after_switch")


def case_paper_blocked() -> None:
    _paper_case("weekly")
    _paper_case("cumulative")


def case_paper_rule() -> None:
    rule = _prd03()["rule"]
    for choice in rule["choices"]:
        closes = [choice["base"]] * (choice["count"] - 1) + [choice["last"]]
        if "first" in choice:
            closes[0] = choice["first"]
        assert v3_paper.rule_choice(closes) == choice["expected"], choice["name"]
    plan, days, day = rule["plan"], [], Date(2025, 1, 1)
    while len(days) < plan["count"]:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    closes = {date: plan["changes"].get(str(index), plan["base"]) for index, date in enumerate(days)}
    data = market_data.MarketData(days, {}, {}, {}, [], {"069500": {date: (price, price) for date, price in closes.items()}})
    chosen = v3_paper.rule_plan(data, [days[int(index)] for index in plan["expected"]])
    assert chosen == {days[int(index)]: value for index, value in plan["expected"].items()}, chosen


# PRD 04 실행과 보고


def _prd04() -> dict:
    return json.loads((FIXTURES / "prd04-run.json").read_text(encoding="utf-8"))


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()}


def _source(fixture: dict, history: list[str], run: dict) -> dict:
    """그 실행의 원본 시세 fixtures.build spec. broken의 날은 fixture의 봉을 빼고, omit_after의 날은 그 시각 뒤 봉을 뺀다."""
    days = [{"date": history[0], "prices": fixture["history"]["prices"]}] + [{"date": day} for day in history[1:]]
    for day in run["source"]:
        spec = {"date": day}
        if day in run.get("broken", []):
            spec["bars"] = copy.deepcopy(fixture["broken_bars"])
        if after := fixture["omit_after"].get(day):
            later = [time for time in sessions.session(day, sessions.exceptions()).bars if time > after]
            spec["bars"] = {symbol: dict.fromkeys(later) for symbol in fixtures.SYMBOLS}
        days.append(spec)
    return {"fetched_at": f"{run['at']}+09:00", "volume": 1000, "days": days}


@functools.cache
def _run_scenario() -> tuple[list[dict], list[dict], list[list]]:
    """PRD 04 fixture의 실행을 차례로 한 번만 돌려 사례들이 함께 쓴다. (실행마다의 결과, 보고서마다의 결과, 마지막 판단 기록).

    시세 받기는 원본을 그 실행의 spec으로 바꾼다. 당일 1분봉·뉴스 받기는 받은 초와 마감을 기록하고, timeout이면 가짜 시계를 받기 마감까지 움직여 받지 못한 것으로 돌려준다.
    """
    fixture = _prd04()
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        paths = v4_daily.Paths(tmp / "market.sqlite3", tmp / "v4" / "market.sqlite3", tmp / "v4" / "paper.sqlite3", tmp / "v4" / "head", tmp / "v4" / "news", tmp / "us.sqlite3")
        period_path, holidays_path = tmp / "v4_period.json", tmp / "krx_holidays.csv"
        period_path.write_text(json.dumps({"start": fixture["start"]}), encoding="utf-8")
        holidays_path.write_text("date,reason\n" + "".join(f"{day},{reason}\n" for day, reason in fixture["holidays"]), encoding="utf-8")
        (tmp / "token" / "auth").mkdir(parents=True)
        (tmp / "token" / "auth" / "codex.json").write_text(json.dumps(fixture["token"] | {"expires": FAR_FUTURE_MS}), encoding="utf-8")
        history, day = [], Date.fromisoformat(fixture["history"]["from"])
        while day <= Date.fromisoformat(fixture["history"]["to"]):
            if day.weekday() < 5:
                history.append(day.isoformat())
            day += timedelta(days=1)
        results, reports = [], []
        for run in fixture["runs"]:
            clock, output = _Clock(*run["at"].split("T")), io.StringIO()
            if run["command"] == "report":
                with contextlib.redirect_stdout(output):
                    code = v4_report.run(paths, period_path, holidays_path, tmp / "reports", clock)
                directory = tmp / "reports" / f"v4-{clock.now.strftime('%Y%m%d-%H%M%S')}"
                files = {path.name: path.read_bytes() for path in sorted(directory.iterdir())} if directory.exists() else {}
                reports.append({"exit": code, "output": output.getvalue(), "files": files})
                continue
            attempts = run.get("attempts") or ([["ok", 5, fixture["replies"][run["reply"]]]] if "reply" in run else [])
            fake, fetched, seconds, deadlines = _Codex(fixture, clock, attempts), [], [], []

            def fetch(left: float, run: dict = run, fetched: list = fetched) -> int:
                fetched.append(left)
                fixtures.build(_source(fixture, history, run), paths.source)
                return 0

            def bars(day: str, left: float, run: dict = run, seconds: list = seconds, clock: _Clock = clock) -> dict | None:
                seconds.append(left)
                if run.get("timeout"):
                    clock.now += timedelta(seconds=left)
                    return None
                return _bars(day, fixture["bars"])

            def news(cutoff: datetime, deadline: datetime, path: Path, run: dict = run, deadlines: list = deadlines, clock: _Clock = clock):
                deadlines.append(deadline.isoformat())
                if run.get("timeout"):
                    clock.now = max(clock.now, deadline)
                    return None, ["코스피: V4_NEWS_SKIPPED: 받기 마감이 지나 부르지 않았다"]
                return fixture["news"], []

            token_dir = tmp / ("no-token" if run.get("no_token") else "token")
            settled_before = v3_daily.settled_days(paths.paper)
            before = _snapshot(tmp)
            with contextlib.redirect_stdout(output), httpx.Client(transport=httpx.MockTransport(fake.handler)) as http:
                if run["command"] == "daily":
                    code = v4_daily.run(paths, period_path, holidays_path, fetch, news, http, lambda: codex.credentials(token_dir), clock, clock.sleep)
                else:
                    code = v4_daily.decide(paths, period_path, holidays_path, run["slot"], bars, news, http, lambda: codex.credentials(token_dir), clock, clock.sleep)
            inputs = [json.loads(json.loads(request.content)["input"][0]["content"][0]["text"]) for request in fake.requests]
            after = _snapshot(tmp)
            settled = v3_daily.settled_days(paths.paper)
            results.append(
                {
                    "exit": code,
                    "output": output.getvalue(),
                    "fetch_budget": fetched[0] if fetched else None,
                    "bars_seconds": seconds,
                    "news_deadlines": deadlines,
                    "codex_calls": len(fake.requests),
                    "settled": settled,
                    "unchanged": after == before,
                    "copy_unchanged": after.get(str(paths.copy)) == before.get(str(paths.copy)) and settled == settled_before,
                    "nulls": bool(inputs) and all(content["today_market"] is None and content["news"] is None for content in inputs),
                }
            )
        decisions = [[row["date"], row["slot"], row["kind"], row["status"] == "missing"] for row in v4_head.read(paths.paper)]
    return results, reports, decisions


def _check_runs(*names: str) -> None:
    """이름의 실행마다 fixture에 적은 기대 키만 비교한다. output은 출력에 그 글이 들어 있는지 본다."""
    runs, (results, _, _) = _prd04()["runs"], _run_scenario()
    found = {run.get("name"): (run, result) for run, result in zip([run for run in runs if run["command"] != "report"], results, strict=True)}
    for name in names:
        run, result = found[name]
        for key, value in run["expected"].items():
            if key == "output":
                for text in value:
                    assert text in result["output"], (name, text, result["output"])
            else:
                assert result[key] == value, (name, key, result[key], result["output"])


def case_run_decide() -> None:
    _check_runs(
        "개장 전 차례는 v4-decide의 차례가 아님",
        "차례에 시작한 장중 판단은 당일 1분봉 받기에 120초",
        "같은 차례를 다시 실행",
        "받기가 T + 2분까지 끝나지 않아도 마감 전에 판단",
        "마감에 시작하면 받기·호출·판단 행이 없음",
        "v4-decide는 시세 사본과 정산을 바꾸지 않음",
        "휴장일 v4-decide",
        "기간 전 v4-decide",
        "기간 끝 v4-decide",
    )
    decisions = _run_scenario()[2]
    assert ["2024-11-11", "13:00", "intraday", True] in decisions, decisions  # 마감에 시작해 하지 않은 차례는 정산에서 missing이다


def case_run_settle() -> None:
    _check_runs(
        "시작일 개장 전 판단",
        "같은 날 v4-daily를 다시 실행",
        "다음 날 정산과 토큰이 없는 개장 전 판단",
        "검증에 실패한 날에서 정산을 멈추고 판단은 함",
        "밀린 날을 오래된 날부터 정산",
        "휴장일 v4-daily",
        "기간 마지막 날까지 정산",
    )
    assert _run_scenario()[2] == _prd04()["decisions"], _run_scenario()[2]  # 차례마다 한 행, 판단하지 않은 차례는 missing


def case_run_verdict() -> None:
    fixture = _prd04()
    for case in fixture["verdict_cases"]:
        equity = case["equity"]
        records = engine.Records([("", engine.INITIAL_CASH), ("2024-11-11 09:01", equity)], {"2024-11-11": equity}, [], [], [], [])
        assert v4_report.verdict(records, case["statuses"]) == case["expected"], case["name"]
    _, reports, _ = _run_scenario()
    mid, expected = json.loads(reports[0]["files"]["report.json"]), fixture["mid"]
    assert {key: mid["run"][key] for key in expected["run"]} == expected["run"], mid["run"]
    assert mid["decisions"] == expected["decisions"] and mid["verdict"] is None, mid
    assert (mid["accounts"][0]["id"], mid["accounts"][0]["final_equity"]) == ("head", expected["head_final_equity"]), mid["accounts"][0]
    assert "정산 1/5" in reports[0]["output"], reports[0]["output"]
    final = json.loads(reports[1]["files"]["report.json"])
    assert final["verdict"] == fixture["final"]["report"]["verdict"], final["verdict"]
    assert "판정: 불통과" in reports[1]["output"], reports[1]["output"]


def _matches(actual: object, expected: object) -> bool:
    """JSON 값이 같은지 본다. 소수는 1e-12 안의 차이만 허용한다."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(_matches(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(map(_matches, actual, expected))
    if isinstance(expected, float):
        return isinstance(actual, float) and abs(actual - expected) < 1e-12
    return type(actual) is type(expected) and actual == expected


def case_run_report() -> None:
    expected = _prd04()["final"]
    _, reports, _ = _run_scenario()
    result = reports[1]
    assert result["exit"] == 0 and sorted(result["files"]) == expected["files"], sorted(result["files"])
    content = json.loads(result["files"]["report.json"])
    assert _matches(content, expected["report"]), content
    rows = list(csv.reader(io.StringIO(result["files"]["decisions.csv"].decode("utf-8"))))
    assert rows[0] == v4_report.DECISION_COLUMNS, rows[0]
    for row, want in zip(rows[1:], expected["decisions_csv"], strict=True):
        assert row[:8] == want[:8] and abs(float(row[8]) - want[8]) < 1e-12, (row, want)
    for name in expected["files"]:
        if name.startswith("trades-"):
            assert next(csv.reader(io.StringIO(result["files"][name].decode("utf-8")))) == report.TRADE_COLUMNS, name
    trades = list(csv.reader(io.StringIO(result["files"]["trades-head.csv"].decode("utf-8"))))
    assert trades[1:] == expected["trades_head"], trades
    assert reports[1]["files"] == reports[2]["files"], "같은 저장 상태의 두 보고서가 다르다"


# PRD 05 서버 실행 환경


def _prd05() -> dict:
    return json.loads((FIXTURES / "prd05-serve.json").read_text(encoding="utf-8"))


def _period(tmp: Path, start: str, holidays: list[list[str]]) -> v3_daily.Period:
    (tmp / "v4_period.json").write_text(json.dumps({"start": start}), encoding="utf-8")
    (tmp / "krx_holidays.csv").write_text("date,reason\n" + "".join(f"{day},{reason}\n" for day, reason in holidays), encoding="utf-8")
    return v3_daily.load_period(tmp / "v4_period.json", tmp / "krx_holidays.csv", "V4_PERIOD_INVALID")


def case_serve_schedule() -> None:
    fixture = _prd05()
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        for item in fixture["schedule"]:
            period, now = _period(tmp, item["start"], fixture["holidays"]), _time(item["now"])
            assert v4_serve.due(now, period) == item["due"], (item["now"], v4_serve.due(now, period))
            assert v4_serve.next_run(now, period) == _time(item["next"]), (item["now"], v4_serve.next_run(now, period))
        period, log_path = _period(tmp, fixture["tick_start"], fixture["holidays"]), tmp / "v4" / "run.log"
        status, done, executed, clock = {"started_at": "", "running": None, "last": None}, None, [], _Clock("2026-01-05", "00:00:00")

        def execute(command: str, file: io.TextIOBase, seconds: float) -> int | None:
            executed.append([command, seconds])
            file.write(f"{command} 출력\n")
            if fixture["tick_exits"][command] is None:  # 시간 초과로 정해 둔 명령은 받은 초를 다 쓴다
                clock.now += timedelta(seconds=seconds)
            return fixture["tick_exits"][command]

        for item in fixture["ticks"]:
            clock.now, before = _time(item["now"]), len(executed)
            done = v4_serve.tick(done, status, log_path, period, clock, execute)
            assert executed[before:] == item["runs"], (item["now"], executed[before:])
            if "last" in item:
                assert status["last"] == item["last"], (item["now"], status["last"])
        assert log_path.read_text(encoding="utf-8") == fixture["tick_log"], log_path.read_text(encoding="utf-8")
        assert status["last"] == fixture["tick_last"] and status["running"] is None, status


def case_serve_page() -> None:
    fixture = _prd05()["page"]
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        paths = v4_daily.Paths(tmp / "market.sqlite3", tmp / "v4" / "market.sqlite3", tmp / "v4" / "paper.sqlite3", tmp / "v4" / "head", tmp / "v4" / "news", tmp / "us.sqlite3")
        period, report_dir, log_path = _period(tmp, fixture["start"], fixture["holidays"]), tmp / "reports", tmp / "v4" / "run.log"
        for row in fixture["decisions"]:
            v4_head.insert(paths.paper, dict.fromkeys(v4_head.COLUMNS, "") | {"attempts": 0} | row)
        with closing(sqlite3.connect(paths.paper)) as db, db:
            db.execute(f"CREATE TABLE settled_days ({v3_daily.SETTLED})")
            db.executemany("INSERT INTO settled_days VALUES (?, ?)", [(day, "") for day in fixture["settled"]])
        for report_name, content in fixture["reports"].items():
            (report_dir / report_name).mkdir(parents=True)
            (report_dir / report_name / "report.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        log_path.write_text("".join(f"줄 {number:02d}\n" for number in range(1, fixture["log_lines"] + 1)), encoding="utf-8")
        now, before = _time(fixture["now"]), _snapshot(tmp)
        httpd = v3_serve.server("127.0.0.1", 0, lambda: v4_serve.page(paths, period, report_dir, log_path, fixture["status"], now))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with httpx.Client(trust_env=False) as http:
                response = http.get(f"{base}/")
                assert response.status_code == 200 and response.headers["content-type"].startswith("text/html"), response
                for text in fixture["present"]:
                    assert text in response.text, (text, response.text)
                for text in fixture["absent"]:
                    assert text not in response.text, (text, response.text)
                assert http.get(f"{base}/other").status_code == 404
                assert http.post(f"{base}/").status_code == 501
        finally:
            httpd.shutdown()
            httpd.server_close()
        assert _snapshot(tmp) == before, "페이지를 열었더니 저장된 파일이 바뀌었다"
        first = tmp / "first" / "paper.sqlite3"
        first.parent.mkdir()
        v4_head.insert(first, dict.fromkeys(v4_head.COLUMNS, "") | {"attempts": 0} | fixture["decisions"][0])
        before = first.read_bytes()
        text = v4_serve.page(paths._replace(paper=first), period, report_dir, log_path, fixture["status"], now)
        assert fixture["first_day"] in text and first.read_bytes() == before, text


CASES = {
    "01": [
        (
            "기사 고르기: T 뒤·T와 같은 시각의 기사 없음, 같은 원문 링크는 한 번, 태그를 지운 뒤 문자 참조를 풂, +0000 기사도 한국 시각으로 고름,"
            " 최신 30개만 늦은 것부터, HTTP 오류·형식 오류인 검색어만 빠짐, 요청은 정해 둔 주소·쿼리와 두 인증 헤더, 인증 정보는 저장 파일과 출력에 없음",
            case_news_select,
        ),
        ("모든 검색어가 실패하면 null, 인증 정보가 없으면 아무것도 부르지 않고 null, 받기 마감이 지난 뒤의 검색어는 부르지 않음", case_news_failure),
    ],
    "02": [
        (
            "헤드 입력: 손 계산과 같고 T 뒤 당일 1분봉·판단일 뒤 일봉(미국 일봉 포함)을 바꿔도 같음, 오늘 시세 다섯 값과 30분 구간 네 개,"
            " 한 종목·두 종목 봉이 없으면 그 종목·today_market이 null, 가짜 토큰과 계좌 id는 입력·요청 본문·기록·출력에 없음",
            case_head_input,
        ),
        ("판단 차례와 마감이 정상일·연초 첫 거래일·수능일의 장 일정에 맞고, 교체 시각은 모두 종가 동시호가 접수 구간보다 앞섬", case_head_slots),
        (
            "장중은 15초마다 다시 부르고 마감(차례 + 5분) 전 ok·뒤 late, 마감까지 형식 오류·호출 오류면 invalid·failed, 실패는 계획 부하를 바꾸지 않음,"
            " 마감에 시작하면 부르지도 기록하지도 않음, 개장 전은 1·2·4·4분이고 08:50·09:50이 마감",
            case_head_deadline,
        ),
        ("응답 검증: 목록 밖 id·다른 키·501자 reason은 형식 오류, overnight을 고른 응답은 통과", case_head_response),
        (
            "교체 한도: 장중 교체 두 번 뒤 다른 부하는 capped이고 계획 부하가 그대로, 같은 부하 ok와 개장 전 교체는 세지 않음,"
            " 입력의 switches_left가 2·1·0으로 줄고 current가 그때의 계획 부하",
            case_head_capped,
        ),
        ("계획 부하: 개장 전 판단이 ok가 아닌 날은 전 거래일 마감 때의 부하로, 기간 첫날은 cash로 시작", case_head_plan),
        ("ok·capped 판단의 노트가 같은 날 다음 판단과 다음 거래일 첫 판단에 들어가고, 사이에 실패가 있어도 마지막 ok·capped 노트", case_head_notes),
    ],
    "03": [
        (
            "장중 교체: 교체 시각 다음 봉 시가에서 한 단계 불리한 호가로 switch_head 매도(이익 난 인버스는 세금)와 hold_* 매수,"
            " 대기 매수는 switch_head 취소, 대기 매도는 사유가 switch_head로, 체결가·수수료·세금·정수 주가 손 계산과 같음",
            case_paper_switch,
        ),
        ("15:15 교체로 쓰기 시작한 last_half_hour는 그날 사지 않음(하루 내내 쓴 계좌는 15:00 신호로 삼)", case_paper_late_switch),
        ("overnight: 마감 10분 전 봉의 매수가 종가 동시호가에, 종가 동시호가 분의 매도가 다음 거래일 시가 동시호가에 체결", case_paper_overnight),
        ("같은 판단 기록으로 돌린 morning 계좌는 장중 교체를 하지 않고 head 계좌만 교체", case_paper_morning),
        (
            "결함일에는 schedule의 교체와 새 주문이 없고 부하와 대기 주문이 다음 거래일로 넘어가며, 다음 거래일 개장 전에 교체,"
            " 전 거래일에 장중 교체가 있었으면 결함일에는 그 마감 때의 부하를 이어 쓰고 다음 날 같은 부하면 교체하지 않음",
            case_paper_defect,
        ),
        (
            "하루 한도 뒤 교체 매수는 entry_blocked, 기간 마지막 거래일의 교체 매수는 last_day이고 남은 보유는 종가 동시호가에 팖,"
            " 같은 시세의 두 계좌 중 한 계좌에서만 한도 발동",
            case_paper_limits,
        ),
        ("규칙 헤드는 전 거래일까지 마지막 200개로 본 추세가 위면 hold_kodex200, 평균과 같거나 낮으면 cash", case_paper_rule),
        (
            "장중 교체 다음 날 시작 부하가 전 거래일 마감 때의 부하와 같으면 개장 전에 팔지 않고 이어 가고,"
            " 장중 교체로 마감 때 쓰는 부하가 하루 안에 끝나는 부하면 그날 종가 동시호가에 팖",
            case_paper_after_switch,
        ),
        ("한 주 한도나 누적 한도만 진입을 막는 날의 장중 교체 매수도 entry_blocked", case_paper_blocked),
    ],
    "04": [
        (
            "v4-decide: 휴장일·기간 밖은 받기·호출 없이 파일 그대로 exit 0, 차례가 아니면 V4_SLOT_INVALID, 마감에 시작하면 받기·호출·판단 행 없이"
            " 다음 정산에서 missing, 같은 차례 두 번째는 받기·호출 없음, 당일 1분봉 받기에 s + 2분까지 남은 초(차례에 시작하면 120초),"
            " 그때까지 못 받으면 today_market·news가 null인 채로 마감 전에 판단, 시세 사본과 정산은 그대로",
            case_run_decide,
        ),
        (
            "정산은 판단 행이 없는 차례마다 missing(개장 전·장중·마감 전), 오래된 날부터 하고 검증에 실패한 날에서 멈춤,"
            " 같은 날 v4-daily 두 번째는 판단·정산이 더 생기지 않음, 휴장일에는 아무것도 하지 않음",
            case_run_settle,
        ),
        ("판정 경계: 판단 실패 한 번이면 불통과, capped만 있으면 통과, 최대 낙폭 정확히 20%면 불통과, 5거래일을 다 정산하기 전에는 판정 없음(null)", case_run_verdict),
        ("report.json·decisions.csv·trades CSV의 형식과 값, 같은 저장 상태로 두 번 만들면 실행 시각을 뺀 보고서 내용이 같음", case_run_report),
    ],
    "05": [
        (
            "실행 차례: 정상일·연초 첫 거래일·수능일의 아침·판단 차례, 판단 마감(s + 5분)과 09:50 경계, 기간 밖·휴장일·주말, 다음 실행 예정 시각,"
            " 늦게 켜져도 마감 전이면 한 번 실행하고 지났으면 실행 안 함, 같은 차례는 다시 실행 안 함, 아침은 v4-daily·v4-report(앞이 실패해도),"
            " 판단은 v4-decide --at s, 실행 기록과 종료 코드, 명령에 시간 제한(판단 s + 6분, 아침 09:55)까지 남은 초를 주고"
            " 넘기면 V4_COMMAND_TIMEOUT과 시간 초과를 남긴 뒤 다음 차례를 실행",
            case_serve_schedule,
        ),
        (
            "현황 웹페이지: GET /에 자동 실행 상태·기간과 정산 진행·오늘 판단(지금 부하, 남은 교체, 차례별 상태·부하·이유와 대기·없음)·지난 판단·"
            "최신 보고서의 계좌와 판정·실행 기록 마지막 40줄, 옛 보고서·40줄 앞 기록은 없음, 이유 속 HTML은 글자로, 다른 경로 404·POST 501,"
            " settled_days 표가 없는 첫날 파일도 바꾸지 않음",
            case_serve_page,
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

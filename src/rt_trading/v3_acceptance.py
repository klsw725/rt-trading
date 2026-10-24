"""v3 acceptance. PRD별 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import contextlib
import copy
import csv
import functools
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from rt_trading import codex, engine, fixtures, market_data, report, sessions, store, v3_daily, v3_head, v3_paper, v3_report, v3_serve
from rt_trading.toss import KST

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "docs" / "specs" / "v3" / "fixtures"
FAR_FUTURE_MS = 4_102_444_800_000  # 2100-01-01. 가짜 토큰이 갱신되지 않게 한다


# PRD 01 Codex 헤드


def _prd01() -> dict:
    return json.loads((FIXTURES / "prd01-head.json").read_text(encoding="utf-8"))


@contextlib.contextmanager
def _temp_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _daily_db(path: Path, market: dict, future: bool = False) -> None:
    """candles_1d만 있는 v1 표 구조의 DB. 적지 않은 날은 기본 수정 종가이고, 원가격도 수정 종가와 같다."""
    days, day = [], Date.fromisoformat(market["start"])
    while day <= Date.fromisoformat(market["end"]):
        if day.weekday() < 5 and day.isoformat() not in market["holidays"]:
            days.append(day.isoformat())
        day += timedelta(days=1)
    prices = {date: {**market["base"], **market["changes"].get(date, {})} for date in days}
    if future:
        prices |= market["future"]
    rows = [(symbol, date, price, price, price, price, 1000, price) for date, symbols in prices.items() for symbol, price in symbols.items()]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"CREATE TABLE candles_1d ({store.TABLES['candles_1d']})")
        db.executemany("INSERT INTO candles_1d VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _expected_input(fixture: dict) -> dict:
    """손으로 계산한 기대 입력. 부하 설명과 백테스트 결과는 저장소의 부하 설명 표에서 넣는다."""
    expected = copy.deepcopy(fixture["expected_input"])
    table = {row["id"]: row for row in v3_head.SUBORDINATES}
    expected["subordinates"] = [
        {
            "id": item["id"],
            "description": table[item["id"]]["description"],
            "backtest": table[item["id"]]["backtest"],
            "recent_returns_pct": item["recent_returns_pct"],
            "holding": item["holding"],
        }
        for item in expected["subordinates"]
    ]
    return expected


def _same(actual: object, expected: object) -> None:
    """키 순서까지 같은지 본다."""
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False), (actual, expected)


def _token_file(directory: Path, token: dict) -> None:
    (directory / "auth").mkdir(parents=True)
    content = {"access": token["access"], "refresh": token["refresh"], "expires": FAR_FUTURE_MS, "account_id": token["account_id"]}
    (directory / "auth" / "codex.json").write_text(json.dumps(content), encoding="utf-8")


class _Clock:
    """가짜 시계. 기다린 시간을 기록한다."""

    def __init__(self, day: str, clock: str) -> None:
        self.now = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=KST)
        self.waits: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode()


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
        if kind in ("http", "rate"):
            return httpx.Response(500 if kind == "http" else 429, text="오류")
        if kind == "failed_event":
            return httpx.Response(200, content=_sse([{"type": "response.failed", "response": {"error": {"code": "server_error", "message": "실패"}}}]))
        reply = text[0] if text else self.fixture["ok_text"] if kind == "ok" else self.fixture["invalid_text"]
        model = {"model": self.fixture["reported_model"]}
        half = len(reply) // 2
        events = [
            {"type": "response.created", "response": model},
            {"type": "response.output_text.delta", "delta": reply[:half]},
            {"type": "response.output_text.delta", "delta": reply[half:]},
            {"type": "response.completed", "response": model},
        ]
        return httpx.Response(200, content=_sse(events))


def _decide(fixture: dict, tmp: Path, day: str, start: str, attempts: list[list], content: dict, token: bool = True):
    """가짜 Codex와 가짜 시계로 판단 하나를 정한다. (기록한 행, 시계, 가짜 Codex)."""
    token_dir = tmp / ("token" if token else "no-token")
    if token and not token_dir.exists():
        _token_file(token_dir, fixture["token"])
    clock = _Clock(day, start)
    fake = _Codex(fixture, clock, attempts)
    with httpx.Client(transport=httpx.MockTransport(fake.handler)) as http:
        row = v3_head.decide(
            tmp / "paper.sqlite3", tmp / "head", day, content, http, lambda: codex.credentials(token_dir), clock, clock.sleep
        )
    return row, clock, fake


def _decision_rows(path: Path, rows: list[dict]) -> list[dict]:
    for row in rows:
        v3_head.insert(path, row)
    return v3_head.read(path)


def case_head_input() -> None:
    fixture = _prd01()
    expected = _expected_input(fixture)
    with _temp_dir() as tmp:
        decisions = _decision_rows(tmp / "given.sqlite3", fixture["decisions"])
        for future in (False, True):
            market = tmp / f"market-{future}.sqlite3"
            _daily_db(market, fixture["market"], future)
            content = v3_head.build_input("2026-01-05", fixture["month_days"], market, fixture["accounts"], decisions)
            _same(content, expected)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            row, _, fake = _decide(fixture, tmp, "2026-01-05", "07:30:00", [["ok", 5]], content)
        assert row["status"] == "ok" and row["reported_model"] == fixture["reported_model"], row
        (request,) = fake.requests
        text = json.dumps(content, ensure_ascii=False)
        prompt = (ROOT / "src" / "rt_trading" / "v3_head_prompt.txt").read_text(encoding="utf-8")
        cache_key = hashlib.sha256(json.dumps({"s": prompt, "u": text}, ensure_ascii=True, sort_keys=True).encode()).hexdigest()
        assert request.method == "POST" and str(request.url) == "https://chatgpt.com/backend-api/codex/responses", request.url
        assert request.headers["authorization"] == f"Bearer {fixture['token']['access']}"
        assert request.headers["chatgpt-account-id"] == fixture["token"]["account_id"]
        assert request.headers["originator"] == "rt-trading" and request.headers["user-agent"] == "rt-trading"
        _same(
            json.loads(request.content),
            {
                "model": "gpt-6-sol",
                "store": False,
                "stream": True,
                "instructions": prompt,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
                "text": {"verbosity": "medium"},
                "include": ["reasoning.encrypted_content"],
                "prompt_cache_key": cache_key,
            },
        )
        stored = [request.content, (tmp / "paper.sqlite3").read_bytes(), output.getvalue().encode()]
        stored += [path.read_bytes() for path in sorted((tmp / "head").rglob("*")) if path.is_file()]
        for secret in (fixture["token"]["access"], fixture["token"]["refresh"], fixture["token"]["account_id"]):
            assert all(secret.encode() not in blob for blob in stored), secret
        assert sorted(path.name for path in (tmp / "head" / "2026-01-05").iterdir()) == ["decision.json", "input.json", "response.txt"]
        assert json.loads((tmp / "head" / "2026-01-05" / "input.json").read_text(encoding="utf-8")) == content
        assert json.loads((tmp / "head" / "2026-01-05" / "decision.json").read_text(encoding="utf-8")) == row
        assert (tmp / "head" / "2026-01-05" / "response.txt").read_text(encoding="utf-8") == fixture["ok_text"]


def case_head_response() -> None:
    fixture = _prd01()
    for item in fixture["responses"]:
        lengths = item.get("lengths")
        text = item.get("text") or json.dumps(
            {"subordinate": "cash", "reason": "가" * lengths["reason"], "notes": "나" * lengths["notes"]}, ensure_ascii=False
        )
        try:
            value = v3_head.parse(text)
        except v3_head.HeadError as error:
            assert not item["valid"] and error.code == "V3_HEAD_RESPONSE_INVALID", item["name"]
        else:
            assert item["valid"] and set(value) == {"subordinate", "reason", "notes"}, item["name"]
    fenced = fixture["responses"][1]["text"]
    with _temp_dir() as tmp:
        row, _, _ = _decide(fixture, tmp, "2026-01-05", "07:30:00", [["ok", 5, fenced]], {"decision_date": "2026-01-05"})
        assert (row["status"], row["subordinate"], row["reason"]) == ("ok", "cash", "쉰다"), row


def case_head_deadline() -> None:
    fixture = _prd01()
    for case in fixture["deadline_cases"]:
        with _temp_dir() as tmp:
            content = {"decision_date": case["date"]}
            row, clock, fake = _decide(fixture, tmp, case["date"], case["start"], case["attempts"], content, not case.get("no_token"))
            expected = case["expected"]
            if expected is None:
                assert row is None and not fake.requests, case["name"]
                assert not (tmp / "paper.sqlite3").exists() and not (tmp / "head").exists(), case["name"]
                continue
            actual = {key: row[key] for key in ("status", "subordinate", "attempts", "error", "finished_at")}
            assert actual == {key: value for key, value in expected.items() if key != "waits"}, (case["name"], actual)
            assert clock.waits == expected["waits"], (case["name"], clock.waits)
            assert len(fake.requests) == (0 if case.get("no_token") else expected["attempts"]), case["name"]
            if row["status"] != "ok":
                assert row["reason"] == "" and row["notes"] == "", case["name"]
            assert v3_head.read(tmp / "paper.sqlite3") == [row], case["name"]
            # 같은 날 다시 부르면 기록한 행을 그대로 돌려준다
            again, _, again_fake = _decide(fixture, tmp, case["date"], case["start"], [], content, not case.get("no_token"))
            assert again == row and not again_fake.requests, case["name"]


def case_head_notes() -> None:
    fixture = _prd01()
    case = fixture["notes_case"]
    with _temp_dir() as tmp:
        market = tmp / "market.sqlite3"
        _daily_db(market, fixture["market"])
        first = case["first"]
        row, _, _ = _decide(fixture, tmp, first["date"], first["start"], [["ok", 5, first["text"]]], {"decision_date": first["date"]})
        assert row["status"] == "ok", row
        row, _, _ = _decide(fixture, tmp, case["failed"]["date"], case["failed"]["start"], [], {"decision_date": case["failed"]["date"]}, False)
        assert row["status"] == "failed", row
        content = v3_head.build_input(case["next"], fixture["month_days"], market, fixture["accounts"], v3_head.read(tmp / "paper.sqlite3"))
        assert content["notes"] == case["expected_notes"], content["notes"]
        _same(content["recent_decisions"], case["expected_recent"])


# PRD 02 부하와 하루 엔진


def _prd02() -> dict:
    return json.loads((FIXTURES / "prd02-paper.json").read_text(encoding="utf-8"))


def _market(spec: dict, path: Path, omit_after: dict[str, str] | None = None) -> market_data.MarketData:
    """v1 fixtures.build spec으로 시세 DB를 만들어 v1 검증으로 읽는다. omit_after의 날은 그 시각 뒤 봉을 뺀다."""
    spec = copy.deepcopy(spec)
    for day in spec["days"]:
        after = (omit_after or {}).get(day["date"])
        if after:
            later = [time for time in sessions.session(day["date"], sessions.exceptions()).bars if time > after]
            for symbol in fixtures.SYMBOLS:
                day.setdefault("bars", {}).setdefault(symbol, {}).update(dict.fromkeys(later))
    fixtures.build(spec, path)
    data, violations = market_data.load(path)
    assert not violations, market_data.describe(violations)
    return data


def _subordinates(tests: dict) -> dict[str, v3_paper.Subordinate]:
    """시험용 부하: 적은 날짜·시각에 적은 주문을 낸다(결함일에도)."""
    extra = {}
    for name, test in tests.items():
        script = {(day, clock): engine.Order(kind, symbol, "signal") for day, clock, kind, symbol in test["orders"]}
        extra[name] = v3_paper.Subordinate(lambda script=script: lambda view: script.get((view.date, view.time)), test["overnight"], None)
    return v3_paper.SUBORDINATES | extra


def _check_records(name: str, records: engine.Records, expected: dict) -> None:
    fills = [[f.time, f.symbol, f.side, f.quantity, f.price, f.fee, f.tax, f.reason] for f in records.fills]
    assert fills == expected["fills"], (name, fills)
    discarded = [[d.time, d.kind, d.symbol, d.reason] for d in records.discarded]
    assert discarded == expected["discarded"], (name, discarded)
    assert records.closes == expected["closes"], (name, records.closes)
    assert [list(limit) for limit in records.limits] == expected["limits"], (name, records.limits)
    assert [trade.pnl for trade in records.trades] == expected["trade_pnls"], (name, records.trades)


def _paper_case(key: str) -> None:
    case = _prd02()["cases"][key]
    with _temp_dir() as tmp:
        data = _market(case["market"], tmp / "market.sqlite3", case.get("omit_after"))
    subordinates = _subordinates(case.get("tests", {}))
    for name, account in case["accounts"].items():
        records = v3_paper.simulate(data, data.days, case["start"], case["final_day"], account["plan"], subordinates)
        _check_records(name, records, account["expected"])


def case_paper_continue_switch() -> None:
    _paper_case("continue_switch_last_day")


def case_paper_defect_cancel() -> None:
    _paper_case("defect_cancel")


def case_paper_limits() -> None:
    _paper_case("limits")


def case_paper_rule() -> None:
    rule = _prd02()["rule"]
    for choice in rule["choices"]:
        closes = [choice["base"]] * (choice["count"] - 1) + [choice["last"]]
        if "first" in choice:
            closes[0] = choice["first"]
        assert v3_paper.rule_choice(closes) == choice["expected"], choice["name"]
    plan = rule["plan"]
    days, day = [], Date(2025, 1, 1)
    while len(days) < plan["count"]:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    closes = {date: plan["changes"].get(str(index), plan["base"]) for index, date in enumerate(days)}
    data = market_data.MarketData(days, {}, {}, {}, [], {"069500": {date: (price, price) for date, price in closes.items()}})
    chosen = v3_paper.rule_plan(data, [days[int(index)] for index in plan["expected"]])
    assert chosen == {days[int(index)]: value for index, value in plan["expected"].items()}, chosen


# PRD 03 매일 실행


def _prd03() -> dict:
    return json.loads((FIXTURES / "prd03-daily.json").read_text(encoding="utf-8"))


def _weekdays(first: str, last: str) -> list[str]:
    days, day = [], Date.fromisoformat(first)
    while day <= Date.fromisoformat(last):
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()}


def _source_days(fixture: dict, history: list[str], run: dict) -> list[dict]:
    """그 실행의 원본 시세 spec. broken의 날은 fixture의 봉을 빼고, omit_after의 날은 그 시각 뒤 봉을 뺀다."""
    days = [{"date": history[0], "prices": fixture["history"]["prices"]}] + [{"date": day} for day in history[1:]]
    for day in run["source"]:
        spec = {"date": day}
        if day in run.get("broken", []):
            spec["bars"] = copy.deepcopy(fixture["broken_bars"])
        if after := fixture.get("omit_after", {}).get(day):
            later = [time for time in sessions.session(day, sessions.exceptions()).bars if time > after]
            spec["bars"] = {symbol: dict.fromkeys(later) for symbol in fixtures.SYMBOLS}
        days.append(spec)
    return days


def _scenario(fixture: dict) -> tuple[list[dict], list[dict], list[list]]:
    """fixture의 실행을 차례로 돌린다. (매일 실행마다의 결과, 보고서마다의 결과, 마지막 판단 기록)."""
    head = _prd01()
    with _temp_dir() as tmp:
        paths = v3_daily.Paths(tmp / "market.sqlite3", tmp / "v3" / "market.sqlite3", tmp / "v3" / "paper.sqlite3", tmp / "v3" / "head")
        period_path, holidays_path = tmp / "v3_period.json", tmp / "krx_holidays.csv"
        period_path.write_text(json.dumps({"start": fixture["start"]}), encoding="utf-8")
        holidays_path.write_text("date,reason\n" + "".join(f"{day},{reason}\n" for day, reason in fixture["holidays"]), encoding="utf-8")
        _token_file(tmp / "token", head["token"])
        history = _weekdays(fixture["history"]["from"], fixture["history"]["to"])
        if "initial_source" in fixture:
            spec = {"source": fixture["initial_source"]}
            fixtures.build({"fetched_at": f"{fixture['runs'][0]['at']}+09:00", "volume": 1000, "days": _source_days(fixture, history, spec)}, paths.source)
        outcomes, reports = [], []
        for run in fixture["runs"]:
            if "report_at" in run:
                clock = _Clock(*run["report_at"].split("T"))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = v3_report.run(paths, period_path, holidays_path, tmp / "reports", clock)
                directory = tmp / "reports" / f"v3-{clock.now.strftime('%Y%m%d-%H%M%S')}"
                files = {path.name: path.read_bytes() for path in sorted(directory.iterdir())} if directory.exists() else {}
                reports.append({"exit": code, "output": output.getvalue(), "files": files})
                continue
            clock = _Clock(*run["at"].split("T"))
            fake = _Codex(head, clock, [["ok", 5, fixture["replies"][run["reply"]]]] if "reply" in run else [])
            fetched: list[float] = []

            def fetch(seconds: float, run: dict = run, fetched: list[float] = fetched, clock: _Clock = clock) -> int:
                fetched.append(seconds)
                if run.get("fetch_timeout"):
                    clock.now += timedelta(seconds=seconds)
                    return 1
                if run.get("fetch_fails"):
                    return 1
                fixtures.build({"fetched_at": f"{run['at']}+09:00", "volume": 1000, "days": _source_days(fixture, history, run)}, paths.source)
                return 0

            token_dir = tmp / ("no-token" if run.get("no_token") else "token")
            before, output = _snapshot(tmp), io.StringIO()
            with contextlib.redirect_stdout(output), httpx.Client(transport=httpx.MockTransport(fake.handler)) as http:
                code = v3_daily.run(paths, period_path, holidays_path, fetch, http, lambda: codex.credentials(token_dir), clock, clock.sleep)
            inputs = [json.loads(json.loads(request.content)["input"][0]["content"][0]["text"]) for request in fake.requests]
            outcomes.append(
                {
                    "exit": code,
                    "output": output.getvalue(),
                    "fetched": bool(fetched),
                    "fetch_budget": fetched[0] if fetched else None,
                    "codex_calls": len(fake.requests),
                    "settled": v3_daily.settled_days(paths.paper),
                    "settled_through": [content["settled_through"] for content in inputs],
                    "unchanged": _snapshot(tmp) == before,
                }
            )
        decisions = [[row["date"], row["status"], row["subordinate"]] for row in v3_head.read(paths.paper)]
    return outcomes, reports, decisions


@functools.cache
def _daily_scenario() -> tuple[list[dict], list[list]]:
    """PRD 03 fixture의 실행을 한 번만 돌려 사례들이 함께 쓴다."""
    outcomes, _, decisions = _scenario(_prd03())
    return outcomes, decisions


def _check_outcome(run: dict, outcome: dict) -> None:
    expected = run["expected"]
    for key in ("exit", "fetched", "codex_calls", "settled"):
        assert outcome[key] == expected[key], (run["name"], key, outcome[key], outcome["output"])
    for text in expected["output"]:
        assert text in outcome["output"], (run["name"], text, outcome["output"])
    if "settled_through" in expected:
        assert outcome["settled_through"] == [expected["settled_through"]], (run["name"], outcome["settled_through"])
    if "fetch_budget" in expected:
        assert outcome["fetch_budget"] == expected["fetch_budget"], (run["name"], outcome["fetch_budget"])
    if expected.get("unchanged"):
        assert outcome["unchanged"], run["name"]


def _check_runs(*indexes: int) -> None:
    runs, (outcomes, _) = _prd03()["runs"], _daily_scenario()
    for index in indexes:
        _check_outcome(runs[index], outcomes[index])


def case_daily_holiday() -> None:
    _check_runs(2)


def case_daily_settle_order() -> None:
    _check_runs(3, 4, 7)
    assert _daily_scenario()[1] == _prd03()["decisions"], _daily_scenario()[1]


def case_daily_decide_on_failure() -> None:
    _check_runs(3, 5)
    fixture = _prd03()
    outcomes, _, _ = _scenario(fixture | {"runs": fixture["cutoff_runs"], "initial_source": []})
    for run, outcome in zip(fixture["cutoff_runs"], outcomes, strict=True):
        _check_outcome(run, outcome)


def case_daily_twice() -> None:
    _check_runs(0, 1)


def case_daily_calendar() -> None:
    _check_runs(6)


# PRD 04 보고서와 판정


def _prd04() -> dict:
    return json.loads((FIXTURES / "prd04-report.json").read_text(encoding="utf-8"))


@functools.cache
def _report_scenario() -> tuple[list[dict], list[dict], list[list]]:
    return _scenario(_prd04())


def _matches(actual: object, expected: object) -> bool:
    """JSON 값이 같은지 본다. 소수는 1e-12 안의 차이만 허용한다."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(_matches(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(map(_matches, actual, expected))
    if isinstance(expected, float):
        return isinstance(actual, float) and abs(actual - expected) < 1e-12
    return type(actual) is type(expected) and actual == expected


def case_report_verdict() -> None:
    fixture = _prd04()
    for case in fixture["verdict_cases"]:
        equity = case["equity"]
        records = engine.Records([("", engine.INITIAL_CASH), ("2026-04-07 09:01", equity)], {"2026-04-07": equity}, [], [], [], [])
        assert v3_report.verdict(records, case["statuses"]) == case["expected"], case["name"]
    _, reports, _ = _report_scenario()
    mid, expected = json.loads(reports[0]["files"]["report.json"]), fixture["mid"]
    assert {key: mid["run"][key] for key in expected["run"]} == expected["run"], mid["run"]
    assert mid["decisions"] == expected["decisions"] and mid["verdict"] is None, mid
    assert (mid["accounts"][0]["id"], mid["accounts"][0]["final_equity"]) == ("head", expected["head_final_equity"]), mid["accounts"][0]
    assert "정산 2/5" in reports[0]["output"], reports[0]["output"]
    final = json.loads(reports[1]["files"]["report.json"])
    assert final["verdict"] == fixture["final"]["report"]["verdict"], final["verdict"]
    assert "판정: 불통과" in reports[1]["output"], reports[1]["output"]


def case_report_files() -> None:
    expected = _prd04()["final"]
    _, reports, _ = _report_scenario()
    result = reports[1]
    assert result["exit"] == 0 and sorted(result["files"]) == expected["files"], sorted(result["files"])
    content = json.loads(result["files"]["report.json"])
    assert _matches(content, expected["report"]), content
    rows = list(csv.reader(io.StringIO(result["files"]["decisions.csv"].decode("utf-8"))))
    assert rows[0] == v3_report.DECISION_COLUMNS, rows[0]
    for row, want in zip(rows[1:], expected["decisions_csv"], strict=True):
        assert row[:6] == want[:6] and abs(float(row[6]) - want[6]) < 1e-12, row
    for name in expected["files"]:
        if name.startswith("trades-"):
            trades = list(csv.reader(io.StringIO(result["files"][name].decode("utf-8"))))
            assert trades[0] == report.TRADE_COLUMNS, name
    trades = list(csv.reader(io.StringIO(result["files"]["trades-head.csv"].decode("utf-8"))))
    assert trades[1:] == expected["trades_head"], trades


def case_report_repeatable() -> None:
    _, reports, _ = _report_scenario()
    assert reports[1]["files"] == reports[2]["files"], "같은 저장 상태의 두 보고서가 다르다"


# PRD 05 실행 환경과 현황 웹페이지


def _prd05() -> dict:
    return json.loads((FIXTURES / "prd05-serve.json").read_text(encoding="utf-8"))


def case_serve_schedule() -> None:
    fixture = _prd05()
    for item in fixture["schedule"]:
        now = datetime.fromisoformat(item["now"]).replace(tzinfo=KST)
        assert v3_serve.due(now) == item["due"], (item["now"], v3_serve.due(now))
        assert v3_serve.next_run(now) == datetime.fromisoformat(item["next"]).replace(tzinfo=KST), (item["now"], v3_serve.next_run(now))
    with _temp_dir() as tmp:
        log_path, status, done, executed = tmp / "v3" / "daily.log", {"started_at": "", "running": None, "last": None}, None, []

        def execute(command: str, file: io.TextIOBase) -> int:
            executed.append(command)
            file.write(f"{command} 출력\n")
            return fixture["tick_exits"][command]

        for item in fixture["ticks"]:
            now, before = datetime.fromisoformat(item["now"]).replace(tzinfo=KST), len(executed)
            done = v3_serve.tick(done, status, log_path, lambda now=now: now, execute)
            assert executed[before:] == item["runs"], (item["now"], executed[before:])
        assert log_path.read_text(encoding="utf-8") == fixture["tick_log"], log_path.read_text(encoding="utf-8")
        assert status["last"] == fixture["tick_last"] and status["running"] is None, status


def case_serve_page() -> None:
    fixture = _prd05()["page"]
    with _temp_dir() as tmp:
        paths = v3_daily.Paths(tmp / "market.sqlite3", tmp / "v3" / "market.sqlite3", tmp / "v3" / "paper.sqlite3", tmp / "v3" / "head")
        period_path, holidays_path, report_dir, log_path = tmp / "v3_period.json", tmp / "krx_holidays.csv", tmp / "reports", tmp / "v3" / "daily.log"
        period_path.write_text(json.dumps({"start": fixture["start"]}), encoding="utf-8")
        holidays_path.write_text("date,reason\n" + "".join(f"{day},{reason}\n" for day, reason in fixture["holidays"]), encoding="utf-8")
        paths.paper.parent.mkdir(parents=True)
        for row in fixture["decisions"]:
            v3_head.insert(paths.paper, dict.fromkeys(v3_head.COLUMNS, "") | {"attempts": 0} | row)
        with closing(sqlite3.connect(paths.paper)) as db, db:
            db.execute(f"CREATE TABLE settled_days ({v3_daily.SETTLED})")
            db.executemany("INSERT INTO settled_days VALUES (?, ?)", [(day, "") for day in fixture["settled"]])
        for name, content in fixture["reports"].items():
            (report_dir / name).mkdir(parents=True)
            (report_dir / name / "report.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        log_path.write_text("".join(f"줄 {number:02d}\n" for number in range(1, fixture["log_lines"] + 1)), encoding="utf-8")
        now = datetime.fromisoformat(fixture["now"]).replace(tzinfo=KST)
        httpd = v3_serve.server(
            "127.0.0.1", 0, lambda: v3_serve.page(paths, period_path, holidays_path, report_dir, log_path, fixture["status"], now)
        )
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
        first = tmp / "first" / "paper.sqlite3"
        first.parent.mkdir()
        v3_head.insert(first, dict.fromkeys(v3_head.COLUMNS, "") | {"attempts": 0} | fixture["decisions"][0])
        before = first.read_bytes()
        text = v3_serve.page(paths._replace(paper=first), period_path, holidays_path, report_dir, log_path, fixture["status"], now)
        assert fixture["first_day"] in text and first.read_bytes() == before, text


CASES = {
    "01": [
        ("헤드 입력: 손 계산과 같고 판단일 뒤 일봉을 바꿔도 같음, 요청 본문은 지시문과 입력뿐, 인증 정보는 어디에도 없음", case_head_input),
        ("응답 검증: 코드 블록 허용, 목록 밖 id·JSON 아님·다른 키·길이 초과(500·2,000자 통과, 501·2,001자 실패)는 형식 오류", case_head_response),
        ("다시 부르기 1·2·4·4분, 마감(정상일 08:50, 10시 개장일 09:50) 전 ok·뒤 late, 마감까지 실패·형식 오류, 토큰 없음, 마감 뒤 시작", case_head_deadline),
        ("ok 판단의 노트가 다음 입력에 들어가고, 실패한 날이 끼어도 마지막 ok 노트가 들어간다", case_head_notes),
    ],
    "02": [
        (
            "같은 부하면 보유·다음 날 시가 매도가 이어짐, 교체 날 시가 동시호가 switch_head 매도 뒤 보유 부하 매수, 마지막 날 개장 전 매수·새 매수 없음과"
            " 종가 청산, 동시호가·한 단계 불리한 호가·수수료·인버스 세금·정수 주",
            case_paper_continue_switch,
        ),
        ("결함일에는 plan이 바뀌어도 교체·새 주문 없이 넘어가고, 다음 날 교체하며 남은 대기 매수를 switch_head로 취소", case_paper_defect_cancel),
        ("손실 한도는 계좌마다 따로 발동하고, 한 주 한도가 막는 동안 보유 부하의 개장 전 매수는 entry_blocked", case_paper_limits),
        ("규칙 헤드: 전 거래일까지 마지막 200개로 본 추세가 위면 hold_kodex200, 평균과 같거나 낮으면 cash", case_paper_rule),
    ],
    "03": [
        ("휴장일에는 시세 받기·Codex 호출이 없고 파일이 그대로이며 exit 0", case_daily_holiday),
        ("정산은 오래된 날부터 하고 검증에 실패한 날에서 멈추며, 판단이 없는 거래일은 missing·cash로 정산, 기간 끝까지 정산하면 기간 종료", case_daily_settle_order),
        (
            "검증 실패나 시세 받기 실패에도 그날 판단을 하고, 입력의 settled_through는 정산을 마친 마지막 날,"
            " 시세 받기는 판단 마감 15분 전까지(07:30 시작이면 3,900초)이고 그 뒤 시작하면 건너뛰고 판단",
            case_daily_decide_on_failure,
        ),
        ("같은 날 두 번 실행하면 두 번째에는 Codex 호출이 없고 판단·정산이 더 생기지 않음", case_daily_twice),
        ("원본 거래일과 휴장일 표의 달력이 다르면 V3_CALENDAR_MISMATCH로 정산하지 않음", case_daily_calendar),
    ],
    "04": [
        ("판정 경계: 판단 실패 한 번이면 불통과, 최대 낙폭 정확히 20%면 불통과, 5거래일을 다 정산하기 전에는 판정 없음(null)", case_report_verdict),
        ("report.json·decisions.csv·trades CSV의 형식과 값(ok·실패 판단, 부하 교체, 기간 안의 결함일이 있는 사례)", case_report_files),
        ("같은 저장 상태로 두 번 만들면 실행 시각을 뺀 보고서 내용이 같다", case_report_repeatable),
    ],
    "05": [
        (
            "실행 차례: 평일 07:30·08:20 경계와 09:50 마감, 주말 없음, 다음 실행 예정 시각(금요일 뒤 월요일 07:30),"
            " 차례마다 v3-daily 다음 v3-report를 한 번씩(앞이 실패해도), 실행 기록과 종료 코드",
            case_serve_schedule,
        ),
        (
            "현황 웹페이지: GET /에 자동 실행 상태·정산 진행·판단·최신 보고서의 계좌와 판정·실행 기록 마지막 40줄,"
            " 옛 보고서·40줄 앞 기록은 없음, 이유 속 HTML은 글자로, 다른 경로 404·POST 501, 첫날 파일도 바꾸지 않음",
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

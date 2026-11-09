"""v6 자동 실행과 보기 전용 현황 웹페이지(v6 SPEC 5.1·8절, PRD 05)."""

import html
import json
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from datetime import time as Time
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from rt_trading import v3_head, v3_serve, v6_market, v6_paper, v6_run, web
from rt_trading.toss import KST

PORT = 8082  # v5는 8081
LOG_LINES = 40
WATCH_LIMIT = timedelta(minutes=9)  # 10분 평가는 다음 차례 1분 전까지
DECIDE_LIMIT = timedelta(minutes=7)  # 판단은 E + 2분까지
REPORT_LIMIT = timedelta(minutes=9)  # 보고서는 10:09까지(10:10 평가 전)
REPORT_AT = Time(10, 0)
SIDES = {"BUY": "매수", "SELL": "매도"}
ORIGINS = {"decision": "판단", "forced": "강제"}
BLOCKS = {"daily_limit": "하루 한도", "weekly_limit": "한 주 한도", "cumulative_limit": "누적 한도", "risk_data_missing": "기준 평가 없음", "forced_sale": "강제 매도 중"}


def due(now: datetime, done: set[str]) -> tuple[str, str, datetime] | None:
    """지금 할 일 (열쇠, 명령, 시간 제한 시각). 10분 평가, 그 시의 40분 평가를 마친 뒤의 판단, 10:00 평가를 마친 뒤의 보고서 순서다. 한 일은 done에 있다."""
    now = now.astimezone(KST)
    slot = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    if (key := f"watch {slot:%Y-%m-%d %H:%M}") not in done:
        return key, "v6-watch", slot + WATCH_LIMIT
    decided = now.replace(minute=40, second=0, microsecond=0)
    key = f"decide {decided:%Y-%m-%d %H:%M}"
    if decided <= now < decided + v6_market.FILL_AFTER and f"watch {decided:%Y-%m-%d %H:%M}" in done and key not in done:
        return key, f"v6-decide --at {decided:%H:%M}", decided + DECIDE_LIMIT
    reported = datetime.combine(now.date(), REPORT_AT, KST)
    key = f"report {now.date()}"
    if reported <= now < reported + REPORT_LIMIT and f"watch {reported:%Y-%m-%d %H:%M}" in done and key not in done:
        return key, "v6-report", reported + REPORT_LIMIT
    return None


def tick(done: set[str], status: dict, log_path: Path, clock: Callable[[], datetime], execute: Callable[[str, TextIO, float], int | None]) -> None:
    """한 번의 확인. 할 일이 있으면 시간 제한 안에서 명령을 따로 실행하고 run.log에 이어 쓴다."""
    job = due(clock(), done)
    if job is None:
        return
    key, command, stop = job
    started = _stamp(clock())
    status["running"] = {"job": key, "started_at": started}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as file:
        file.write(f"=== {started} {key} ===\n")
        file.flush()
        left = (stop - clock()).total_seconds()
        code = execute(command, file, left) if left > 0 else None
        if code is None:
            file.write(f"V6_COMMAND_TIMEOUT: {stop:%H:%M}까지 마치지 못해 멈췄다({command})\n")
    done.add(key)
    status["last"] = {"job": key, "started_at": started, "finished_at": _stamp(clock()), "exit": code}
    status["running"] = None


def run(paths: v6_run.Paths, host: str, port: int, refresh: Callable[[], None]) -> int:
    """켜질 때 장 운영 정보를 받고(refresh), 웹페이지를 열고, 10초마다 확인해 할 일을 한다. 멈출 때까지 돈다."""
    clock = lambda: datetime.now(KST)
    status = {"started_at": _stamp(clock()), "running": None, "last": None}
    refresh()
    httpd = v3_serve.server(host, port, lambda: page(paths, status, clock()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    done: set[str] = set()
    while True:
        tick(done, status, paths.root / "run.log", clock, _execute)
        time.sleep(10)


def _execute(command: str, file: TextIO, seconds: float) -> int | None:
    """명령을 seconds초 안에 실행해 종료 코드를 돌려준다. 끝나지 않으면 멈추고 None이다."""
    try:
        return subprocess.run(["rt-trading", *command.split()], stdout=file, stderr=subprocess.STDOUT, timeout=seconds).returncode
    except subprocess.TimeoutExpired:
        return None


# 현황 웹페이지


def page(paths: v6_run.Paths, status: dict, now: datetime) -> str:
    """현황 웹페이지 HTML(web SPEC). 장부는 공유 잠금 아래 읽기 전용으로 열고, 파일을 바꾸지 않는다."""
    now = now.astimezone(KST)
    cards = [web.card("실행", _running(status))]
    sections = []
    if paths.paper.exists() and paths.lock.exists():
        with v6_run.reading(paths) as conn:
            data = _read(conn, now)
        calendar = v6_market.load_calendar(paths.calendar)
        cards[0] = web.card("실행", _running(status) + _period(data, calendar, now))
        cards += [web.card("모의 계좌", _account(data)), web.card("손실 한도", _limits(data)), web.card("판정 지표", _criteria(paths, now))]
        sections += [_holdings(data), _decisions(data, calendar, now), _fills(data)]
    else:
        cards.append(web.card("모의 계좌", _p("장부 없음. 시작일 설정 뒤 첫 실행에서 만든다.")))
    sections.append(f'<section><h2>실행 기록</h2><pre>{html.escape(_tail(paths.root / "run.log"))}</pre></section>')
    body = f'<div class="cards">{"".join(cards)}</div>' + "".join(sections)
    return web.document("rt-trading v6 현황", "rt-trading v6 모의 운용 현황", body)


def _read(conn, now: datetime) -> dict:
    day = now.date().isoformat()
    monday = (now.date() - timedelta(days=now.weekday())).isoformat()
    names = {symbol: name for symbol, name in conn.execute("SELECT symbol, name FROM instruments")}
    return {
        "meta": dict(conn.execute("SELECT key, value FROM meta")),
        "valuation": conn.execute("SELECT slot, at, equity, krw, usd, rate, used, stale, stale_detail FROM valuations WHERE kind = 'watch' ORDER BY slot DESC LIMIT 1").fetchone(),
        "cash": dict(conn.execute("SELECT currency, amount FROM cash")),
        "positions": conn.execute("SELECT symbol, market, currency, quantity, avg_price FROM positions ORDER BY symbol").fetchall(),
        "last": {key: value for key, value in conn.execute("SELECT key, value FROM last_values")},
        "names": names,
        "bases": {kind: conn.execute("SELECT equity, source FROM baselines WHERE kind = ? AND key = ?", (kind, key)).fetchone() for kind, key in (("daily", day), ("weekly", monday))},
        "limits": conn.execute("SELECT kind, key FROM limits WHERE (kind = 'daily' AND key = ?) OR (kind = 'weekly' AND key = ?) OR kind = 'cumulative'", (day, monday)).fetchall(),
        "blocked": v6_paper.blocked_reasons(conn, now),
        "decisions": {row[0]: row for row in conn.execute("SELECT time, status, positions, strategy, reason FROM decisions WHERE time LIKE ? ORDER BY time", (f"{day}%",))},
        "fills": conn.execute(
            "SELECT at, origin, symbol, side, quantity, amount, currency, commission, tax, risk_cost, sec_fee, fx_usd, fx_krw FROM fills WHERE at LIKE ? ORDER BY id",
            (f"{day}%",),
        ).fetchall(),
    }


def _running(status: dict) -> str:
    running, last = status.get("running"), status.get("last")
    lines = [_p(f"켠 시각 {status['started_at']}")]
    lines.append(_p(f'<span class="dot run"></span>실행 중: {html.escape(running["job"])}', raw=True) if running else _p('<span class="dot"></span>대기', raw=True))
    if last:
        lines.append(_p(f"마지막 실행: {last['job']} (종료 코드 {last['exit']}, {last['finished_at']})"))
    return "".join(lines)


def _period(data: dict, calendar: dict, now: datetime) -> str:
    meta = data["meta"]
    if "start" not in meta:
        return _p("검증 구간: 시작 전")
    end = datetime.fromisoformat(meta["end"])
    day, passed = datetime.fromisoformat(meta["start"]).date(), 0
    while day < min(now.date(), end.date()):
        passed += bool(v6_market.trading_day(calendar, day.isoformat()))
        day += timedelta(days=1)
    upcoming = _next_decision(calendar, now)
    return _p(f"검증 구간: {meta['start']} ~ {end:%Y-%m-%d %H:%M}, 지난 국내 거래일 {passed}일") + _p(f"다음 판단: {upcoming or '장 운영 정보 없음'}")


def _next_decision(calendar: dict, now: datetime) -> str | None:
    """저장한 장 운영 정보의 마지막 날 다음 날까지 보며 지금 뒤의 첫 판단 차례."""
    stored = [key for market in ("KR", "US") for key in calendar.get(market, {})]
    if not stored:
        return None
    day, last = now.date(), datetime.fromisoformat(max(stored)).date() + timedelta(days=1)
    while day <= last:
        for clock, kind in v6_market.slots(calendar, day.isoformat()):
            if kind == "slot" and datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=KST) > now:
                return f"{day} {clock}"
        day += timedelta(days=1)
    return None


def _account(data: dict) -> str:
    row = data["valuation"]
    if row is None:
        return _p("평가 없음")
    slot, _, equity, krw, usd, rate, used, stale, detail = row
    profit = equity - v6_paper.START_CASH
    rate_text = f"{Decimal(rate):,.2f}원" if rate else "없음"
    old = [key for key, value in json.loads(used).items() if not value[2]]
    parts = [
        f'<div class="big">{equity:,}원</div>',
        _p(web.signed(f"수익 {profit:+,}원 ({v3_head.pct(Decimal(profit) / v6_paper.START_CASH):+.2f}%)", profit), raw=True),
        _p(f"원화 현금 {int(data['cash'].get('KRW', 0)):,}원 · 달러 현금 ${Decimal(data['cash'].get('USD', '0')):,.2f} · 환율 {rate_text}"),
        _p(f"평가 {slot}" + (f" · 낡은 값: {', '.join(old)}" if old else "")),
    ]
    if stale:
        parts.append(f'<p class="alert">stale: {html.escape(detail)}</p>')
    return "".join(parts)


def _limits(data: dict) -> str:
    bases = data["bases"]
    lines = [_p(f"{label} 기준 {f'{base[0]:,}원 ({base[1]})' if base else '아직 없음'}") for label, base in (("오늘 하루", bases["daily"]), ("이번 주", bases["weekly"]))]
    triggered = [f"{kind} {key}" for kind, key in data["limits"]]
    lines.append(_p(f"발동한 한도: {', '.join(triggered) if triggered else '없음'}"))
    lines.append(_p("강제 매도 중" if "forced_sale" in data["blocked"] else "강제 매도 없음"))
    reasons = [BLOCKS.get(reason, reason) for reason in data["blocked"]]
    lines.append(f'<p class="alert">매수 막힘: {html.escape(", ".join(reasons))}</p>' if reasons else _p("매수 막힘 없음"))
    return "".join(lines)


def _criteria(paths: v6_run.Paths, now: datetime) -> str:
    try:
        content, _ = v6_run.build(paths, now)
    except v6_market.V6Error as error:
        return _p(str(error))
    criteria = content["criteria"]
    ledger = criteria["ledger"]
    lines = [
        f"판단 실패 {criteria['failures']['failed']}/{criteria['failures']['slots']}",
        f"최대 낙폭 {content['account']['max_drawdown_pct']:.2f}%",
        f"10분 평가 {criteria['valuations']['recorded']}/{criteria['valuations']['expected']}",
        f"빠진 00:00 평가 {len(criteria['midnight']['missing'])}일",
        f"stale {criteria['stale']['count']}번",
        f"장부 규칙 위반 {sum(value for key, value in ledger.items() if key != 'passed')}번",
    ]
    verdict = content["verdict"] or "검증 구간이 끝난 뒤"
    return "".join(_p(line) for line in lines) + _p(f"판정: {verdict}")


def _holdings(data: dict) -> str:
    rate = Decimal(data["valuation"][5]) if data["valuation"] and data["valuation"][5] else None
    equity = data["valuation"][2] if data["valuation"] else None
    rows = []
    for symbol, market, currency, quantity, average in data["positions"]:
        price = Decimal(data["last"].get(symbol, average))
        value = quantity * price * (1 if market == "KR" else (rate or Decimal(0)))
        weight = f"{v3_head.pct(value / equity):.2f}%" if equity else "-"
        rows.append((symbol, data["names"].get(symbol, ""), market, quantity, average, str(price), f"{int(value):,}원", weight))
    return _section("보유 종목", ("종목", "이름", "시장", "수량", "평균 단가", "마지막 가격", "원화 평가", "비중"), rows)


def _decisions(data: dict, calendar: dict, now: datetime) -> str:
    day = now.date().isoformat()
    slots = v6_market.slots(calendar, day)
    cells = []
    for clock, kind in slots:
        row = data["decisions"].get(f"{day} {clock}")
        waiting = row is None and datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=KST) + v6_market.FILL_AFTER > now
        cells.append(web.slot(clock, "대기" if waiting else row[1] if row else "missing", ""))
    rows = [(time[11:], status, ", ".join(f"{item['symbol']} {item['weight']}" for item in json.loads(positions)) or "현금", strategy, reason) for time, status, positions, strategy, reason in data["decisions"].values()]
    table = _table(("시각", "상태", "목표 보유", "전략 이름", "이유"), rows, "decisions")
    return f'<section><h2>오늘의 판단</h2><div class="slots">{"".join(cells)}</div>{table}</section>'


def _fills(data: dict) -> str:
    rows = [
        (at[11:19], ORIGINS.get(origin, origin), symbol, SIDES.get(side, side), quantity, amount, currency, f"{commission} / {tax} / {risk} / {sec}", f"${fx_usd} ← {fx_krw}원" if fx_krw != "0" else "")
        for at, origin, symbol, side, quantity, amount, currency, commission, tax, risk, sec, fx_usd, fx_krw in data["fills"]
    ]
    return _section("오늘의 체결", ("시각", "구분", "종목", "방향", "수량", "금액", "통화", "수수료/세금/위험 비용/SEC Fee", "환전"), rows)


def _section(title: str, header: tuple[str, ...], rows: list[tuple]) -> str:
    return f"<section><h2>{html.escape(title)}</h2>{_table(header, rows) if rows else _p('없음')}</section>"


def _table(header: tuple[str, ...], rows: list[tuple], cls: str = "") -> str:
    head = "".join(f"<th>{html.escape(str(cell))}</th>" for cell in header)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table class="{cls}"><tr>{head}</tr>{body}</table></div>'


def _p(text: str, raw: bool = False) -> str:
    return f"<p>{text if raw else html.escape(text)}</p>"


def _tail(path: Path) -> str:
    try:
        return "".join(path.read_text(encoding="utf-8").splitlines(keepends=True)[-LOG_LINES:])
    except FileNotFoundError:
        return "아직 실행 기록이 없다"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(KST).isoformat(timespec="seconds")

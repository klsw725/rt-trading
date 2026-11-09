"""v5 자동 실행, 실행기 돌보기와 보기 전용 현황 웹페이지(v5 SPEC 5.1·8절, PRD 05)."""

import html
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from rt_trading import sessions, v3_daily, v3_serve, v4_daily, v4_head, v4_paper, v4_serve, v5_book, v5_live, v5_target, web
from rt_trading.toss import KST

PORT = 8081  # v4와 같은 포트라 v4와 v5 컨테이너는 함께 켜지 않는다
EARLY = timedelta(minutes=15)  # 실행기를 켜기 시작하는 때: 개장 15분 전(start 차례)
RESTART = timedelta(seconds=30)  # 실행기가 끝난 것을 본 뒤 다시 켜기까지
MAP_DAYS = 5
TURN_ROWS = 20
MODES = {"dry": "연습 운용", "live": "실주문"}
STOP_KINDS = {"human": "사람의 정지", "safety": "안전 정지", "manual": "수동 확인 필요", "limit": "누적 손실 한도"}
STOP_MODES = {"hold": "멈춤", "sell": "멈추고 다 팔기"}
ORDER_KINDS = {"live": "실", "dry": "연습", "check": "점검"}
SIDES = {"BUY": "매수", "SELL": "매도"}
ORDER_HEADER = ("시각", "구분", "종목", "방향", "방식", "수량", "상태", "체결 수량", "평균 체결가", "수수료", "세금")


def period(paths: v5_live.Paths, now: datetime) -> v3_daily.Period:
    """now의 날로 다시 계산한 v5 기간(PRD 02). 확인할 때마다 부른다."""
    return v5_target.load_period(paths.period, paths.holidays, now.date().isoformat())


def live_wanted(now: datetime, period: v3_daily.Period, live_db: Path) -> bool:
    """now에 실행기가 돌고 있어야 하는가. v5 기간(시작일부터)의 거래일 개장 15분 전부터 그날 end 차례를 마쳤다는 기록(days.ended)이 생길 때까지다. 날은 자정에 바뀐다."""
    day = now.date().isoformat()
    if day < period.start or not v3_daily.is_trading_day(day, period.holidays) or now < v4_serve._at(day, sessions.session(day, sessions.exceptions()).open) - EARLY:
        return False
    return not v5_book.read(live_db, "SELECT 1 FROM days WHERE date = ? AND ended = 1", (day,))


def watch(state: dict, now: datetime, period: v3_daily.Period, live_db: Path, launch: Callable[[], subprocess.Popen]) -> None:
    """실행기 돌보기 한 번. state는 {"process": 켠 실행기, "exited": 그 실행기가 끝난 것을 본 시각}이다.

    끝난 실행기를 거두고, 돌고 있는 실행기가 없고 끝난 것을 본 지 30초가 지났으며 돌아야 할 때(live_wanted)면 launch로 켠다.
    """
    if state["process"] is not None and state["process"].poll() is not None:
        state["process"], state["exited"] = None, now
    if state["process"] is None and (state["exited"] is None or now - state["exited"] >= RESTART) and live_wanted(now, period, live_db):
        state["process"] = launch()


def supervise(period: v3_daily.Period, live_db: Path, clock: Callable[[], datetime], launch: Callable[[], subprocess.Popen], stop: threading.Event) -> None:
    """실행기 돌보기 스레드. 판단 명령과 따로 1초마다 watch를 하고, stop이 서면 끝난다. period는 시작일과 휴장일만 쓴다."""
    state = {"process": None, "exited": None}
    while True:
        try:
            watch(state, clock(), period, live_db, launch)
        except Exception as error:  # 이 스레드가 죽으면 실행기를 다시 켜는 쪽이 없어진다
            print(f"V5_SUPERVISE_ERROR: {type(error).__name__}: {error}", flush=True)
        if stop.wait(1):
            return


def page(paths: v5_live.Paths, period: v3_daily.Period, report_dir: Path, log_path: Path, status: dict, now: datetime) -> str:
    """현황 웹페이지 HTML(PRD 05). 맨 위에 실계좌 묶음, 그 아래에 v4 페이지의 묶음이 있다. 파일은 읽기만 한다."""
    body = "\n".join(_live(paths.live, now) + _paper(paths.daily, period, report_dir, log_path, status, now))
    return web.document("rt-trading v5 현황", "rt-trading v5 현황", body)


def run(paths: v5_live.Paths, report_dir: Path, log_path: Path, live_log: Path, host: str, port: int) -> int:
    """웹페이지와 실행기 돌보기 스레드를 켜고, 10초마다 한 번씩 확인해 차례의 명령을 실행한다. 기간 설정이 틀리면 1이고, 아니면 멈출 때까지 돈다."""
    clock = lambda: datetime.now(KST)
    try:
        first = period(paths, clock())
    except v3_daily.DailyError as error:
        print(error)
        return 1
    status = {"started_at": v4_serve._stamp(clock()), "running": None, "last": None}
    httpd = v3_serve.server(host, port, lambda: page(paths, period(paths, clock()), report_dir, log_path, status, clock()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=supervise, args=(first, paths.live, clock, lambda: _launch(live_log, clock()), threading.Event()), daemon=True).start()
    done = None
    while True:
        done = v4_serve.tick(done, status, log_path, period(paths, clock()), clock, v4_serve._execute, "v5")
        time.sleep(10)


def _launch(log_path: Path, now: datetime) -> subprocess.Popen:
    """rt-trading v5-live를 켜고 출력을 log_path에 이어 쓴다. 하루 내내 도는 실행기의 줄이 바로 보이게 출력을 버퍼에 모으지 않는다."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as file:
        file.write(f"=== {v4_serve._stamp(now)} v5-live ===\n")
        file.flush()
        return subprocess.Popen(["rt-trading", "v5-live"], stdout=file, stderr=subprocess.STDOUT, env=os.environ | {"PYTHONUNBUFFERED": "1"})


def _live(db: Path, now: datetime) -> list[str]:
    """실계좌 묶음(live.sqlite3를 읽기 전용으로). 봇 자산과 실계좌 상태는 가장 최근 차례 기록의 값이다."""
    today = now.date().isoformat()
    day = next(iter(v5_book.read(db, "SELECT mode FROM days WHERE date = ?", (today,))), None)
    stops = v5_book.read(db, "SELECT * FROM stops WHERE cleared_at IS NULL ORDER BY id")
    latest = next(iter(v5_book.read(db, "SELECT * FROM turns ORDER BY rowid DESC LIMIT 1")), None)
    seen = next(iter(v5_book.read(db, "SELECT * FROM turns WHERE real IS NOT NULL ORDER BY rowid DESC LIMIT 1")), None)
    mode = latest["mode"] if latest else day["mode"] if day else None
    ledger = next(iter(v5_book.read(db, "SELECT * FROM ledgers WHERE kind = ? ORDER BY id DESC LIMIT 1", (mode,))), None)
    lines = [_p(f"오늘 방식: {MODES.get(day['mode'], day['mode']) if day else '기록 없음'}", "lead")]
    for stop in stops:
        lines.append(_p(f"{_when(stop['time'])} {STOP_KINDS.get(stop['kind'], stop['kind'])}({STOP_MODES.get(stop['mode'], stop['mode'])}): {stop['reason']}", "alert"))
        if stop["kind"] == "manual":
            lines.append(_p("토스 앱에서 정리한 뒤 v5-resume --new-ledger로 푼다"))
    if not stops:
        lines.append(_p("정지 없음"))
    lines.append(_p(f"마지막 차례: {_when(latest['time'])} {latest['turn']} · {latest['action']}" if latest else "마지막 차례: 기록 없음", "muted"))
    if latest is None or latest["cash"] is None:
        money = _p("장부 없음" if latest else "차례 기록 없음")
    else:
        held, prices, equity = json.loads(latest["holdings"]), json.loads(latest["prices"] or "{}"), latest["equity"]
        profit = equity - ledger["start_cash"] if equity is not None and ledger else None
        money = (
            (f'<div class="big">{equity:,}원</div>' if equity is not None else _p("봇 자산: 가격 없음"))
            + ("<p>수익 " + web.signed(f"{profit:+,}원 ({profit / ledger['start_cash']:+.2%})" if profit else "0원 (0.00%)", profit) + "</p>" if profit is not None else "")
            + f'<span class="hold">{html.escape(_holding(held))}</span>'
            + _p(f"봇 현금 {latest['cash']:,}원")
            + _p("가격: " + " · ".join(f"{web.NAMES[symbol]} {prices[symbol]:,}원" for symbol in v5_target.SYMBOLS if symbol in prices))
            + _p(f"{_when(latest['time'])} {latest['turn']} 차례 기준", "muted")
        )
    if seen:
        compare = (
            (_p("장부와 같음", "lead") if seen["matched"] else _p("장부와 다름", "alert"))
            + _p(f"실계좌: {_counts(json.loads(seen['real']))}")
            + _p(f"장부: {_counts(json.loads(seen['holdings'] or '{}'))}")
            + _p(f"{_when(seen['time'])} {seen['turn']} 차례에 봄", "muted")
        )
    else:
        compare = _p("대조한 차례 없음")
    if ledger:
        latches = {(row["kind"], row["period"]): row["value"] for row in v5_book.read(db, "SELECT kind, period, value FROM latches WHERE ledger_id = ?", (ledger["id"],))}
        daily, weekly = latches.get(("daily", today)), latches.get(("weekly", "{}-W{:02d}".format(*now.isocalendar()[:2])))
        cumulative = any(stop["kind"] == "limit" for stop in stops)
        limits = (
            _p(f"하루 한도(−2%): {'발동' if daily else '발동 안 함'}", "alert" if daily else "")
            + _p(f"한 주 한도(−5%): {'발동' if weekly else '발동 안 함'}", "alert" if weekly else "")
            + _p(f"누적 한도(−20%): {'정지 중' if cumulative else '정지 아님'}", "alert" if cumulative else "")
            + _p(f"거부 연속 {latches.get(('rejections', ''), 0)}번(3번이면 안전 정지)")
        )
    else:
        limits = _p("장부 없음")
    turns = v5_book.read(db, "SELECT * FROM turns WHERE substr(time, 1, 10) = ? ORDER BY rowid DESC LIMIT ?", (today, TURN_ROWS))
    orders = v5_book.read(db, "SELECT * FROM orders WHERE date = ? ORDER BY created_at, client_order_id", (today,))
    return [
        '<div class="cards">'
        + web.card("실행", "".join(lines))
        + web.card("봇 자산(연습)" if mode == "dry" else "봇 자산", money)
        + web.card("실계좌 대조", compare)
        + web.card("한도", limits)
        + "</div>",
        "<section><h2>오늘 차례</h2>" + v4_serve._table(("시각", "차례", "목표", "한 일"), [(row["time"][11:19], row["turn"], _goal(row), row["action"]) for row in turns]) + "</section>",
        "<section><h2>오늘 주문</h2>" + _grid(ORDER_HEADER, [_order(row) for row in orders]) + "</section>",
    ]


def _paper(paths: v4_daily.Paths, period: v3_daily.Period, report_dir: Path, log_path: Path, status: dict, now: datetime) -> list[str]:
    """v4 페이지의 묶음(web SPEC). 판단 지도는 오늘까지 최근 5거래일, 기간 칸은 시작일과 정산한 날 수이고, 판단 칸에 통과 기준 줄이 없다."""
    running, last, today = status["running"], status["last"], now.date().isoformat()
    settled = v4_serve._read(paths.paper, "settled_days", ("date",), "date")
    rows = [dict(zip(v4_serve.COLUMNS, row)) for row in v4_serve._read(paths.paper, "decisions", v4_serve.COLUMNS, "date, slot")]
    reports = sorted(report_dir.glob("v5-*"))
    content = json.loads((reports[-1] / "report.json").read_text(encoding="utf-8")) if reports else None
    log = log_path.read_text(encoding="utf-8").splitlines()[-v4_serve.LOG_LINES :] if log_path.exists() else ["기록 없음"]
    exits = ", ".join(f"{command} 시간 초과" if code is None else f"{command} 종료 코드 {code}" for command, code in last["exits"]) if last else ""
    found, days, counts = {(row["date"], row["slot"]): row for row in rows}, [], {"ok": 0, "capped": 0, "failed": 0}
    for day in [day for day in period.days if day <= today]:
        chips = []
        for slot in v4_head.slots(day):
            row = found.get((day, slot.time))
            state = row["status"] if row else ("없음" if now >= v4_serve._at(day, slot.deadline) else "대기")
            if state != "대기":  # 판단 행이 있거나 마감이 지난 차례
                counts[state if state in ("ok", "capped") else "failed"] += 1
            chips.append(web.slot(slot.time, state, row["subordinate"] if row else ""))
        label = f"{day[5:]}({web.WEEKDAYS[Date.fromisoformat(day).weekday()]})"
        days.append(f'<div class="day"><div class="date{" today" if day == today else ""}">{label}</div><div class="slots">{"".join(chips)}</div></div>')
    auto = (
        f'<p class="lead"><span class="dot{" run" if running else ""}"></span>'
        + html.escape(f"상태: 실행 중({running['slot']}, {running['started_at']} 시작)" if running else "상태: 대기 중")
        + "</p>"
        + _p(
            f"마지막 실행: {last['slot']}, {last['started_at']} 시작, {last['finished_at']} 끝, {exits}" if last else f"마지막 실행: 없음({status['started_at']}에 켠 뒤)",
            "alert" if last and any(code != 0 for _, code in last["exits"]) else "",
        )
        + _p(f"다음 실행: {v4_serve.next_run(now, period):%Y-%m-%d %H:%M}")
        + _p(f"페이지 시각: {now:%Y-%m-%d %H:%M:%S}", "muted")
    )
    judged = (
        f'<div class="big {"tone-bad" if counts["failed"] else "tone-good"}">판단 실패 {counts["failed"]}번</div>'
        + _p(f"ok {counts['ok']}번 · capped {counts['capped']}번 · 지난 차례 {sum(counts.values())}개")
    )
    parts = [
        '<div class="cards">'
        + web.card("자동 실행", auto)
        + web.card("기간", _p(f"정산 {len(settled)}일", "lead") + _p(f"시작일: {period.start}"))
        + web.card("헤드 계좌", v4_serve._head(reports[-1], content) if content else _p("보고서 없음"))
        + web.card("판단", judged)
        + "</div>",
        f'<section><h2>판단 지도</h2>{web.LEGEND}{"".join(days[-MAP_DAYS:])}</section>',
    ]
    if today in period.days:
        plan = v4_head.plan([day for day in period.days if day <= today], rows)[today]
        table = []
        for slot in v4_head.slots(today):
            if row := found.get((today, slot.time)):
                table.append((slot.time, slot.kind, row["status"], row["subordinate"], row["reason"]))
            else:
                table.append((slot.time, slot.kind, "대기" if now < v4_serve._at(today, slot.deadline) else "없음", "", ""))
        parts.append(
            "<section><h2>오늘 판단</h2>"
            + _p(f"지금 부하: {v4_paper.closing(plan)}, 남은 교체 {v4_head.SWITCH_LIMIT - len(plan[1])}번")
            + v4_serve._table(("차례", "종류", "상태", "부하", "이유"), table)
            + "</section>"
        )
    past = [tuple(row.values()) for row in rows if row["date"] in period.days and row["date"] < today]
    parts.append("<section><h2>판단</h2>" + v4_serve._table(("날짜", "차례", "종류", "상태", "부하", "이유"), past) + "</section>")
    if content:
        result = content["verdict"]
        verdict = "판정: 기간 중" if result is None else (
            f"판정: {'통과' if result['passed'] else '불통과'} " + ", ".join(f"{key} {value}" for key, value in result["criteria"].items())
        )
        parts.append("<section><h2>계좌</h2>" + _p(f"보고서: {reports[-1].name}") + v4_serve._accounts(content["accounts"]) + _p(verdict) + "</section>")
    else:
        parts.append("<section><h2>계좌</h2>" + _p("보고서 없음") + "</section>")
    parts.append(f"<section><h2>최근 실행 기록</h2><pre>{html.escape(chr(10).join(log))}</pre></section>")
    return parts


_p = v4_serve._p


def _when(moment: str) -> str:
    return moment[:19].replace("T", " ")


def _holding(held: dict[str, int]) -> str:
    return "보유: " + " · ".join(f"{web.NAMES.get(symbol, symbol)} {count}주" for symbol, count in held.items()) if held else "보유: 현금"


def _counts(quantities: dict[str, int]) -> str:
    return " · ".join(f"{web.NAMES[symbol]} {quantities.get(symbol, 0)}주" for symbol in v5_target.SYMBOLS)


def _goal(row: dict) -> str:
    """차례의 목표 종목 이름이나 현금, 목표를 정하지 않았으면 그 이유."""
    if row["target"]:
        return web.NAMES.get(row["target"], row["target"])
    return f"정하지 않음({row['reason']})" if row["reason"] else ""


def _order(row: dict) -> tuple:
    status = f"{row['status']}({row['toss_status']})" if row["toss_status"] else row["status"]
    return (
        row["created_at"][11:19], ORDER_KINDS.get(row["kind"], row["kind"]), web.NAMES.get(row["symbol"], row["symbol"]), SIDES.get(row["side"], row["side"]),
        row["tif"], row["quantity"], status, row["filled_quantity"], _won(row["average_price"]), _won(row["commission"]), _won(row["tax"]),
    )


def _won(value: str | None) -> str:
    return "" if value is None else f"{Decimal(value):,}원"


def _grid(header: tuple[str, ...], rows: list[tuple]) -> str:
    """칸마다 이스케이프한 표. 판단 표와 달리 마지막 열을 넓히지 않는다."""

    def cells(tag: str, row: tuple) -> str:
        return "".join(f"<{tag}>{html.escape(str(value))}</{tag}>" for value in row)

    return f'<div class="scroll"><table><tr>{cells("th", header)}</tr>{"".join(f"<tr>{cells('td', row)}</tr>" for row in rows)}</table></div>'

"""v4 자동 실행과 보기 전용 현황 웹페이지(v4 SPEC 5.1·8절, PRD 05)."""

import html
import json
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

from rt_trading import v3_daily, v3_serve, v4_daily, v4_head, v4_paper
from rt_trading.toss import KST

MORNING = ("07:30", "08:20")
UNTIL = "09:50"  # 10시 개장일의 개장 전 판단 마감. 이때부터는 아침 차례를 실행하지 않는다
MORNING_STOP = "09:55"  # 아침 명령을 멈추는 시각. 가장 이른 판단 차례(10:00) 전이다
DECIDE_GRACE = timedelta(minutes=1)  # v4-decide는 마감 1분 뒤에 멈춘다. 마감 직전에 마친 판단이 기록을 끝낼 여유다
PORT = 8081
LOG_LINES = 40
COLUMNS = ("date", "slot", "kind", "status", "subordinate", "reason")


def due(now: datetime, period: v3_daily.Period) -> str | None:
    """now의 실행 차례 '날짜 차례'. 아침은 평일에 이미 지난 07:30·08:20 중 마지막이고 09:50 전일 때, 판단은 기간의 거래일에 s ≤ now < s + 5분일 때다."""
    day, clock = now.date().isoformat(), now.strftime("%H:%M")
    passed = [slot for slot in MORNING if slot <= clock < UNTIL]
    if now.weekday() < 5 and passed:
        return f"{day} 아침 {passed[-1]}"
    if day in period.days:
        for slot in v4_head.slots(day):
            if slot.kind != "pre_open" and slot.time <= clock < slot.deadline:
                return f"{day} 판단 {slot.time}"
    return None


def next_run(now: datetime, period: v3_daily.Period) -> datetime:
    """now보다 뒤인 첫 실행 차례의 시작 시각."""
    day = now.date()
    while True:
        times = list(MORNING) if day.weekday() < 5 else []
        if day.isoformat() in period.days:
            times += [slot.time for slot in v4_head.slots(day.isoformat()) if slot.kind != "pre_open"]
        later = [at for clock in times if (at := datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=KST)) > now]
        if later:
            return min(later)
        day += timedelta(days=1)


def page(paths: v4_daily.Paths, period: v3_daily.Period, report_dir: Path, log_path: Path, status: dict, now: datetime) -> str:
    """현황 웹페이지 HTML. status는 자동 실행 상태(켠 시각, 실행 중인 차례, 마지막 실행)다. 파일은 읽기만 한다."""
    running, last, today = status["running"], status["last"], now.date().isoformat()
    settled = [date for (date,) in _read(paths.paper, "settled_days", ("date",), "date")]
    rows = [dict(zip(COLUMNS, row)) for row in _read(paths.paper, "decisions", COLUMNS, "date, slot")]
    reports = sorted(report_dir.glob("v4-*"))
    log = log_path.read_text(encoding="utf-8").splitlines()[-LOG_LINES:] if log_path.exists() else ["기록 없음"]
    exits = ", ".join(f"{command} 시간 초과" if code is None else f"{command} 종료 코드 {code}" for command, code in last["exits"]) if last else ""
    parts = [
        "<h2>자동 실행</h2>",
        _p(f"상태: 실행 중({running['slot']}, {running['started_at']} 시작)" if running else "상태: 대기 중"),
        _p(f"마지막 실행: {last['slot']}, {last['started_at']} 시작, {last['finished_at']} 끝, {exits}" if last else f"마지막 실행: 없음({status['started_at']}에 켠 뒤)"),
        _p(f"다음 실행: {next_run(now, period):%Y-%m-%d %H:%M}"),
        _p(f"페이지 시각: {now:%Y-%m-%d %H:%M:%S}"),
        "<h2>기간</h2>",
        _p(f"기간: {period.start} ~ {period.final_day}"),
        _p(f"정산 {len(settled)}/{len(period.days)} (마친 마지막 날 {settled[-1] if settled else '없음'})"),
    ]
    if today in period.days:
        plan = v4_head.plan([day for day in period.days if day <= today], rows)[today]
        found, table = {row["slot"]: row for row in rows if row["date"] == today}, []
        for slot in v4_head.slots(today):
            if row := found.get(slot.time):
                table.append((slot.time, slot.kind, row["status"], row["subordinate"], row["reason"]))
            else:
                table.append((slot.time, slot.kind, "대기" if now < _at(today, slot.deadline) else "없음", "", ""))
        parts += [
            "<h2>오늘 판단</h2>",
            _p(f"지금 부하: {v4_paper.closing(plan)}, 남은 교체 {v4_head.SWITCH_LIMIT - len(plan[1])}번"),
            _table(("차례", "종류", "상태", "부하", "이유"), table),
        ]
    past = [tuple(row.values()) for row in rows if row["date"] in period.days and row["date"] < today]
    parts += ["<h2>판단</h2>", _table(("날짜", "차례", "종류", "상태", "부하", "이유"), past), "<h2>계좌</h2>"]
    if reports:
        content = json.loads((reports[-1] / "report.json").read_text(encoding="utf-8"))
        accounts = [
            (item["id"], f"{item['final_equity']:,}원", f"{item['total_return']:+.2%}", f"{item['max_drawdown']:.2%}", item["trade_count"])
            for item in content["accounts"]
        ]
        result = content["verdict"]
        verdict = "판정: 기간 중" if result is None else (
            f"판정: {'통과' if result['passed'] else '불통과'} " + ", ".join(f"{key} {value}" for key, value in result["criteria"].items())
        )
        parts += [_p(f"보고서: {reports[-1].name}"), _table(("계좌", "최종 자산", "총수익", "최대 낙폭", "거래 수"), accounts), _p(verdict)]
    else:
        parts.append(_p("보고서 없음"))
    parts += ["<h2>최근 실행 기록</h2>", f"<pre>{html.escape(chr(10).join(log))}</pre>"]
    return (
        f'<!doctype html>\n<html lang="ko"><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">'
        f"<title>rt-trading v4 현황</title><style>{v3_serve.STYLE}</style></head>\n<body><h1>rt-trading v4 모의 운용 현황</h1>\n"
        + "\n".join(parts)
        + "\n</body></html>\n"
    )


def tick(
    done: str | None,
    status: dict,
    log_path: Path,
    period: v3_daily.Period,
    clock: Callable[[], datetime],
    execute: Callable[[str, TextIO, float], int | None],
) -> str | None:
    """한 번의 확인. 아직 실행하지 않은 차례이면 그 차례의 명령을 차례로 실행하고 그 차례를, 아니면 done을 돌려준다.

    아침 차례는 v4-daily와 v4-report(앞이 실패해도), 판단 차례는 v4-decide --at s다.
    execute에는 시간 제한(아침 09:55, 판단 마감 1분 뒤)까지 남은 초를 주고, 끝나지 않아 멈췄으면 None을 받는다.
    """
    slot = due(clock(), period)
    if not slot or slot == done:
        return done
    day, name, started = slot[:10], slot[11:], _stamp(clock())
    if name.startswith("아침"):
        commands, stop = ["v4-daily", "v4-report"], _at(day, MORNING_STOP)
    else:
        deadline = next(item.deadline for item in v4_head.slots(day) if item.time == name[3:])
        commands, stop = [f"v4-decide --at {name[3:]}"], _at(day, deadline) + DECIDE_GRACE
    status["running"] = {"slot": name, "started_at": started}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exits = []
    with log_path.open("a", encoding="utf-8") as file:
        file.write(f"=== {started} {name} ===\n")
        file.flush()
        for command in commands:
            left = (stop - clock()).total_seconds()
            code = execute(command, file, left) if left > 0 else None
            if code is None:
                file.write(f"V4_COMMAND_TIMEOUT: {stop:%H:%M}까지 마치지 못해 멈췄다({command})\n")
                file.flush()
            exits.append([command, code])
    status["last"] = {"slot": name, "started_at": started, "finished_at": _stamp(clock()), "exits": exits}
    status["running"] = None
    return slot


def run(paths: v4_daily.Paths, period_path: Path, holidays_path: Path, report_dir: Path, log_path: Path, host: str, port: int) -> int:
    """웹페이지를 열고, 10초마다 한 번씩 확인해 차례의 명령을 실행한다. 기간 설정이 틀리면 1이고, 아니면 멈출 때까지 돈다."""
    clock = lambda: datetime.now(KST)
    try:
        period = v3_daily.load_period(period_path, holidays_path, "V4_PERIOD_INVALID")
    except v3_daily.DailyError as error:
        print(error)
        return 1
    status = {"started_at": _stamp(clock()), "running": None, "last": None}
    httpd = v3_serve.server(host, port, lambda: page(paths, period, report_dir, log_path, status, clock()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    done = None
    while True:
        done = tick(done, status, log_path, period, clock, _execute)
        time.sleep(10)


def _execute(command: str, file: TextIO, seconds: float) -> int | None:
    """명령을 seconds초 안에 실행해 종료 코드를 돌려준다. 끝나지 않으면 멈추고 None이다."""
    try:
        return subprocess.run(["rt-trading", *command.split()], stdout=file, stderr=subprocess.STDOUT, timeout=seconds).returncode
    except subprocess.TimeoutExpired:
        return None


def _at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=KST)


def _read(path: Path, table: str, columns: tuple[str, ...], order: str) -> list[tuple]:
    """SQLite 파일을 읽기 전용으로 열어 표를 읽는다. 파일이나 표가 없으면 빈 목록이다."""
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            return []
        return db.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}").fetchall()


def _stamp(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}"


def _p(text: str) -> str:
    return f"<p>{html.escape(text)}</p>"


def _table(header: tuple[str, ...], rows: list[tuple]) -> str:
    def cells(tag: str, row: tuple) -> str:
        return "".join(f"<{tag}>{html.escape(str(value))}</{tag}>" for value in row)

    return f"<table><tr>{cells('th', header)}</tr>" + "".join(f"<tr>{cells('td', row)}</tr>" for row in rows) + "</table>"

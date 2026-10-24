"""v3 자동 실행과 보기 전용 현황 웹페이지(v3 SPEC 8절, PRD 05)."""

import html
import json
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TextIO

from rt_trading import v3_daily
from rt_trading.toss import KST

SLOTS = ("07:30", "08:20")
UNTIL = "09:50"  # 10시 개장일의 판단 마감. 이때부터는 놓친 차례를 실행하지 않는다
PORT = 8080
LOG_LINES = 40
STYLE = "body{font-family:sans-serif}table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:2px 8px}"


def due(now: datetime) -> str | None:
    """now의 실행 차례(날짜와 시각). 평일에 이미 지난 SLOTS 중 마지막이고, UNTIL 전일 때만 있다."""
    clock = now.strftime("%H:%M")
    passed = [slot for slot in SLOTS if slot <= clock < UNTIL]
    return f"{now.date()} {passed[-1]}" if now.weekday() < 5 and passed else None


def next_run(now: datetime) -> datetime:
    """now보다 뒤인 첫 평일 실행 시각."""
    day = now.date()
    while True:
        if day.weekday() < 5:
            later = [at for slot in SLOTS if (at := datetime.fromisoformat(f"{day}T{slot}").replace(tzinfo=KST)) > now]
            if later:
                return later[0]
        day += timedelta(days=1)


def page(
    paths: v3_daily.Paths, period_path: Path, holidays_path: Path, report_dir: Path, log_path: Path, status: dict, now: datetime
) -> str:
    """현황 웹페이지 HTML. status는 자동 실행 상태(켠 시각, 실행 중인 차례, 마지막 실행)다."""
    running, last = status["running"], status["last"]
    period = v3_daily.load_period(period_path, holidays_path)
    settled = [date for (date,) in _read(paths.paper, "settled_days", "date")]
    decisions = [row for row in _read(paths.paper, "decisions", "date, status, subordinate, reason") if row[0] in period.days]
    reports = sorted(report_dir.glob("v3-*"))
    log = log_path.read_text(encoding="utf-8").splitlines()[-LOG_LINES:] if log_path.exists() else ["기록 없음"]
    parts = [
        "<h2>자동 실행</h2>",
        _p(f"상태: 실행 중({running['slot']}, {running['started_at']} 시작)" if running else "상태: 대기 중"),
        _p(
            f"마지막 실행: {last['started_at']} 시작, {last['finished_at']} 끝, "
            f"v3-daily 종료 코드 {last['daily_exit']}, v3-report 종료 코드 {last['report_exit']}"
            if last
            else f"마지막 실행: 없음({status['started_at']}에 켠 뒤)"
        ),
        _p(f"다음 실행: {next_run(now):%Y-%m-%d %H:%M}"),
        _p(f"페이지 시각: {now:%Y-%m-%d %H:%M:%S}"),
        "<h2>기간</h2>",
        _p(f"기간: {period.start} ~ {period.final_day}"),
        _p(f"정산 {len(settled)}/{len(period.days)} (마친 마지막 날 {settled[-1] if settled else '없음'})"),
        "<h2>판단</h2>",
        _table(("날짜", "상태", "부하", "이유"), decisions),
        "<h2>계좌</h2>",
    ]
    if reports:
        content = json.loads((reports[-1] / "report.json").read_text(encoding="utf-8"))
        rows = [
            (item["id"], f"{item['final_equity']:,}원", f"{item['total_return']:+.2%}", f"{item['max_drawdown']:.2%}", item["trade_count"])
            for item in content["accounts"]
        ]
        result = content["verdict"]
        verdict = "판정: 기간 중" if result is None else (
            f"판정: {'통과' if result['passed'] else '불통과'} " + ", ".join(f"{key} {value}" for key, value in result["criteria"].items())
        )
        parts += [_p(f"보고서: {reports[-1].name}"), _table(("계좌", "최종 자산", "총수익", "최대 낙폭", "거래 수"), rows), _p(verdict)]
    else:
        parts.append(_p("보고서 없음"))
    parts += ["<h2>최근 실행 기록</h2>", f"<pre>{html.escape(chr(10).join(log))}</pre>"]
    return (
        f'<!doctype html>\n<html lang="ko"><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">'
        f"<title>rt-trading v3 현황</title><style>{STYLE}</style></head>\n<body><h1>rt-trading v3 모의 운용 현황</h1>\n"
        + "\n".join(parts)
        + "\n</body></html>\n"
    )


def server(host: str, port: int, render: Callable[[], str]) -> ThreadingHTTPServer:
    """GET /에만 render()의 페이지로 답하는 웹 서버. 다른 경로는 404, GET이 아닌 요청은 501이다."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/":
                self.send_error(404)
                return
            try:
                code, body = 200, render()
            except Exception as error:
                code, body = 500, html.escape(f"{type(error).__name__}: {error}")
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass  # 60초마다 새로 고치므로 접속 기록은 남기지 않는다

    return ThreadingHTTPServer((host, port), Handler)


def tick(done: str | None, status: dict, log_path: Path, clock: Callable[[], datetime], execute: Callable[[str, TextIO], int]) -> str | None:
    """한 번의 확인. 아직 실행하지 않은 차례이면 v3-daily와 v3-report를 차례로 실행하고 그 차례를, 아니면 done을 돌려준다."""
    slot = due(clock())
    if not slot or slot == done:
        return done
    started = _stamp(clock())
    status["running"] = {"slot": slot, "started_at": started}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as file:
        file.write(f"=== {started} ===\n")
        file.flush()
        codes = [execute(command, file) for command in ("v3-daily", "v3-report")]
    status["last"] = {"slot": slot, "started_at": started, "finished_at": _stamp(clock()), "daily_exit": codes[0], "report_exit": codes[1]}
    status["running"] = None
    return slot


def run(paths: v3_daily.Paths, period_path: Path, holidays_path: Path, report_dir: Path, log_path: Path, host: str, port: int) -> None:
    """웹페이지를 열고, 30초마다 한 번씩 확인해 v3-daily와 v3-report를 실행한다. 멈출 때까지 돈다."""
    clock = lambda: datetime.now(KST)
    status = {"started_at": _stamp(clock()), "running": None, "last": None}
    httpd = server(host, port, lambda: page(paths, period_path, holidays_path, report_dir, log_path, status, clock()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    done = None
    while True:
        done = tick(done, status, log_path, clock, _execute)
        time.sleep(30)


def _execute(command: str, file: TextIO) -> int:
    return subprocess.run(["rt-trading", command], stdout=file, stderr=subprocess.STDOUT).returncode


def _read(path: Path, table: str, columns: str) -> list[tuple]:
    """SQLite 파일을 읽기 전용으로 열어 표를 날짜순으로 읽는다. 파일이나 표가 없으면 빈 목록이다."""
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            return []
        return db.execute(f"SELECT {columns} FROM {table} ORDER BY date").fetchall()


def _stamp(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}"


def _p(text: str) -> str:
    return f"<p>{html.escape(text)}</p>"


def _table(header: tuple[str, ...], rows: list[tuple]) -> str:
    def cells(tag: str, row: tuple) -> str:
        return "".join(f"<{tag}>{html.escape(str(value))}</{tag}>" for value in row)

    return f"<table><tr>{cells('th', header)}</tr>" + "".join(f"<tr>{cells('td', row)}</tr>" for row in rows) + "</table>"

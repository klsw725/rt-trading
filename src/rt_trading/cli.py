"""rt-trading 명령."""

import argparse
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from rt_trading import (
    codex,
    engine,
    market_data,
    report,
    store,
    strategies,
    toss,
    v0,
    v0_acceptance,
    v2_acceptance,
    v2_backtest,
    v3_acceptance,
    v3_daily,
    v3_report,
    v3_serve,
    v3_us,
    v4_acceptance,
    v4_daily,
    v4_news,
    v4_report,
    v4_serve,
    v5_live,
    v5_report,
    v5_serve,
    v5_target,
    web_acceptance,
)

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "market.sqlite3"
REPORT_DIR = ROOT / "data" / "reports"
V3_DIR = ROOT / "data" / "v3"
V3_PATHS = v3_daily.Paths(DB_PATH, V3_DIR / "market.sqlite3", V3_DIR / "paper.sqlite3", V3_DIR / "head", ROOT / "data" / "us.sqlite3")
V4_DIR = ROOT / "data" / "v4"
V4_PATHS = v4_daily.Paths(
    DB_PATH, V4_DIR / "market.sqlite3", V4_DIR / "paper.sqlite3", V4_DIR / "head", V4_DIR / "news", ROOT / "data" / "us.sqlite3"
)
V5_DIR = ROOT / "data" / "v5"
V5_PATHS = v4_daily.Paths(
    DB_PATH, V5_DIR / "market.sqlite3", V5_DIR / "paper.sqlite3", V5_DIR / "head", V5_DIR / "news", ROOT / "data" / "us.sqlite3"
)
V5_LIVE = v5_live.Paths(
    V5_PATHS, V5_DIR / "live.sqlite3", V5_DIR / "config.json", V5_DIR / "live.lock", V5_DIR / "order.lock", v5_target.PERIOD, v3_daily.HOLIDAYS
)
SYMBOLS = ("069500", "114800")
SINCE = "2022-11-23"  # 토스 국내 1분봉 제공 시작일


def main() -> None:
    from rt_trading import acceptance, v5_acceptance  # 두 acceptance가 이 모듈을 쓰므로 여기서 불러온다

    parser = argparse.ArgumentParser(prog="rt-trading")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch", help="토스에서 두 종목의 1분봉과 일봉을 받아 data/market.sqlite3에 저장한다")
    commands.add_parser("backtest", help="저장된 시세로 백테스트한다")
    checks = commands.add_parser("acceptance", help="v1 acceptance를 네트워크 없이 실행한다")
    checks.add_argument("--prd", choices=sorted(acceptance.CASES))
    commands.add_parser("v0-check", help="v0 일봉 빠른 확인을 실행한다")
    commands.add_parser("v0-acceptance", help="v0 acceptance를 실행한다")
    commands.add_parser("v2-backtest", help="저장된 두 DB로 v2 두 단계 백테스트를 한다")
    v2_checks = commands.add_parser("v2-acceptance", help="v2 acceptance를 네트워크 없이 실행한다")
    v2_checks.add_argument("--prd", choices=sorted(v2_acceptance.CASES))
    commands.add_parser("v3-daily", help="v3 매일 실행: 시세를 받아 밀린 날을 정산하고 오늘 헤드 판단을 한다")
    commands.add_parser("v3-us-fetch", help="토스에서 미국 네 종목(SOXX·QQQ·SPY·MU) 일봉을 받아 data/us.sqlite3에 저장한다")
    commands.add_parser("v3-report", help="v3 모의 운용 보고서와, 기간이 끝났으면 판정을 만든다")
    v3_checks = commands.add_parser("v3-acceptance", help="v3 acceptance를 네트워크 없이 실행한다")
    v3_checks.add_argument("--prd", choices=sorted(v3_acceptance.CASES))
    serve = commands.add_parser("v3-serve", help="평일 07:30·08:20 자동 실행과 보기 전용 현황 웹페이지(Docker 컨테이너에서 쓴다)")
    serve.add_argument("--host", default="127.0.0.1")
    commands.add_parser("v4-daily", help="v4 아침 실행: 시세를 받아 밀린 날을 정산하고 오늘 개장 전 판단을 한다")
    v4_decide = commands.add_parser("v4-decide", help="v4 장중·마감 전 판단 한 번: 당일 1분봉과 뉴스를 받아 판단한다")
    v4_decide.add_argument("--at", required=True, help="판단 차례 HH:MM")
    commands.add_parser("v4-report", help="v4 모의 운용 보고서와, 기간이 끝났으면 판정을 만든다")
    v4_serve_args = commands.add_parser("v4-serve", help="아침 실행·판단 차례마다 자동 실행과 보기 전용 현황 웹페이지(서버의 Docker 컨테이너에서 쓴다)")
    v4_serve_args.add_argument("--host", default="127.0.0.1")
    v4_checks = commands.add_parser("v4-acceptance", help="v4 acceptance를 네트워크 없이 실행한다")
    v4_checks.add_argument("--prd", choices=sorted(v4_acceptance.CASES))
    commands.add_parser("web-acceptance", help="현황 웹페이지 모양(web SPEC) acceptance를 네트워크 없이 실행한다")
    v5_checks = commands.add_parser("v5-acceptance", help="v5 acceptance를 네트워크 없이 실행한다")
    v5_checks.add_argument("--prd", choices=sorted(v5_acceptance.CASES))
    commands.add_parser("v5-daily", help="v5 아침 실행: 시세를 받아 밀린 날을 정산하고 오늘 개장 전 판단을 한다(기간 끝 없음)")
    v5_decide = commands.add_parser("v5-decide", help="v5 장중·마감 전 판단 한 번: 당일 1분봉과 뉴스를 받아 판단한다")
    v5_decide.add_argument("--at", required=True, help="판단 차례 HH:MM")
    commands.add_parser("v5-report", help="v5 보고서: 헤드 모의 계좌와 실장부, 주문, 날마다의 수익률")
    commands.add_parser("v5-live", help="v5 실계좌 실행기: 그날 끝 차례까지 목표 보유를 실계좌에 맞춘다")
    v5_stop = commands.add_parser("v5-stop", help="v5 실행기를 멈춘다(보유 그대로). --sell이면 멈추고 다 판다")
    v5_stop.add_argument("--sell", action="store_true")
    v5_resume = commands.add_parser("v5-resume", help="v5 정지를 푼다. --new-ledger면 실장부를 예산으로 새로 시작하고 푼다")
    v5_resume.add_argument("--new-ledger", action="store_true")
    v5_check = commands.add_parser("v5-check", help="토스 계좌를 조회한다. --order면 사람이 지켜보는 실주문 점검을 한다")
    v5_check.add_argument("--order", action="store_true")
    v5_serve_args = commands.add_parser("v5-serve", help="아침 실행·판단 차례 자동 실행, 실행기 돌보기와 보기 전용 현황 웹페이지(서버의 Docker 컨테이너에서 쓴다)")
    v5_serve_args.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    if args.command == "fetch":
        sys.exit(fetch())
    if args.command == "backtest":
        sys.exit(run_backtest(DB_PATH, REPORT_DIR, lambda: datetime.now(toss.KST)))
    if args.command == "acceptance":
        sys.exit(acceptance.run(args.prd))
    if args.command == "v2-backtest":
        sys.exit(v2_backtest.run(v0.DB_PATH, DB_PATH, REPORT_DIR, lambda: datetime.now(toss.KST)))
    if args.command == "v2-acceptance":
        sys.exit(v2_acceptance.run(args.prd))
    if args.command == "v3-daily":
        sys.exit(run_v3_daily())
    if args.command == "v3-us-fetch":
        sys.exit(fetch_us())
    if args.command == "v3-report":
        sys.exit(v3_report.run(V3_PATHS, v3_daily.PERIOD, v3_daily.HOLIDAYS, REPORT_DIR, lambda: datetime.now(toss.KST)))
    if args.command == "v3-acceptance":
        sys.exit(v3_acceptance.run(args.prd))
    if args.command == "v3-serve":
        sys.exit(v3_serve.run(V3_PATHS, v3_daily.PERIOD, v3_daily.HOLIDAYS, REPORT_DIR, V3_DIR / "daily.log", args.host, v3_serve.PORT))
    if args.command == "v4-daily":
        sys.exit(run_v4_daily())
    if args.command == "v4-decide":
        sys.exit(run_v4_decide(args.at))
    if args.command == "v4-report":
        sys.exit(v4_report.run(V4_PATHS, v4_daily.PERIOD, v3_daily.HOLIDAYS, REPORT_DIR, lambda: datetime.now(toss.KST)))
    if args.command == "v4-serve":
        sys.exit(v4_serve.run(V4_PATHS, v4_daily.PERIOD, v3_daily.HOLIDAYS, REPORT_DIR, V4_DIR / "run.log", args.host, v4_serve.PORT))
    if args.command == "v4-acceptance":
        sys.exit(v4_acceptance.run(args.prd))
    if args.command == "web-acceptance":
        sys.exit(web_acceptance.run())
    if args.command == "v5-acceptance":
        sys.exit(v5_acceptance.run(args.prd))
    if args.command == "v5-daily":
        sys.exit(run_v5_daily())
    if args.command == "v5-decide":
        sys.exit(run_v5_decide(args.at))
    if args.command == "v5-report":
        sys.exit(v5_report.run(V5_LIVE, v5_period, REPORT_DIR, lambda: datetime.now(toss.KST)))
    if args.command == "v5-stop":
        sys.exit(v5_live.stop(V5_LIVE, args.sell, lambda: datetime.now(toss.KST)))
    if args.command in ("v5-live", "v5-resume", "v5-check"):
        sys.exit(run_v5_live(args))
    if args.command == "v5-serve":
        sys.exit(v5_serve.run(V5_LIVE, REPORT_DIR, V5_DIR / "run.log", V5_DIR / "live.log", args.host, v5_serve.PORT))
    sys.exit(v0.check() if args.command == "v0-check" else v0_acceptance.run())


def fetch() -> int:
    with httpx.Client(base_url=toss.BASE_URL, timeout=30) as http:
        return run_fetch(http, toss.read_credentials(ROOT), DB_PATH, lambda: datetime.now(toss.KST), shared_token(http))


def fetch_us() -> int:
    with httpx.Client(base_url=toss.BASE_URL, timeout=30) as http:
        return v3_us.run(http, toss.read_credentials(ROOT), V3_PATHS.us, lambda: datetime.now(toss.KST), shared_token(http))


def shared_token(http: httpx.Client) -> toss.SharedToken:
    """서버의 모든 토스 호출이 함께 쓰는 토큰(v5 SPEC 5.3)."""
    return toss.SharedToken(http, toss.read_credentials(ROOT), ROOT / "data" / "toss" / "token.json")


def fetch_market(seconds: float) -> int:
    """v3-daily·v4-daily의 시세 받기. v3-us-fetch와 v1 fetch 명령을 seconds초 안에서 차례로 따로 실행한다."""
    try:
        toss.read_credentials(ROOT)
    except KeyError as error:
        print(f"V1_TOSS_CREDENTIALS_MISSING: .env나 환경 변수에 {error}가 없다")
        return 1
    deadline, codes = time.monotonic() + seconds, []
    for command in ("v3-us-fetch", "fetch"):  # 미국 시세는 그날 판단에 쓰이고 몇 초면 끝나서 먼저 받는다
        try:
            codes.append(subprocess.run(["rt-trading", command], timeout=deadline - time.monotonic()).returncode)
        except subprocess.TimeoutExpired:
            print(f"V3_FETCH_TIMEOUT: 판단 마감 15분 전까지 시세를 다 받지 못해 멈췄다({command})")
            return 1
    return 0 if codes == [0, 0] else 1


def run_v3_daily() -> int:
    """v3 매일 실행. 시세는 v3-us-fetch와 v1 fetch 명령을 따로 실행해 받고, Codex 토큰은 trading-oracle과 함께 쓴다."""
    clock = lambda: datetime.now(toss.KST)
    with httpx.Client() as codex_http:
        return v3_daily.run(
            V3_PATHS,
            v3_daily.PERIOD,
            v3_daily.HOLIDAYS,
            fetch_market,
            codex_http,
            lambda: codex.credentials(codex.TOKEN_DIR),
            clock,
            time.sleep,
        )


def run_v4_daily() -> int:
    """v4 아침 실행. 시세 받기는 v3와 같고, 뉴스는 NAVER API HUB에서 받는다."""
    clock = lambda: datetime.now(toss.KST)
    with httpx.Client() as http:
        news = lambda cutoff, deadline, path: v4_news.fetch(http, v4_news.read_credentials(ROOT), cutoff, deadline, clock, path)
        return v4_daily.run(
            V4_PATHS, v4_daily.PERIOD, v3_daily.HOLIDAYS, fetch_market, news, http, lambda: codex.credentials(codex.TOKEN_DIR), clock, time.sleep
        )


def run_v4_decide(slot: str) -> int:
    """v4 장중·마감 전 판단 한 번. 당일 1분봉은 토스에서, 뉴스는 NAVER API HUB에서 받는다."""
    clock = lambda: datetime.now(toss.KST)
    with httpx.Client() as http:
        news = lambda cutoff, deadline, path: v4_news.fetch(http, v4_news.read_credentials(ROOT), cutoff, deadline, clock, path)
        bars = lambda day, seconds: v4_daily.fetch_bars(ROOT, day, seconds)
        return v4_daily.decide(
            V4_PATHS, v4_daily.PERIOD, v3_daily.HOLIDAYS, slot, bars, news, http, lambda: codex.credentials(codex.TOKEN_DIR), clock, time.sleep
        )


def v5_period():
    """오늘(한국 시각) 기준의 v5 기간. 기간 마지막 날은 늘 오늘 뒤다(v5 PRD 02)."""
    return v5_target.load_period(v5_target.PERIOD, v3_daily.HOLIDAYS, datetime.now(toss.KST).date().isoformat())


def run_v5_daily() -> int:
    """v5 아침 실행. v4-daily와 같고, v5 파일 위치와 끝 없는 기간을 쓴다."""
    clock = lambda: datetime.now(toss.KST)
    with httpx.Client() as http:
        news = lambda cutoff, deadline, path: v4_news.fetch(http, v4_news.read_credentials(ROOT), cutoff, deadline, clock, path)
        return v4_daily.run(
            V5_PATHS, v5_target.PERIOD, v3_daily.HOLIDAYS, fetch_market, news, http, lambda: codex.credentials(codex.TOKEN_DIR), clock, time.sleep, v5_period
        )


def run_v5_decide(slot: str) -> int:
    """v5 장중·마감 전 판단 한 번. v4-decide와 같고, v5 파일 위치와 끝 없는 기간을 쓴다."""
    clock = lambda: datetime.now(toss.KST)
    with httpx.Client() as http:
        news = lambda cutoff, deadline, path: v4_news.fetch(http, v4_news.read_credentials(ROOT), cutoff, deadline, clock, path)
        bars = lambda day, seconds: v4_daily.fetch_bars(ROOT, day, seconds)
        return v4_daily.decide(
            V5_PATHS, v5_target.PERIOD, v3_daily.HOLIDAYS, slot, bars, news, http, lambda: codex.credentials(codex.TOKEN_DIR), clock, time.sleep, v5_period
        )


def run_v5_live(args: argparse.Namespace) -> int:
    """v5-live, v5-resume, v5-check. 토스 호출은 서버의 함께 쓰는 토큰을 쓴다."""
    clock = lambda: datetime.now(toss.KST)
    with httpx.Client(base_url=toss.BASE_URL, timeout=v5_live.REQUEST_TIMEOUT if args.command == "v5-live" else 30) as http:
        token = shared_token(http)
        if args.command == "v5-live":
            return v5_live.Live(V5_LIVE, http, token, clock, time.sleep).run()
        if args.command == "v5-resume":
            return v5_live.resume(V5_LIVE, http, token, clock, time.sleep, args.new_ledger)
        return v5_live.check(V5_LIVE, http, token, clock, time.sleep, args.order)


def run_backtest(db_path: Path, report_dir: Path, clock: Callable[[], datetime]) -> int:
    """저장된 시세를 검증하고 노이즈 영역 돌파를 시뮬레이션해 보고서를 만든다.

    검증에 실패하면 PRD 02의 코드를 출력하고 보고서 없이 1을 돌려준다. 후보가 통과하지 못해도 보고서를 만들면 0이다.
    """
    data, violations = market_data.load(db_path)
    if violations:
        print(market_data.describe(violations))
        return 1
    records = engine.run(data, strategies.NoiseBreakout())
    content = report.build(data, records, max(row[0] for row in store.read(db_path, "fetch_runs")))
    directory = report_dir / clock().strftime("%Y%m%d-%H%M%S")
    report.write(directory, content, records.fills)
    print(report.summary(content))
    print(f"보고서: {directory}")
    return 0


def run_fetch(
    http: httpx.Client, credentials: tuple[str, str], db_path: Path, clock: Callable[[], datetime], token: toss.SharedToken | None = None
) -> int:
    """두 종목의 1분봉 원가격과 일봉 원가격·수정 가격을 모두 받아 검사한 뒤 한 번에 저장한다.

    하나라도 실패하면 오류 코드를 출력하고 저장하지 않은 채 1을 돌려준다. token을 주지 않으면 토큰을 새로 받는다.
    """
    today = clock().date().isoformat()
    try:
        token = token or toss.issue_token(http, *credentials)
        minute, daily = [], []
        for symbol in SYMBOLS:
            minute += toss.minute_rows(symbol, toss.candles(http, token, symbol, "1m", False, SINCE), SINCE, today)
            daily += toss.daily_rows(
                symbol,
                toss.candles(http, token, symbol, "1d", False, SINCE),
                toss.candles(http, token, symbol, "1d", True, SINCE),
                SINCE,
                today,
            )
    except toss.TossError as error:
        print(error)
        return 1
    fetched_at = clock().isoformat(timespec="seconds")
    runs = []
    for symbol in SYMBOLS:
        for interval, rows in (("1m", minute), ("1d", daily)):
            dates = [row[1][:10] for row in rows if row[0] == symbol]
            runs.append((fetched_at, symbol, interval, dates[0], dates[-1], len(dates)))
    store.replace(db_path, minute, daily, runs)
    for _, symbol, interval, start, end, count in runs:
        print(f"{symbol} {interval} {start} ~ {end} {count:,}개")
    print(f"저장: {db_path}")
    return 0

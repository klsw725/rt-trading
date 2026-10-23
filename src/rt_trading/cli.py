"""rt-trading 명령."""

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from rt_trading import engine, market_data, report, store, strategies, toss, v0, v0_acceptance, v2_acceptance

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "market.sqlite3"
REPORT_DIR = ROOT / "data" / "reports"
SYMBOLS = ("069500", "114800")
SINCE = "2022-11-23"  # 토스 국내 1분봉 제공 시작일


def main() -> None:
    from rt_trading import acceptance  # acceptance가 이 모듈의 run_fetch를 쓰므로 여기서 불러온다

    parser = argparse.ArgumentParser(prog="rt-trading")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch", help="토스에서 두 종목의 1분봉과 일봉을 받아 data/market.sqlite3에 저장한다")
    commands.add_parser("backtest", help="저장된 시세로 백테스트한다")
    checks = commands.add_parser("acceptance", help="v1 acceptance를 네트워크 없이 실행한다")
    checks.add_argument("--prd", choices=sorted(acceptance.CASES))
    commands.add_parser("v0-check", help="v0 일봉 빠른 확인을 실행한다")
    commands.add_parser("v0-acceptance", help="v0 acceptance를 실행한다")
    v2_checks = commands.add_parser("v2-acceptance", help="v2 acceptance를 네트워크 없이 실행한다")
    v2_checks.add_argument("--prd", choices=sorted(v2_acceptance.CASES))
    args = parser.parse_args()
    if args.command == "fetch":
        sys.exit(fetch())
    if args.command == "backtest":
        sys.exit(run_backtest(DB_PATH, REPORT_DIR, lambda: datetime.now(toss.KST)))
    if args.command == "acceptance":
        sys.exit(acceptance.run(args.prd))
    if args.command == "v2-acceptance":
        sys.exit(v2_acceptance.run(args.prd))
    sys.exit(v0.check() if args.command == "v0-check" else v0_acceptance.run())


def fetch() -> int:
    with httpx.Client(base_url=toss.BASE_URL, timeout=30) as http:
        return run_fetch(http, toss.read_credentials(ROOT), DB_PATH, lambda: datetime.now(toss.KST))


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


def run_fetch(http: httpx.Client, credentials: tuple[str, str], db_path: Path, clock: Callable[[], datetime]) -> int:
    """두 종목의 1분봉 원가격과 일봉 원가격·수정 가격을 모두 받아 검사한 뒤 한 번에 저장한다.

    하나라도 실패하면 오류 코드를 출력하고 저장하지 않은 채 1을 돌려준다.
    """
    today = clock().date().isoformat()
    try:
        token = toss.issue_token(http, *credentials)
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

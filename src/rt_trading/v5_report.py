"""v5 보고서: 헤드 모의 계좌와 실장부, 주문, 날마다의 수익률(v5 SPEC 7절, PRD 04)."""

import csv
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rt_trading import codex, engine, report, v3_daily, v4_report, v5_book, v5_live

ORDER_COLUMNS = [
    "client_order_id", "kind", "date", "turn", "symbol", "side", "tif", "quantity", "toss_status",
    "filled_quantity", "filled_amount", "average_price", "commission", "tax", "error",
]
DAILY_COLUMNS = ["date", "head_return", "close_equity", "bot_return"]


def build(paths: v5_live.Paths, period: v3_daily.Period) -> tuple[dict, list[dict], dict, list[dict], list[dict]]:
    """(report.json 내용, decisions.csv 행, 계좌별 결과, orders.csv 행, daily.csv 행). 정산한 날이 있어야 한다.

    run은 다시 만든다. v4_report.build의 run에는 엔진에 주는 기간 마지막 날(PRD 02)이 있어 쓰지 않는다.
    """
    base, rows, records = v4_report.build(paths.daily, period)
    settled = v3_daily.settled_days(paths.daily.paper)
    run = {
        "start": period.start, "settled_through": settled[-1], "model": codex.MODEL, "initial_capital": engine.INITIAL_CASH,
        "commission_rate": report.COMMISSION_RATE, "loss_limits": report.LOSS_LIMITS,
    }
    head = [engine.INITIAL_CASH] + [records["head"].closes[day] for day in settled]
    live, daily = None, []
    with v5_book.connect(paths.live) as db:
        orders = v5_book.orders(db, "kind != 'dry'")
        ledger = v5_book.ledger(db, "live")
        if ledger:
            cash, held = v5_book.balance(db, ledger["id"])
            equity = v5_live.close_equity(paths, db, ledger, settled[-1])
            mine = [order for order in orders if order["kind"] == "live" and order["ledger_id"] == ledger["id"]]
            live = {
                "start_cash": ledger["start_cash"],
                "cash": cash,
                "holdings": held,
                "close_equity": equity,
                "total_return": None if equity is None else equity / ledger["start_cash"] - 1,
                "commission_total": sum(order["applied_commission"] for order in mine),
                "tax_total": sum(order["applied_tax"] for order in mine),
                "orders": sum(order["order_id"] is not None for order in mine),
                "safety_stops": db.execute("SELECT COUNT(*) FROM stops WHERE kind IN ('safety', 'manual')").fetchone()[0],
            }
            previous = ledger["start_cash"]
            for index, day in enumerate(settled):
                if day < ledger["created_at"][:10]:
                    continue
                closing = v5_live.close_equity(paths, db, ledger, day)
                daily.append(
                    {
                        "date": day,
                        "head_return": head[index + 1] / head[index] - 1,
                        "close_equity": closing,
                        "bot_return": None if closing is None or previous is None else closing / previous - 1,
                    }
                )
                previous = closing
    content = {"run": run, "decisions": base["decisions"], "accounts": base["accounts"], "verdict": None, "live": live}
    return content, rows, records, orders, daily


def run(paths: v5_live.Paths, period: Callable[[], v3_daily.Period], report_dir: Path, clock: Callable[[], datetime]) -> int:
    """보고서를 만들면 0, 기간 설정이 틀렸거나 정산한 날이 없으면 1이다."""
    try:
        found = period()
    except v3_daily.DailyError as error:
        print(error)
        return 1
    if not v3_daily.settled_days(paths.daily.paper):
        print("V5_NOTHING_SETTLED: 정산한 날이 없다")
        return 1
    content, rows, records, orders, daily = build(paths, found)
    directory = report_dir / f"v5-{clock().strftime('%Y%m%d-%H%M%S')}"
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, columns, items in (("decisions.csv", v4_report.DECISION_COLUMNS, rows), ("orders.csv", ORDER_COLUMNS, orders), ("daily.csv", DAILY_COLUMNS, daily)):
        with (directory / name).open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(items)
    for name, result in records.items():
        report.write_trades(directory / f"trades-{name}.csv", result.fills)
    head = next(item for item in content["accounts"] if item["id"] == "head")
    print(f"헤드 모의 계좌: {head['final_equity']:,}원 ({head['total_return']:+.2%}), 정산한 마지막 날 {content['run']['settled_through']}")
    live = content["live"]
    if live:
        equity = "계산할 수 없음" if live["close_equity"] is None else f"{live['close_equity']:,}원 ({live['total_return']:+.2%})"
        print(f"실장부: 마감 봇 자산 {equity}, 현금 {live['cash']:,}원, 보유 {live['holdings'] or '없음'}, 실주문 {live['orders']}건")
    else:
        print("실장부 없음")
    print(f"보고서: {directory}")
    return 0

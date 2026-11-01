"""v4 보고서와 판정, 터미널 요약(v4 SPEC 6·7절, PRD 04)."""

import csv
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rt_trading import codex, engine, report, v3_daily, v3_report, v4_daily, v4_head, v4_paper

STATUSES = ("ok", "capped", "late", "invalid", "failed", "missing")
DECISION_COLUMNS = ["date", "slot", "kind", "subordinate", "reason", "status", "model", "reported_model", "head_return"]


def verdict(head: engine.Records, statuses: list[str]) -> dict:
    """SPEC 6절. statuses는 기간 5거래일의 모든 판단 차례의 상태다. capped는 실패가 아니고, 낙폭은 report.metrics와 같은 정수 판정이다."""
    criteria = {
        "no_decision_failures": all(status in ("ok", "capped") for status in statuses),
        "drawdown_below_limit": report.metrics(head)["criteria"]["drawdown_below_limit"],
    }
    return {"criteria": criteria, "passed": all(criteria.values())}


def build(paths: v4_daily.Paths, period: v3_daily.Period) -> tuple[dict, list[dict], dict[str, engine.Records]]:
    """(report.json 내용, decisions.csv 행, 계좌별 결과). 정산한 날이 있어야 한다."""
    settled = v3_daily.settled_days(paths.paper)
    data = v3_daily.settled_data(paths, settled)
    decisions = v4_head.read(paths.paper)
    records = v4_paper.run_accounts(data, v3_daily.calendar(data.days, period), period.start, period.final_day, decisions)
    closes = [engine.INITIAL_CASH] + [records["head"].closes[day] for day in settled]
    head_return = {day: closes[i + 1] / closes[i] - 1 for i, day in enumerate(settled)}
    accounts = []
    for name in v4_paper.ACCOUNTS:
        metrics = report.metrics(records[name])
        accounts.append(
            {
                "id": name,
                **{key: metrics[key] for key in v3_report.METRICS},
                "distribution_total": sum(amount for _, _, amount, _ in records[name].distributions),
                "distribution_tax_total": sum(tax for _, _, _, tax in records[name].distributions),
            }
        )
    content = {
        "run": {
            "start": period.start,
            "end": period.final_day,
            "trading_days": len(period.days),
            "decision_slots": sum(len(v4_head.slots(day)) for day in period.days),
            "settled_days": len(settled),
            "settled_through": settled[-1],
            "model": codex.MODEL,
            "initial_capital": engine.INITIAL_CASH,
            "commission_rate": report.COMMISSION_RATE,
            "loss_limits": report.LOSS_LIMITS,
            "defect_days": [day for day in data.defects if period.start <= day <= period.final_day],
        },
        "decisions": {name: sum(row["status"] == name for row in decisions if row["date"] in settled) for name in STATUSES},
        "accounts": accounts,
        "verdict": verdict(records["head"], [row["status"] for row in decisions if row["date"] in period.days])
        if period.final_day in settled
        else None,
    }
    rows = [
        {**{key: row[key] for key in DECISION_COLUMNS[:-1]}, "head_return": head_return.get(row["date"], "")}
        for row in decisions
        if row["date"] in period.days
    ]
    return content, rows, records


def run(paths: v4_daily.Paths, period_path: Path, holidays_path: Path, report_dir: Path, clock: Callable[[], datetime]) -> int:
    """보고서를 만들면 판정 여부와 관계없이 0, 기간 설정이 틀렸거나 정산한 날이 없으면 1이다."""
    try:
        period = v3_daily.load_period(period_path, holidays_path, "V4_PERIOD_INVALID")
    except v3_daily.DailyError as error:
        print(error)
        return 1
    if not v3_daily.settled_days(paths.paper):
        print("V4_NOTHING_SETTLED: 정산한 날이 없다")
        return 1
    content, rows, records = build(paths, period)
    directory = report_dir / f"v4-{clock().strftime('%Y%m%d-%H%M%S')}"
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (directory / "decisions.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, DECISION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    for name, result in records.items():
        report.write_trades(directory / f"trades-{name}.csv", result.fills)
    print(v3_report.summary(content, records))
    print(f"보고서: {directory}")
    return 0

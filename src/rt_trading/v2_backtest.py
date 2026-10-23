"""v2 실행: 두 DB 확인, 1단계와 2단계, 최종 판정, 보고서와 터미널 요약(v2 SPEC 3·6·7절, PRD 04)."""

import json
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rt_trading import engine, market_data, report, store, v2_stage1, v2_strategies

ORDER = ("turn_of_month", "trend_200", "last_half_hour", "overnight_trend")  # ①②③④
PARAMS = {**v2_stage1.PARAMS, "last_half_hour": {"signal_after_open_minutes": 30, "buy_before_close_minutes": 30}}
MIN_DAYS = v2_strategies.TREND_DAYS + 1  # ②·④의 첫 판단일(201번째 거래일)이 있어야 한다
CAUTION = (
    "v2까지 후보 7개를 시험했으므로 통과해도 운일 수 있다. ④는 v0 결과를 본 뒤 만든 변형이고,"
    " ③은 4년 남짓한 1분봉으로만 시험했다. 통과해도 실계좌 운용 승인이 아니다."
)


def run(v0_path: Path, market_path: Path, report_dir: Path, clock: Callable[[], datetime]) -> int:
    """두 DB를 확인하고 두 단계를 실행해 보고서를 만든다.

    확인에 실패하면 어긴 것을 모두 출력하고 보고서 없이 1을 돌려준다. 후보가 통과하지 못해도 보고서를 만들면 0이다.
    """
    problems = []
    try:
        daily = v2_stage1.load(v0_path)
    except v2_stage1.Stage1Error as error:
        problems.append(str(error))
    data, violations = market_data.load(market_path)
    problems += [market_data.describe(violations)] if violations else _check_stage2(data)
    if problems:
        print("\n".join(problems))
        return 1
    stage1 = v2_stage1.build(daily)
    first = {result["id"]: result["passed"] for result in stage1["candidates"]}
    tested = [candidate for candidate in ORDER if _stage(first, candidate) != "failed"]
    records = {candidate: engine.run(data, v2_strategies.CANDIDATES[candidate](), *v2_strategies.SETTINGS[candidate]) for candidate in tested}
    content = _report(data, records, max(row[0] for row in store.read(market_path, "fetch_runs")))
    second = {result["id"]: result["passed"] for result in content["candidates"]}
    verdict = {"candidates": [], "caution": CAUTION}
    for candidate in ORDER:
        stages = {"stage1": _stage(first, candidate), "stage2": _stage(second, candidate)}
        verdict["candidates"].append({"id": candidate, **stages, "passed": "failed" not in stages.values()})
    directory = report_dir / f"v2-{clock().strftime('%Y%m%d-%H%M%S')}"
    directory.mkdir(parents=True)
    for name, value in (("stage1.json", stage1), ("report.json", content), ("verdict.json", verdict)):
        (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for candidate, result in records.items():
        report.write_trades(directory / f"trades-{candidate}.csv", result.fills)
    print(summary(stage1, content, verdict))
    print(f"보고서: {directory}")
    return 0


def _check_stage2(data: market_data.MarketData) -> list[str]:
    """v1 검증을 통과한 시세의 2단계 확인: 거래일 수와 마지막 거래일 두 종목의 종가 동시호가 봉."""
    problems = []
    if len(data.days) < MIN_DAYS:
        problems.append(f"V2_STAGE2_DATA_INVALID: 거래일이 {len(data.days)}일이라 {MIN_DAYS}일보다 적다")
    last = data.days[-1]
    close_time = data.sessions[last].bars[-1]
    for symbol in market_data.SYMBOLS:
        if not any(bar.time == close_time and bar.volume > 0 for bar in data.bars[symbol][last]):
            problems.append(f"V2_STAGE2_DATA_INVALID: 마지막 거래일 {last} {symbol}에 거래량이 있는 {close_time} 종가 동시호가 봉이 없다")
    return problems


def _stage(results: dict[str, bool], candidate: str) -> str:
    if candidate not in results:
        return "not_tested"
    return "passed" if results[candidate] else "failed"


def _report(data: market_data.MarketData, records: dict[str, engine.Records], fetched_at: str) -> dict:
    """2단계 report.json 내용. v1 PRD 05의 계약에서 시험 기간을 후보마다 두고 분배금 합계를 더한다."""
    candidates = []
    for candidate, result in records.items():
        days = list(result.closes)
        candidates.append(
            {
                "id": candidate,
                "params": PARAMS[candidate],
                "simulation_start": days[0],
                "simulation_end": days[-1],
                "trading_days": len(days),
                **report.metrics(result),
                "distribution_total": sum(amount for _, _, amount, _ in result.distributions),
                "distribution_tax_total": sum(tax for _, _, _, tax in result.distributions),
            }
        )
    return {
        "run": {
            "fetched_at": fetched_at,
            "data_start": data.days[0],
            "data_end": data.days[-1],
            "initial_capital": engine.INITIAL_CASH,
            "commission_rate": report.COMMISSION_RATE,
            "loss_limits": report.LOSS_LIMITS,
            "defect_days": data.defects,
        },
        "candidates": candidates,
    }


def summary(stage1: dict, content: dict, verdict: dict) -> str:
    """터미널 요약표. 후보마다 시험한 단계의 지표와 판정, 최종 판정을 한 줄씩 보여 준다."""
    results = {(1, item["id"]): item for item in stage1["candidates"]} | {(2, item["id"]): item for item in content["candidates"]}
    rows = [("후보", "단계", "최종 자산", "총수익", "최대 낙폭", "앞 기간 수익", "뒤 기간 수익", "판정")]
    for item in verdict["candidates"]:
        for stage in (1, 2):
            result = results.get((stage, item["id"]))
            if result:
                final = f"{result['final_equity']:.4f}" if stage == 1 else f"{result['final_equity']:,}원"
                rows.append(
                    (
                        item["id"],
                        f"{stage}단계",
                        final,
                        f"{result['total_return']:+.2%}",
                        f"{result['max_drawdown']:.2%}",
                        f"{result['first_half_return']:+.2%}",
                        f"{result['second_half_return']:+.2%}",
                        "통과" if result["passed"] else "불통과",
                    )
                )
        rows.append((item["id"], "최종", "", "", "", "", "", "통과" if item["passed"] else "불통과"))
    widths = [max(_width(row[column]) for row in rows) for column in range(len(rows[0]))]
    return "\n".join("  ".join(cell + " " * (width - _width(cell)) for cell, width in zip(row, widths)).rstrip() for row in rows)


def _width(text: str) -> int:
    """터미널에서 한글은 두 칸을 차지한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)

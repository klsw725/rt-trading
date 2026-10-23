"""지표, 통과 판정, 보고서 파일, 터미널 요약(SPEC 6~7절)."""

import csv
import json
import unicodedata
from pathlib import Path

from rt_trading import engine, strategies
from rt_trading.market_data import MarketData

CANDIDATE = "noise_breakout"
COMMISSION_RATE = 0.000188
LOSS_LIMITS = {"daily": -0.02, "weekly": -0.05, "cumulative": -0.20}
LIMIT_KINDS = ("daily", "weekly", "cumulative")
TRADE_COLUMNS = ["time", "symbol", "side", "quantity", "price", "commission", "tax", "reason"]


def build(data: MarketData, records: engine.Records, fetched_at: str) -> dict:
    """report.json 내용. 실행 시각은 넣지 않아 같은 입력이면 같은 내용이 나온다."""
    start, closes = engine.INITIAL_CASH, list(records.closes.items())
    days, final = len(closes), closes[-1][1]
    boundary = closes[days // 2 - 1][1] if days // 2 else start  # 앞 기간은 처음 floor(N/2)거래일
    peak = drawdown = 0
    for _, equity in records.equity:  # 첫 값 E_0(시작 자금)부터 본다
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    year_end = {date[:4]: equity for date, equity in closes}
    yearly, previous = {}, start
    for year, equity in year_end.items():
        yearly[year] = equity / previous - 1
        previous = equity
    pnls = [trade.pnl for trade in records.trades]
    first, second = boundary / start - 1, final / boundary - 1
    criteria = {
        "net_positive": final > start,
        "drawdown_below_limit": drawdown < -LOSS_LIMITS["cumulative"],
        "both_halves_positive": first > 0 and second > 0,
    }
    simulation_start, simulation_end = closes[0][0], closes[-1][0]
    return {
        "run": {
            "fetched_at": fetched_at,
            "data_start": data.days[0],
            "data_end": data.days[-1],
            "simulation_start": simulation_start,
            "simulation_end": simulation_end,
            "trading_days": days,
            "initial_capital": start,
            "commission_rate": COMMISSION_RATE,
            "loss_limits": LOSS_LIMITS,
            "defect_days": [day for day in data.defects if simulation_start <= day <= simulation_end],
        },
        "candidates": [
            {
                "id": CANDIDATE,
                "params": strategies.PARAMS,
                "final_equity": final,
                "total_return": final / start - 1,
                "annualized_return": (final / start) ** (252 / days) - 1,
                "max_drawdown": drawdown,
                "first_half_return": first,
                "second_half_return": second,
                "yearly_returns": yearly,
                "trade_count": len(pnls),
                "win_rate": sum(pnl > 0 for pnl in pnls) / len(pnls) if pnls else None,
                "avg_trade_pnl": sum(pnls) / len(pnls) if pnls else None,
                "commission_total": sum(fill.fee for fill in records.fills),
                "tax_total": sum(fill.tax for fill in records.fills),
                "limit_triggers": {kind: sum(hit == kind for _, hit in records.limits) for kind in LIMIT_KINDS},
                "criteria": criteria,
                "passed": all(criteria.values()),
            }
        ],
    }


def write(directory: Path, content: dict, fills: list[engine.Fill]) -> None:
    """report.json과 trades-{id}.csv를 저장한다."""
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (directory / f"trades-{CANDIDATE}.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(TRADE_COLUMNS)
        for fill in fills:
            time = f"{fill.time[:10]}T{fill.time[11:]}:00+09:00"
            writer.writerow([time, fill.symbol, fill.side, fill.quantity, fill.price, fill.fee, fill.tax, fill.reason])


def summary(content: dict) -> str:
    """터미널 요약표."""
    result = content["candidates"][0]
    rows = [
        ("후보", result["id"]),
        ("최종 자산", f"{result['final_equity']:,}원"),
        ("총수익", f"{result['total_return']:+.2%}"),
        ("연환산 수익", f"{result['annualized_return']:+.2%}"),
        ("최대 낙폭", f"{result['max_drawdown']:.2%}"),
        ("앞 기간 수익", f"{result['first_half_return']:+.2%}"),
        ("뒤 기간 수익", f"{result['second_half_return']:+.2%}"),
        ("판정", "통과" if result["passed"] else "불통과"),
    ]
    width = max(_width(label) for label, _ in rows)
    return "\n".join(label + " " * (width - _width(label) + 2) + value for label, value in rows)


def _width(text: str) -> int:
    """터미널에서 한글은 두 칸을 차지한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)

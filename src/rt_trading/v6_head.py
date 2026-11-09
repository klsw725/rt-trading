"""v6 목표 보유 헤드(v6 SPEC 3절, PRD 02): 시각, C까지의 후보, 평가 자산과 계좌 요약, 입력, Codex 호출과 검증, 판단 기록."""

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx

from rt_trading import codex, v3_head, v6_market
from rt_trading.toss import KST

MODEL = "gpt-6-sol"  # v6의 모델. codex.MODEL을 따라가지 않는다
PROMPT = Path(__file__).with_name("v6_head_prompt.txt").read_text(encoding="utf-8")
CUTOFF_AFTER = timedelta(minutes=2)  # 입력 마감 C = 판단 시각 T + 2분
RETRY_WAIT = 15  # 첫 호출이 실패하면 다시 부르기 전에 기다리는 초
START_EQUITY = 500_000
MAX_POSITIONS, MAX_WEIGHT = 5, Decimal("0.4")
LENGTHS = {"strategy": 40, "reason": 500, "notes": 1000}
RECENT = 10
LIMITS = {"max_positions": 5, "max_weight": 0.4, "max_total_weight": 1, "loss_limits_pct": {"daily": -2, "weekly": -5, "cumulative": -20}}
COLUMNS = ("time", "status", "positions", "strategy", "reason", "notes", "model", "reported_model", "attempts", "error", "started_at", "finished_at")
TABLE = ", ".join(f"{column} {'INTEGER' if column == 'attempts' else 'TEXT'} NOT NULL" for column in COLUMNS) + ", PRIMARY KEY (time)"


class HeadError(Exception):
    """오류 코드와 설명."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def times(slot: str) -> tuple[datetime, datetime, datetime]:
    """차례 이름 'YYYY-MM-DD HH:MM'의 (판단 시각 T, 입력 마감 C, 체결 시각 E)."""
    day, clock = slot.split(" ")
    decided = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=KST)
    return decided, decided + CUTOFF_AFTER, v6_market.fill_time(day, clock)


def candidates_at(
    cutoff: datetime,
    rankings: dict[str, tuple[list[dict], datetime]],
    infos: tuple[dict[str, dict], datetime] | None,
    saved_infos: dict[str, dict],
    holdings: dict[str, int],
    open_at: dict,
    total: int,
    used: dict,
) -> list[dict]:
    """C까지 받은 이번 랭킹·종목 정보로 만든 후보(SPEC 2.2).

    이번 종목 정보를 C까지 받지 못했으면 랭킹은 쓰지 않고 보유 종목만 마지막 저장값으로 넣는다. 저장값도 C 이하에 받은 것만 쓴다.
    total과 used는 valuation의 C 평가다. 1주 값(보유 종목은 C 평가에 쓴 값, 아니면 랭킹 현재가. 미국은 C 평가의 환율로 바꾼다)이
    total의 40%보다 크거나 알 수 없는 랭킹 종목은 고르지 않고(보유 종목은 남는다), 후보마다 1주 비중 one_share_pct를 붙인다.
    """
    current = infos[0] if infos is not None and infos[1] <= cutoff else None
    usable = {} if current is None else {market: items for market, (items, observed) in rankings.items() if observed <= cutoff}
    chosen = dict(current or {})
    for symbol, quantity in holdings.items():
        saved = saved_infos.get(symbol)
        if quantity > 0 and symbol not in chosen and saved and datetime.fromisoformat(saved["observed_at"]) <= cutoff:
            chosen[symbol] = saved
    rate = used["USDKRW"][0] if "USDKRW" in used else None
    listed = {item["symbol"]: (market, item["last_price"]) for market, items in usable.items() for item in items}

    def won(market: str, price: object) -> Decimal | None:
        if price is None or (market == "US" and rate is None):
            return None
        return Decimal(price) * (1 if market == "KR" else rate)

    skip = frozenset(symbol for symbol, (market, price) in listed.items() if (cost := won(market, price)) is None or cost > total * MAX_WEIGHT)
    result = v6_market.candidates(usable, chosen, holdings, open_at, skip)
    for item in result:
        cost = won(item["market"], used[item["symbol"]][0] if item["symbol"] in used else listed.get(item["symbol"], (None, None))[1])
        item["one_share_pct"] = v3_head.pct(cost / total) if cost is not None and total > 0 else None
    return result


def equity(krw: int, usd: Decimal, positions: dict[str, dict], prices: dict[str, Decimal], rate: Decimal | None) -> int:
    """원화 평가 자산(정수 원). 원화 현금 + 국내 수량 × 가격 + (달러 현금 + 미국 수량 × 가격) × rate를 원 미만 ROUND_HALF_UP한다."""
    total, dollars = Decimal(krw), Decimal(usd)
    for symbol, position in positions.items():
        value = position["quantity"] * prices[symbol]
        if position["market"] == "KR":
            total += value
        else:
            dollars += value
    if dollars:
        total += dollars * rate
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def valuation(state: dict, observed: dict, cutoff: datetime) -> tuple[int, dict]:
    """C 평가 자산과 쓴 값: (A_C, 쓴 값).

    state는 {"krw", "usd", "positions": {종목: {"market", "quantity", "avg_price", …}}, "last": {종목·USDKRW: (값, 받은 시각)}},
    observed는 이번 차례에 받은 {"prices": {종목: (값, 받은 시각)}, "rate": (값, 받은 시각) 또는 None}이다.
    값은 C까지 받은 이번 값, 없으면 마지막 값이고, 쓴 값은 종목·USDKRW마다 (값, 받은 시각, 이번에 받았는지)다.
    """
    used = {symbol: _pick(observed["prices"].get(symbol), state["last"].get(symbol), cutoff) for symbol in state["positions"]}
    rate = _pick(observed.get("rate"), state["last"].get("USDKRW"), cutoff)
    if rate is not None or state["usd"] or any(position["market"] == "US" for position in state["positions"].values()):
        used["USDKRW"] = rate
    if None in used.values():
        missing = [key for key, value in used.items() if value is None]
        raise HeadError("V6_VALUE_MISSING", f"받은 적이 없는 값이 있다({', '.join(missing)})")
    prices = {symbol: used[symbol][0] for symbol in state["positions"]}
    return equity(state["krw"], state["usd"], state["positions"], prices, rate[0] if rate else None), used


def account(
    state: dict, observed: dict, cutoff: datetime, bases: dict, history: list[int], blocked: list[str], candidates: list[dict]
) -> tuple[int, dict, dict]:
    """C 평가: (A_C, 쓴 값, 계좌 요약). A_C와 쓴 값은 valuation과 같다."""
    total, used = valuation(state, observed, cutoff)
    prices = {symbol: used[symbol][0] for symbol in state["positions"]}
    midrate = used["USDKRW"][0] if "USDKRW" in used else None
    flags = {item["symbol"]: item for item in candidates}
    holdings = []
    for symbol in sorted(state["positions"]):
        position, price = state["positions"][symbol], prices[symbol]
        value = position["quantity"] * price * (1 if position["market"] == "KR" else midrate)
        item = flags.get(symbol, {})
        holdings.append(
            {
                "symbol": symbol,
                "name": item.get("name", ""),
                "market": position["market"],
                "weight_pct": v3_head.pct(value / total),
                "return_pct": v3_head.pct(price / position["avg_price"] - 1),
                "tradable_now": item.get("tradable_now", False),
                "exit_only": item.get("exit_only", False),
            }
        )
    peak, drawdown = START_EQUITY, Decimal(0)
    for value in [START_EQUITY, *history, total]:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - Decimal(value) / peak)
    summary = {
        "krw_cash_pct": v3_head.pct(Decimal(state["krw"]) / total),
        "usd_cash_pct": v3_head.pct(state["usd"] * midrate / total) if state["usd"] else 0.0,
        "holdings": holdings,
        "return_since_start_pct": v3_head.pct(Decimal(total) / START_EQUITY - 1),
        "today_return_pct": v3_head.pct(Decimal(total) / bases["daily"] - 1),
        "week_return_pct": v3_head.pct(Decimal(total) / bases["weekly"] - 1),
        "max_drawdown_pct": v3_head.pct(drawdown),
        "buy_blocked": list(blocked),
    }
    return total, used, summary


def build_input(
    slot: str, open_at: dict, ends_at: dict, summary: dict, candidates: list[dict], news: list[dict] | None, decisions: list[dict]
) -> dict:
    """헤드 입력(SPEC 3.2). decisions는 decisions 표의 행을 차례 순으로 넣는다."""
    decided, cutoff, fill_at = times(slot)
    sessions = {market: None if open_at[market] is None else {"name": open_at[market], "ends_at": _minute(ends_at[market])} for market in ("KR", "US")}
    earlier = [row for row in decisions if row["time"] < slot]
    return {
        "decision": {"time": _minute(decided), "input_cutoff": _minute(cutoff), "fill_at": _minute(fill_at), "sessions": sessions},
        "account": summary,
        "candidates": candidates,
        "news": news,
        "recent_decisions": [
            {"time": row["time"], "status": row["status"], "positions": json.loads(row["positions"]), "strategy": row["strategy"], "reason": row["reason"]}
            for row in earlier[-RECENT:]
        ],
        "notes": next((row["notes"] for row in reversed(earlier) if row["status"] == "ok"), ""),
        "limits": LIMITS,
    }


def parse(text: str, candidates: list[dict]) -> dict:
    """응답을 검증해 {"positions": [{"symbol", "weight": Decimal}], "strategy", "reason", "notes"}를 돌려준다. 어기면 V6_HEAD_RESPONSE_INVALID."""
    text = text.strip()
    if fenced := v3_head.FENCE.fullmatch(text):
        text = fenced.group(1)
    try:
        value = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise HeadError("V6_HEAD_RESPONSE_INVALID", f"JSON 객체 하나가 아니다({error.msg})") from error
    if not isinstance(value, dict) or set(value) != {"positions", "strategy", "reason", "notes"}:
        raise HeadError("V6_HEAD_RESPONSE_INVALID", "키가 positions, strategy, reason, notes 넷이 아니다")
    positions = value["positions"]
    if not isinstance(positions, list) or len(positions) > MAX_POSITIONS:
        raise HeadError("V6_HEAD_RESPONSE_INVALID", "positions가 5개 이하의 목록이 아니다")
    allowed = {item["symbol"] for item in candidates if not item["exit_only"]}
    chosen, total = [], Decimal(0)
    for item in positions:
        if not isinstance(item, dict) or set(item) != {"symbol", "weight"}:
            raise HeadError("V6_HEAD_RESPONSE_INVALID", "positions 항목의 키가 symbol, weight 둘이 아니다")
        symbol, weight = item["symbol"], item["weight"]
        if not isinstance(symbol, str) or symbol not in allowed:
            raise HeadError("V6_HEAD_RESPONSE_INVALID", f"후보에 없거나 exit_only인 종목 {symbol!r}")
        if symbol in [entry["symbol"] for entry in chosen]:
            raise HeadError("V6_HEAD_RESPONSE_INVALID", f"겹친 종목 {symbol}")
        if isinstance(weight, bool) or not isinstance(weight, (int, Decimal)) or not 0 < weight <= MAX_WEIGHT:
            raise HeadError("V6_HEAD_RESPONSE_INVALID", f"{symbol} 비중 {weight!r}이 0보다 크고 0.4 이하가 아니다")
        chosen.append({"symbol": symbol, "weight": Decimal(weight)})
        total += Decimal(weight)
    if total > 1:
        raise HeadError("V6_HEAD_RESPONSE_INVALID", f"비중 합 {total}이 1보다 크다")
    for key, limit in LENGTHS.items():
        if not isinstance(value[key], str) or len(value[key]) > limit:
            raise HeadError("V6_HEAD_RESPONSE_INVALID", f"{key}가 {limit}자 이하의 글자가 아니다")
    return {"positions": chosen, "strategy": value["strategy"], "reason": value["reason"], "notes": value["notes"]}


def decide(
    head_dir: Path,
    slot: str,
    content: dict,
    observations: dict,
    candidates: list[dict],
    http: httpx.Client,
    credentials: Callable[[], tuple[str, str]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> tuple[dict, str | None] | None:
    """판단을 정해 (기록할 행, 마지막 응답 원문)을 돌려준다. 저장소에는 쓰지 않는다(PRD 04가 record로 쓴다).

    호출 전에 input.json을 쓴다. 첫 호출이 호출 오류·형식 오류면 15초 뒤 한 번 더 부르고, 응답은 E까지만 기다린다.
    시작할 때 이미 E이거나 늦으면 부르지 않고 None이다.
    """
    _, _, fill_at = times(slot)
    started = clock()
    if started >= fill_at:
        return None
    folder = _folder(head_dir, slot)
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(folder / "input.json", {"input": content, "observed_at": observations})
    text = json.dumps(content, ensure_ascii=False)
    attempts, raw, reported, decision, status, error = 0, None, "", None, "failed", ""
    while True:
        attempts += 1
        outcome = _call(http, credentials, text, (fill_at - clock()).total_seconds())
        if isinstance(outcome, codex.Reply):
            raw, reported = outcome.text, outcome.model or reported
        if outcome is None or clock() >= fill_at:
            status, error = "late", "V6_HEAD_LATE"
            break
        if isinstance(outcome, codex.Reply):
            try:
                parsed = parse(outcome.text, candidates)
            except HeadError as failure:
                status, error = "invalid", failure.code
            else:
                if clock() >= fill_at:  # 검증까지 마친 시각이 E 전이어야 한다
                    status, error = "late", "V6_HEAD_LATE"
                else:
                    decision = parsed
                break
        else:
            status, error = "failed", getattr(outcome, "code", type(outcome).__name__)
            if error == "V3_CODEX_TOKEN_MISSING":
                break
        if attempts == 2 or clock() + timedelta(seconds=RETRY_WAIT) >= fill_at:
            break
        sleep(RETRY_WAIT)
    ok = decision is not None
    row = {
        "time": slot,
        "status": "ok" if ok else status,
        "positions": json.dumps([{"symbol": item["symbol"], "weight": float(item["weight"])} for item in decision["positions"]]) if ok else "[]",
        "strategy": decision["strategy"] if ok else "",
        "reason": decision["reason"] if ok else "",
        "notes": decision["notes"] if ok else "",
        "model": MODEL,
        "reported_model": reported,
        "attempts": attempts,
        "error": "" if ok else error,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": clock().isoformat(timespec="seconds"),
    }
    return row, raw


def record(conn: sqlite3.Connection, head_dir: Path, row: dict, raw: str | None) -> None:
    """PRD 04가 연 트랜잭션 안에서 판단 행을 넣고 response.txt와 decision.json을 쓴다. missing 행도 이것으로 넣는다."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS decisions ({TABLE})")
    conn.execute(f"INSERT INTO decisions VALUES ({', '.join('?' * len(COLUMNS))})", [row[column] for column in COLUMNS])
    folder = _folder(head_dir, row["time"])
    folder.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        (folder / "response.txt").write_text(raw, encoding="utf-8")
    _write_json(folder / "decision.json", row)


def read(conn: sqlite3.Connection) -> list[dict]:
    """decisions 표의 행을 차례 순으로 읽는다."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS decisions ({TABLE})")
    rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM decisions ORDER BY time").fetchall()
    return [dict(zip(COLUMNS, row)) for row in rows]


def targets(row: dict) -> dict[str, Decimal] | None:
    """실행할 목표 {종목: 비중}. ok가 아니면 None이라 사고팔지 않는다."""
    if row["status"] != "ok":
        return None
    return {item["symbol"]: item["weight"] for item in json.loads(row["positions"], parse_float=Decimal)}


def _call(http: httpx.Client, credentials: Callable[[], tuple[str, str]], text: str, seconds: float) -> object | None:
    """Codex 호출 하나를 따로 돌리고 seconds초(벽시계)까지만 기다린다. 끝나지 않았으면 None, 끝났으면 응답이나 오류다."""
    outcome: list = []

    def attempt() -> None:
        try:
            outcome.append(codex.ask(http, *credentials(), PROMPT, text, model=MODEL))
        except Exception as error:  # 따로 돈 호출의 오류는 부른 쪽에서 상태로 바꾼다
            outcome.append(error)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(max(seconds, 0))
    return outcome[0] if outcome else None


def _pick(received: tuple[Decimal, datetime] | None, last: tuple[Decimal, datetime] | None, cutoff: datetime) -> tuple | None:
    if received is not None and received[1] <= cutoff:
        return received[0], received[1], True
    if last is not None:
        return last[0], last[1], False
    return None


def _minute(moment: datetime | str) -> str:
    moment = datetime.fromisoformat(moment) if isinstance(moment, str) else moment
    return moment.astimezone(KST).strftime("%Y-%m-%dT%H:%M+09:00")


def _folder(head_dir: Path, slot: str) -> Path:
    return head_dir / slot[:10] / slot[11:].replace(":", "")


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

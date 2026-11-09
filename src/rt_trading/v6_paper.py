"""v6 모의 계좌(v6 SPEC 4절, PRD 03): 장부, 받기, 판단 체결, 비용과 환전, 10분 평가, 기준과 손실 한도, 강제 매도.

장부 함수는 PRD 04가 잠금 안에서 연 SQLite 연결(트랜잭션 하나)에만 쓰고 네트워크를 부르지 않는다. 받기 함수는 받은 값만 돌려준다.
"""

import json
import sqlite3
from collections.abc import Callable
from datetime import date as Date
from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

import httpx

from rt_trading import toss, v6_head, v6_market
from rt_trading.toss import KST

START_CASH = 500_000
MAX_HOLDINGS = 5
KR_FEE = {"regular": Decimal("0.00015"), "pre": Decimal("0.00014"), "after": Decimal("0.00014")}  # NXT(장전·장후) 체결은 0.014%
KR_STOCK_TAX = Decimal("0.002")
ETF_RISK = Decimal("0.154")  # 국내 ETF 매도 차익에 보수적으로 넣는 비용. 실제 세금이 아니다
US_FEE = Decimal("0.001")
SEC_RATE, SEC_MIN = Decimal("0.0000206"), Decimal("0.01")
FX_COST_REGULAR, FX_COST_OTHER = Decimal("0.0005"), Decimal("0.005")  # 국내 거래일 09:00~15:30이면 앞의 값
CENT = Decimal("0.01")
BOOK_WAIT, BOOK_AGE = timedelta(seconds=40), timedelta(minutes=5)
STALE_AFTER = timedelta(minutes=20)
TABLES = {
    "cash": "currency TEXT PRIMARY KEY, amount TEXT NOT NULL",
    "positions": "symbol TEXT PRIMARY KEY, market TEXT NOT NULL, currency TEXT NOT NULL, quantity INTEGER NOT NULL, avg_price TEXT NOT NULL",
    "fills": "id INTEGER PRIMARY KEY, at TEXT NOT NULL, origin TEXT NOT NULL, turn TEXT NOT NULL, symbol TEXT NOT NULL, market TEXT NOT NULL,"
    " session TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL, levels TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,"
    " commission TEXT NOT NULL, tax TEXT NOT NULL, risk_cost TEXT NOT NULL, sec_fee TEXT NOT NULL, fx_usd TEXT NOT NULL, fx_krw TEXT NOT NULL,"
    " fx_rate TEXT NOT NULL, fx_cost_rate TEXT NOT NULL, realized TEXT NOT NULL, eligible INTEGER NOT NULL, tradable INTEGER NOT NULL,"
    " blocked INTEGER NOT NULL, krw_after TEXT NOT NULL, usd_after TEXT NOT NULL, holdings_after INTEGER NOT NULL",
    "valuations": "slot TEXT PRIMARY KEY, kind TEXT NOT NULL, at TEXT NOT NULL, equity INTEGER NOT NULL, krw TEXT NOT NULL, usd TEXT NOT NULL,"
    " rate TEXT NOT NULL, used TEXT NOT NULL, stale INTEGER NOT NULL, stale_detail TEXT NOT NULL",
    "baselines": "kind TEXT NOT NULL, key TEXT NOT NULL, equity INTEGER NOT NULL, source TEXT NOT NULL, PRIMARY KEY (kind, key)",
    "limits": "kind TEXT NOT NULL, key TEXT NOT NULL, at TEXT NOT NULL, equity INTEGER NOT NULL, PRIMARY KEY (kind, key)",
    "events": "at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL",
    "last_values": "key TEXT PRIMARY KEY, value TEXT NOT NULL, observed_at TEXT NOT NULL",
    "instruments": ", ".join(
        f"{column} {'INTEGER' if column in ('nxt_supported', 'nxt_suspended', 'krx_suspended', 'liquidation') else 'TEXT'} NOT NULL"
        for column in v6_market.INSTRUMENT_COLUMNS
    )
    + ", PRIMARY KEY (symbol)",
}
FILL_COLUMNS = (
    "at", "origin", "turn", "symbol", "market", "session", "side", "quantity", "levels", "amount", "currency", "commission", "tax", "risk_cost",
    "sec_fee", "fx_usd", "fx_krw", "fx_rate", "fx_cost_rate", "realized", "eligible", "tradable", "blocked", "krw_after", "usd_after", "holdings_after",
)


# 장부


def start(conn: sqlite3.Connection, day: str) -> None:
    """표를 만들고, 비어 있으면 원화 500,000·달러 0, 보유 없음, 시작일과 그 주의 기준 500,000으로 시작한다."""
    for name, columns in TABLES.items():
        conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({columns})")
    if conn.execute("SELECT COUNT(*) FROM cash").fetchone()[0]:
        return
    conn.executemany("INSERT INTO cash VALUES (?, ?)", [("KRW", str(START_CASH)), ("USD", "0")])
    conn.execute("INSERT INTO baselines VALUES ('daily', ?, ?, 'start')", (day, START_CASH))
    conn.execute("INSERT INTO baselines VALUES ('weekly', ?, ?, 'start')", (_monday(day), START_CASH))


def save_instruments(conn: sqlite3.Connection, infos: dict[str, dict]) -> None:
    """받은 종목 정보를 instruments 표의 마지막 값으로 쓴다."""
    columns = v6_market.INSTRUMENT_COLUMNS
    conn.executemany(f"INSERT OR REPLACE INTO instruments VALUES ({', '.join('?' * len(columns))})", [[info[column] for column in columns] for info in infos.values()])


def instruments(conn: sqlite3.Connection) -> dict[str, dict]:
    columns = v6_market.INSTRUMENT_COLUMNS
    return {row[0]: dict(zip(columns, row)) for row in conn.execute(f"SELECT {', '.join(columns)} FROM instruments")}


def remember(conn: sqlite3.Connection, received: dict) -> None:
    """받은 현재가({"prices": {종목: (값, 받은 시각)}})와 환율({"rate": (값, 받은 시각)})을 last_values에 쓴다."""
    items = list(received.get("prices", {}).items())
    if received.get("rate"):
        items.append(("USDKRW", received["rate"]))
    conn.executemany("INSERT OR REPLACE INTO last_values VALUES (?, ?, ?)", [(key, str(value), _iso(at)) for key, (value, at) in items])


def state(conn: sqlite3.Connection) -> dict:
    """PRD 02 account가 받는 꼴의 장부 상태: {"krw", "usd", "positions", "last"}."""
    cash = dict(conn.execute("SELECT currency, amount FROM cash"))
    positions = {
        symbol: {"market": market, "currency": currency, "quantity": quantity, "avg_price": Decimal(average)}
        for symbol, market, currency, quantity, average in conn.execute("SELECT symbol, market, currency, quantity, avg_price FROM positions")
    }
    last = {key: (Decimal(value), datetime.fromisoformat(at)) for key, value, at in conn.execute("SELECT key, value, observed_at FROM last_values")}
    return {"krw": int(cash["KRW"]), "usd": Decimal(cash["USD"]), "positions": positions, "last": last}


def record_valuation(conn: sqlite3.Connection, slot: str, kind: str, at: datetime, total: int, ledger: dict, used: dict, stale: list[str]) -> None:
    """valuations에 한 행을 쓴다. used는 종목·USDKRW마다 (값, 받은 시각, 이번에 받았는지)다."""
    rate = used.get("USDKRW")
    conn.execute(
        "INSERT INTO valuations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            slot,
            kind,
            _iso(at),
            total,
            str(ledger["krw"]),
            str(ledger["usd"]),
            str(rate[0]) if rate else "",
            json.dumps({key: [str(value), _iso(seen), received] for key, (value, seen, received) in used.items()}),
            int(bool(stale)),
            ",".join(stale),
        ),
    )


# 받기


def observe_prices(
    http: httpx.Client, token: toss.SharedToken, symbols: list[str], deadline: datetime, clock: Callable[[], datetime], events: list[dict]
) -> dict[str, tuple[Decimal, datetime]]:
    """보유 종목 현재가 {종목: (lastPrice, 받은 시각)}. 응답에 없는 종목은 받지 않은 것이다."""
    if not symbols:
        return {}
    result, observed = v6_market.request(http, token, "/api/v1/prices", {"symbols": ",".join(sorted(symbols))}, deadline, clock, events)
    try:
        return {item["symbol"]: (_decimal(item["lastPrice"]), observed) for item in result if item["symbol"] in symbols}
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise v6_market.V6Error("V6_TOSS_RESPONSE_INVALID", f"현재가 형식 오류({error!r})") from error


def observe_rate(http: httpx.Client, token: toss.SharedToken, deadline: datetime, clock: Callable[[], datetime], events: list[dict]) -> tuple[Decimal, datetime]:
    """원·달러 midRate와 받은 시각. validFrom 등 응답 안의 시각은 보지 않는다."""
    result, observed = v6_market.request(http, token, "/api/v1/exchange-rate", {"baseCurrency": "USD", "quoteCurrency": "KRW"}, deadline, clock, events)
    try:
        return _decimal(result["midRate"]), observed
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise v6_market.V6Error("V6_TOSS_RESPONSE_INVALID", f"환율 형식 오류({error!r})") from error


def observe_books(
    http: httpx.Client,
    token: toss.SharedToken,
    symbols: list[str],
    after: datetime,
    deadline: datetime,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    events: list[dict],
) -> dict[str, dict]:
    """종목마다 after 뒤에 호가를 요청한다. {종목: {"requested_at", "observed_at", "timestamp", "asks"(낮은 값부터), "bids"(높은 값부터)}}.

    받지 못했거나 형식이 틀린 종목은 넣지 않는다.
    """
    books = {}
    for symbol in sorted(symbols):
        if clock() < after:
            sleep((after - clock()).total_seconds())
        requested = clock()
        try:
            result, observed = v6_market.request(http, token, "/api/v1/orderbook", {"symbol": symbol}, deadline, clock, events)
            books[symbol] = {
                "requested_at": requested,
                "observed_at": observed,
                "timestamp": None if result.get("timestamp") is None else datetime.fromisoformat(result["timestamp"]),
                "asks": sorted(_levels(result["asks"])),
                "bids": sorted(_levels(result["bids"]), reverse=True),
            }
        except (v6_market.V6Error, KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
            continue
    return books


def usable(book: dict | None, at: datetime) -> bool:
    """체결 시각 at에 쓸 수 있는 호가: at 뒤에 요청해 at + 40초까지 받았고, 데이터 시각이 at − 5분 이상이다."""
    return (
        book is not None
        and book["requested_at"] >= at
        and book["observed_at"] <= at + BOOK_WAIT
        and book["timestamp"] is not None
        and book["timestamp"] >= at - BOOK_AGE
    )


# 판단 체결


def rebalance(
    conn: sqlite3.Connection,
    turn: str,
    at: datetime,
    open_at: dict,
    targets: dict[str, Decimal],
    a_c: int,
    rate_c: Decimal | None,
    books: dict[str, dict],
    blocked: bool,
    calendar: dict,
) -> None:
    """ok 판단의 목표로 판 뒤 산다(SPEC 4.1). rate_c는 그 차례에 C까지 받은 환율이고, 없으면 미국 목표 수량을 계산하지 않는다."""
    infos, ledger = instruments(conn), state(conn)
    live = {symbol for symbol in set(targets) | set(ledger["positions"]) if _tradable(infos, symbol, open_at) and usable(books.get(symbol), at)}
    wanted = {symbol: 0 for symbol in ledger["positions"] if symbol not in targets}
    for symbol, weight in targets.items():
        if symbol not in live or not books[symbol]["asks"]:
            continue
        if infos[symbol]["market"] == "US" and rate_c is None:
            continue
        ask = books[symbol]["asks"][0][0] * (1 if infos[symbol]["market"] == "KR" else rate_c)
        wanted[symbol] = int((a_c * weight / ask).to_integral_value(rounding=ROUND_FLOOR))
    for symbol in sorted(ledger["positions"]):
        held = ledger["positions"][symbol]["quantity"]
        if symbol in live and symbol in wanted and wanted[symbol] < held:
            _sell(conn, turn, at, "decision", symbol, held - wanted[symbol], books[symbol], infos[symbol], open_at)
    if not blocked:
        for symbol in sorted(targets, key=lambda item: (-targets[item], item)):
            if symbol in live and symbol in wanted:
                _buy(conn, turn, at, symbol, wanted[symbol], books[symbol], infos[symbol], open_at, rate_c, calendar)
    prices, rate = _used(conn, _slot(v6_head.times(turn)[1]))
    check(conn, _equity_after(conn, prices, rate), at)


def _sell(conn: sqlite3.Connection, turn: str, at: datetime, origin: str, symbol: str, quantity: int, book: dict, info: dict, open_at: dict) -> None:
    ledger = state(conn)
    position = ledger["positions"][symbol]
    available = min(quantity, sum(volume for _, volume in book["bids"]))
    if available <= 0:
        return
    if info["market"] == "US" and ledger["usd"] + _proceeds(_walk(book["bids"], available), info, position, open_at)[0] < 0:
        return  # 받는 돈은 수량이 늘수록 커지므로, 다 팔아도 달러 현금이 음수면 이번에는 팔지 않는다
    levels = _walk(book["bids"], available)
    net, costs = _proceeds(levels, info, position, open_at)
    amount = _amount(levels)
    if info["market"] == "KR":
        ledger["krw"] += int(net)
    else:
        ledger["usd"] += net
    realized = amount - available * position["avg_price"] - sum(costs.values())
    left = position["quantity"] - available
    if left:
        conn.execute("UPDATE positions SET quantity = ? WHERE symbol = ?", (left, symbol))
    else:
        conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
    _write_fill(conn, turn, at, origin, symbol, info, open_at, "SELL", levels, amount, costs, {}, realized, 0, ledger)


def _buy(
    conn: sqlite3.Connection,
    turn: str,
    at: datetime,
    symbol: str,
    wanted: int,
    book: dict,
    info: dict,
    open_at: dict,
    rate_c: Decimal | None,
    calendar: dict,
) -> None:
    ledger = state(conn)
    held = ledger["positions"].get(symbol, {}).get("quantity", 0)
    if wanted <= held or (held == 0 and len(ledger["positions"]) >= MAX_HOLDINGS):
        return
    fx_cost = _fx_cost(at, calendar)
    count = _largest(min(wanted - held, sum(volume for _, volume in book["asks"])), lambda count: _pay(_walk(book["asks"], count), info, open_at, ledger, rate_c, fx_cost) is not None)
    if count <= 0:
        return
    levels = _walk(book["asks"], count)
    amount = _amount(levels)
    commission, fx = _pay(levels, info, open_at, ledger, rate_c, fx_cost)
    if info["market"] == "KR":
        ledger["krw"] -= int(amount + commission)
    else:
        ledger["usd"] += fx.get("usd", Decimal(0)) - amount - commission
        ledger["krw"] -= fx.get("krw", 0)
    if held:
        average = (held * ledger["positions"][symbol]["avg_price"] + amount) / (held + count)
        conn.execute("UPDATE positions SET quantity = ?, avg_price = ? WHERE symbol = ?", (held + count, str(average), symbol))
    else:
        conn.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?)", (symbol, info["market"], info["currency"], count, str(amount / count)))
        conn.execute("INSERT OR IGNORE INTO last_values VALUES (?, ?, ?)", (symbol, str(amount / count), _iso(book["observed_at"])))
    costs = {"commission": commission, "tax": Decimal(0), "risk_cost": Decimal(0), "sec_fee": Decimal(0)}
    blocked = int(bool(blocked_reasons(conn, at)))  # 장부 규칙 검사(PRD 04)가 볼 그때의 매수 막힘
    _write_fill(conn, turn, at, "decision", symbol, info, open_at, "BUY", levels, amount, costs, fx, Decimal(0), blocked, ledger)


def _proceeds(levels: list[tuple[Decimal, int]], info: dict, position: dict, open_at: dict) -> tuple[Decimal, dict[str, Decimal]]:
    """매도 (받는 돈, 비용)."""
    amount, count = _amount(levels), sum(volume for _, volume in levels)
    costs = {"commission": Decimal(0), "tax": Decimal(0), "risk_cost": Decimal(0), "sec_fee": Decimal(0)}
    if info["market"] == "KR":
        costs["commission"] = _floor(amount * KR_FEE[open_at["KR"]])
        if info["security_type"] == "STOCK":
            costs["tax"] = _floor(amount * KR_STOCK_TAX)
        else:
            costs["risk_cost"] = _floor(max(Decimal(0), amount - count * position["avg_price"]) * ETF_RISK)
    else:
        costs["commission"] = (amount * US_FEE).quantize(CENT, rounding=ROUND_FLOOR)
        costs["sec_fee"] = max((amount * SEC_RATE).quantize(CENT, rounding=ROUND_HALF_UP), SEC_MIN)
    return amount - sum(costs.values()), costs


def _pay(
    levels: list[tuple[Decimal, int]], info: dict, open_at: dict, ledger: dict, rate_c: Decimal | None, fx_cost: Decimal
) -> tuple[Decimal, dict] | None:
    """매수 (수수료, 환전 {"usd", "krw", "rate", "cost_rate"}). 낼 수 없으면 None이다."""
    amount = _amount(levels)
    if info["market"] == "KR":
        commission = _floor(amount * KR_FEE[open_at["KR"]])
        return (commission, {}) if amount + commission <= ledger["krw"] else None
    commission = (amount * US_FEE).quantize(CENT, rounding=ROUND_FLOOR)
    short = max(Decimal(0), amount + commission - ledger["usd"])
    if not short:
        return commission, {}
    won = int((short * rate_c * (1 + fx_cost)).to_integral_value(rounding=ROUND_CEILING))
    return (commission, {"usd": short, "krw": won, "rate": rate_c, "cost_rate": fx_cost}) if won <= ledger["krw"] else None


def _write_fill(
    conn: sqlite3.Connection,
    turn: str,
    at: datetime,
    origin: str,
    symbol: str,
    info: dict,
    open_at: dict,
    side: str,
    levels: list[tuple[Decimal, int]],
    amount: Decimal,
    costs: dict[str, Decimal],
    fx: dict,
    realized: Decimal,
    blocked: int,
    ledger: dict,
) -> None:
    conn.execute("UPDATE cash SET amount = ? WHERE currency = 'KRW'", (str(ledger["krw"]),))
    conn.execute("UPDATE cash SET amount = ? WHERE currency = 'USD'", (str(ledger["usd"]),))
    holdings = conn.execute("SELECT COUNT(*) FROM positions WHERE quantity > 0").fetchone()[0]
    values = {
        "at": _iso(at),
        "origin": origin,
        "turn": turn,
        "symbol": symbol,
        "market": info["market"],
        "session": open_at[info["market"]] or "",
        "side": side,
        "quantity": sum(volume for _, volume in levels),
        "levels": json.dumps([[str(price), volume] for price, volume in levels]),
        "amount": _text(amount, info["market"]),
        "currency": info["currency"],
        **{key: _text(value, info["market"]) for key, value in costs.items()},
        "fx_usd": str(fx.get("usd", "0")),
        "fx_krw": str(fx.get("krw", "0")),
        "fx_rate": str(fx.get("rate", "")),
        "fx_cost_rate": str(fx.get("cost_rate", "")),
        "realized": _text(realized, info["market"]),
        "eligible": int(v6_market.eligible(info)),
        "tradable": int(v6_market.tradable(info, open_at)),
        "blocked": blocked,
        "krw_after": str(ledger["krw"]),
        "usd_after": str(ledger["usd"]),
        "holdings_after": holdings,
    }
    conn.execute(f"INSERT INTO fills ({', '.join(FILL_COLUMNS)}) VALUES ({', '.join('?' * len(FILL_COLUMNS))})", [values[column] for column in FILL_COLUMNS])


# 10분 평가


def value_slot(conn: sqlite3.Connection, slot: str, at: datetime, received: dict, calendar: dict) -> int:
    """10분 평가 차례 slot의 평가를 기록하고 자산을 돌려준다. remember 뒤에 부른다. 받지 못한 값은 last_values를 쓴다.

    거래할 수 있는 보유의 현재가나 필요한 환율을 마지막으로 받은 시각이 at보다 20분 넘게 이르면 stale이다. 응답 안의 시각은 보지 않는다.
    """
    ledger, infos = state(conn), instruments(conn)
    needs_rate = bool(ledger["usd"]) or any(position["market"] == "US" for position in ledger["positions"].values())
    used = {symbol: (*ledger["last"][symbol], symbol in received.get("prices", {})) for symbol in ledger["positions"]}
    rate = ledger["last"].get("USDKRW")
    if rate:
        used["USDKRW"] = (*rate, bool(received.get("rate")))
    total = v6_head.equity(ledger["krw"], ledger["usd"], ledger["positions"], {symbol: used[symbol][0] for symbol in ledger["positions"]}, rate[0] if rate else None)
    open_at = v6_market.sessions(calendar, at)
    choices = [open_at] if open_at else [{"KR": session, "US": "regular"} for session in ("pre", "regular", "after")]  # 세션을 모르면 열린 것으로 본다
    stale = [
        symbol
        for symbol in sorted(ledger["positions"])
        if any(_tradable(infos, symbol, choice) for choice in choices) and used[symbol][1] < at - STALE_AFTER
    ]
    if needs_rate and (rate is None or rate[1] < at - STALE_AFTER):
        stale.append("USDKRW")
    record_valuation(conn, slot, "watch", at, total, ledger, used, stale)
    return total


# 기준, 손실 한도와 매수 막기


def bases(conn: sqlite3.Connection, at: datetime) -> None:
    """at의 하루 기준(그날)과 한 주 기준(그 주 월요일)을 그 날짜 00:00 차례 평가로 넣는다.

    시작일 기준은 start가 넣는다. 그 날짜 00:10이 지나도 00:00 평가가 없으면 앞의 기준을 이어 쓰고(inherited) risk_data_missing을 기록한다.
    """
    day = at.astimezone(KST).date()
    missing = []
    for kind, key in (("daily", day), ("weekly", Date.fromisoformat(_monday(day.isoformat())))):
        if _base(conn, kind, key.isoformat()) is not None:
            continue
        midnight = conn.execute("SELECT equity FROM valuations WHERE slot = ? AND kind = 'watch'", (f"{key} 00:00",)).fetchone()
        if midnight:
            conn.execute("INSERT INTO baselines VALUES (?, ?, ?, 'valuation')", (kind, key.isoformat(), midnight[0]))
        elif at >= datetime.combine(key, time(0, 10), KST):
            previous = conn.execute("SELECT equity FROM baselines WHERE kind = ? AND key < ? ORDER BY key DESC LIMIT 1", (kind, key.isoformat())).fetchone()
            conn.execute("INSERT INTO baselines VALUES (?, ?, ?, 'inherited')", (kind, key.isoformat(), previous[0]))
            missing.append(f"{key} {'하루' if kind == 'daily' else '한 주'} 기준")
    if missing:
        conn.execute("INSERT INTO events VALUES (?, 'risk_data_missing', ?)", (_iso(at), ", ".join(missing)))


def check(conn: sqlite3.Connection, total: int, at: datetime) -> None:
    """자산 total로 하루·한 주·누적 손실 한도를 본다. 그 기간에 처음 발동한 한도를 쓰고, 강제 매도 중이 아니고 보유가 있으면 강제 매도를 시작한다."""
    day = at.astimezone(KST).date()
    daily, weekly = _base(conn, "daily", day.isoformat()), _base(conn, "weekly", _monday(day.isoformat()))
    hits = []
    if daily is not None and total * 100 <= daily * 98:
        hits.append(("daily", day.isoformat()))
    if weekly is not None and total * 100 <= weekly * 95:
        hits.append(("weekly", _monday(day.isoformat())))
    if total * 100 <= START_CASH * 80:
        hits.append(("cumulative", "all"))
    fresh = [hit for hit in hits if not conn.execute("SELECT 1 FROM limits WHERE kind = ? AND key = ?", hit).fetchone()]
    conn.executemany("INSERT INTO limits VALUES (?, ?, ?, ?)", [(*hit, _iso(at), total) for hit in fresh])
    if fresh and not _forced(conn) and conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]:
        conn.execute("INSERT INTO events VALUES (?, 'forced_start', ?)", (_iso(at), ", ".join(kind for kind, _ in fresh)))


def blocked_reasons(conn: sqlite3.Connection, at: datetime) -> list[str]:
    """at에 매수가 막힌 이유. 하나라도 있으면 사지 않는다."""
    day = at.astimezone(KST).date().isoformat()
    monday = _monday(day)
    reasons = []
    if conn.execute("SELECT 1 FROM limits WHERE kind = 'daily' AND key = ?", (day,)).fetchone():
        reasons.append("daily_limit")
    if conn.execute("SELECT 1 FROM limits WHERE kind = 'weekly' AND key = ?", (monday,)).fetchone():
        reasons.append("weekly_limit")
    if conn.execute("SELECT 1 FROM limits WHERE kind = 'cumulative'").fetchone():
        reasons.append("cumulative_limit")
    inherited = conn.execute(
        "SELECT 1 FROM baselines WHERE source = 'inherited' AND ((kind = 'daily' AND key = ?) OR (kind = 'weekly' AND key = ?))", (day, monday)
    ).fetchone()
    if inherited:
        reasons.append("risk_data_missing")
    if _forced(conn):
        reasons.append("forced_sale")
    return reasons


# 강제 매도


def force(conn: sqlite3.Connection, turn: str, at: datetime, open_at: dict, books: dict[str, dict]) -> None:
    """강제 매도 중이면 10분 평가 차례 turn(시작 시각 at)에 거래할 수 있는 보유를 모두 판다. 다 팔면 forced_end를 쓰고, 판 뒤 한도를 본다."""
    if not _forced(conn):
        return
    infos, ledger = instruments(conn), state(conn)
    for symbol in sorted(ledger["positions"]):
        if _tradable(infos, symbol, open_at) and usable(books.get(symbol), at):
            _sell(conn, turn, at, "forced", symbol, ledger["positions"][symbol]["quantity"], books[symbol], infos[symbol], open_at)
    if not conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]:
        conn.execute("INSERT INTO events VALUES (?, 'forced_end', '')", (_iso(at),))
    prices, rate = _used(conn, turn)
    check(conn, _equity_after(conn, prices, rate), at)


# 도움 함수


def _tradable(infos: dict[str, dict], symbol: str, open_at: dict) -> bool:
    return symbol in infos and v6_market.tradable(infos[symbol], open_at)


def _forced(conn: sqlite3.Connection) -> bool:
    """마지막 forced_start 뒤에 forced_end가 없으면 강제 매도 중이다."""
    last = conn.execute("SELECT kind FROM events WHERE kind IN ('forced_start', 'forced_end') ORDER BY rowid DESC LIMIT 1").fetchone()
    return bool(last) and last[0] == "forced_start"


def _base(conn: sqlite3.Connection, kind: str, key: str) -> int | None:
    row = conn.execute("SELECT equity FROM baselines WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    return row[0] if row else None


def _used(conn: sqlite3.Connection, slot: str) -> tuple[dict[str, Decimal], Decimal | None]:
    """평가 slot에 쓴 (현재가, 환율)."""
    row = conn.execute("SELECT used FROM valuations WHERE slot = ?", (slot,)).fetchone()
    used = json.loads(row[0]) if row else {}
    rate = used.pop("USDKRW", None)
    return {key: Decimal(value) for key, (value, _, _) in used.items()}, Decimal(rate[0]) if rate else None


def _equity_after(conn: sqlite3.Connection, prices: dict[str, Decimal], rate: Decimal | None) -> int:
    """체결 뒤 자산. 평가에 값이 없던 종목은 평균 단가로 친다."""
    ledger = state(conn)
    marks = {symbol: prices.get(symbol, position["avg_price"]) for symbol, position in ledger["positions"].items()}
    return v6_head.equity(ledger["krw"], ledger["usd"], ledger["positions"], marks, rate)


def _largest(limit: int, affordable: Callable[[int], bool]) -> int:
    """0 이상 limit 이하에서 affordable이 참인 가장 큰 정수. affordable은 수량이 늘면 참에서 거짓으로만 바뀐다."""
    low, high = 0, limit
    while low < high:
        middle = (low + high + 1) // 2
        if affordable(middle):
            low = middle
        else:
            high = middle - 1
    return low


def _walk(levels: list[tuple[Decimal, int]], quantity: int) -> list[tuple[Decimal, int]]:
    """호가를 앞에서부터 quantity주까지 채운 (가격, 수량) 목록."""
    filled = []
    for price, volume in levels:
        if quantity <= 0:
            break
        take = min(volume, quantity)
        filled.append((price, take))
        quantity -= take
    return filled


def _amount(levels: list[tuple[Decimal, int]]) -> Decimal:
    return sum((price * volume for price, volume in levels), Decimal(0))


def _levels(items: list[dict]) -> list[tuple[Decimal, int]]:
    levels = [(_decimal(item["price"]), int(_decimal(item["volume"]).to_integral_value(rounding=ROUND_FLOOR))) for item in items]
    return [(price, volume) for price, volume in levels if volume > 0]


def _fx_cost(at: datetime, calendar: dict) -> Decimal:
    """환전 비용률: 국내 거래일 09:00~15:30은 0.05%, 그 밖(장 운영 정보가 없는 날 포함)은 0.5%."""
    local = at.astimezone(KST)
    regular = v6_market.trading_day(calendar, local.date().isoformat()) and time(9, 0) <= local.time() < time(15, 30)
    return FX_COST_REGULAR if regular else FX_COST_OTHER


def _floor(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_FLOOR)


def _decimal(text: object) -> Decimal:
    if not isinstance(text, str):
        raise TypeError(f"수가 글자가 아니다({type(text).__name__})")
    return Decimal(text)


def _text(value: Decimal, market: str) -> str:
    return str(int(value)) if market == "KR" else str(value)


def _slot(moment: datetime) -> str:
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _monday(day: str) -> str:
    value = Date.fromisoformat(day)
    return (value - timedelta(days=value.weekday())).isoformat()


def _iso(moment: datetime) -> str:
    return moment.astimezone(KST).isoformat(timespec="seconds")

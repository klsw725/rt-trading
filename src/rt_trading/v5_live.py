"""v5 실계좌 실행기와 v5-stop·v5-resume·v5-check(v5 SPEC 4절, PRD 03)."""

import fcntl
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import NamedTuple

import httpx

from rt_trading import engine, sessions, toss, toss_account, v3_daily, v4_daily, v4_head, v5_book, v5_target
from rt_trading.market_data import Bar
from rt_trading.toss import KST

SYMBOLS = ("069500", "114800")
ENGINE_RATE = Decimal("0.000188")
UNKNOWN_LIMIT = timedelta(minutes=8)
RESEND = timedelta(seconds=10)
POLL = 5  # 차례 사이에 정지를 보는 초
BAR_WAIT = timedelta(seconds=40)
LOCK_WAIT = 30  # v5-resume·v5-check --order가 주문 잠금을 기다리는 초
CHECK_WAIT = 60  # 점검 주문이 기대한 상태가 되기를 기다리는 초


class Paths(NamedTuple):
    daily: v4_daily.Paths  # v5 판단·정산 파일(data/v5/)
    live: Path  # data/v5/live.sqlite3
    config: Path  # data/v5/config.json
    lock: Path  # 실행기 잠금
    order_lock: Path  # 주문 잠금
    period: Path
    holidays: Path


class Turn(NamedTuple):
    name: str  # start, pre_open, intraday, closing, end
    at: datetime
    bar: str | None  # 차례 봉 시각


class Stopped(Exception):
    """안전 정지를 기록한 뒤 그 차례를 그만둔다."""


def turns(day: str) -> list[Turn]:
    """장 일정(개장 o, 마감 c)으로 정한 그날의 실행 차례(PRD 03)."""
    session = sessions.session(day, sessions.exceptions())
    opening, close = _at(day, session.open), _at(day, session.close)
    found = [Turn("start", opening - timedelta(minutes=15), None), Turn("pre_open", opening - timedelta(minutes=5), None)]
    for bar in session.bars:
        at = _at(day, bar)
        if opening + timedelta(minutes=1) <= at <= close - timedelta(minutes=11):
            found.append(Turn("intraday", at + timedelta(seconds=10), bar))
    last = close - timedelta(minutes=10)
    found += [Turn("closing", last + timedelta(seconds=10), f"{last:%H:%M}"), Turn("end", close + timedelta(minutes=10), None)]
    return found


def close_equity(paths: Paths, db: sqlite3.Connection, ledger: dict, day: str) -> int | None:
    """마감 봇 자산: day 마감(c + 10분)까지의 장부와 정산한 day 종가. day가 정산되지 않았으면 None이다(PRD 03)."""
    if day not in v3_daily.settled_days(paths.daily.paper):
        return None
    session = sessions.session(day, sessions.exceptions())
    cash, held = v5_book.balance(db, ledger["id"], (_at(day, session.close) + timedelta(minutes=10)).isoformat(timespec="seconds"))
    closes = {}
    with closing(sqlite3.connect(f"{paths.daily.copy.resolve().as_uri()}?mode=ro", uri=True)) as market:
        for symbol in held:
            row = market.execute("SELECT close FROM candles_1d WHERE symbol = ? AND date = ?", (symbol, day)).fetchone()
            if row is None:
                return None
            closes[symbol] = row[0]
    return cash + sum(count * closes[symbol] for symbol, count in held.items())


def quantity(cash: int, price: int, rate: Decimal, max_shares: int | None) -> int:
    """수량 × 가격 + 수수료(원 미만 올림)가 cash 이하인 최대 정수 주. max_shares보다 크지 않다."""
    count = cash // price
    while count and count * price + _fee(count * price, rate) > cash:
        count -= 1
    return min(count, max_shares) if max_shares is not None else count


def read_config(path: Path) -> dict | None:
    """data/v5/config.json. 없거나 틀리면 None이다."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        valid = (
            type(config["account_seq"]) is int
            and type(config["budget"]) is int
            and config["budget"] > 0
            and (config["max_shares"] is None or (type(config["max_shares"]) is int and config["max_shares"] > 0))
            and type(config["orders"]) is bool
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return config if valid else None


@contextmanager
def file_lock(path: Path, wait: float, sleep: Callable[[float], None]):
    """잠금 파일을 쥔다. wait초 안에 쥐지 못하면 거짓을 내준다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as file:
        waited = 0.0
        while True:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited >= wait:
                    yield False
                    return
                sleep(1)
                waited += 1
        try:
            yield True
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


class Live:
    """그날 하루를 도는 실행기. 토스, 시계, 기다리기, 목표 계산, 봉 받기를 바꿔 끼울 수 있다(acceptance)."""

    def __init__(
        self,
        paths: Paths,
        http: httpx.Client,
        token: toss_account.Token,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
        target: Callable[..., v5_target.Target] = v5_target.target,
        bars: Callable[[str], dict[str, list[Bar]]] | None = None,
    ) -> None:
        self.paths, self.http, self.token, self.clock, self.sleep, self.target = paths, http, token, clock, sleep, target
        self.bars = bars or self._fetch_bars
        self.next_try: dict[str, datetime] = {}
        self.handled_stops: set[int] = set()
        self.prices: dict[str, int] = {}
        self.real: tuple[dict, bool] | None = None
        self.cached: tuple[tuple, object] | None = None

    # 하루

    def run(self) -> int:
        today = self.clock().date().isoformat()
        try:
            period = v5_target.load_period(self.paths.period, self.paths.holidays, today)
        except v3_daily.DailyError as error:
            print(error)
            return 1
        if not v3_daily.is_trading_day(today, period.holidays):
            print("휴장일")
            return 0
        with file_lock(self.paths.lock, 0, self.sleep) as held:
            if not held:
                print("V5_LIVE_RUNNING: 실행기가 이미 돌고 있다")
                return 1
            return self._day(today, period)

    def _day(self, today: str, period: v3_daily.Period) -> int:
        plan, self.period, self.today = turns(today), period, today
        with v5_book.connect(self.paths.live) as db:
            record = v5_book.day(db, today)
        if record and record["ended"]:
            return 0
        if record is None:
            self._turn(plan[0])
        index = 1
        while True:
            now = self.clock()
            if now >= plan[-1].at:
                self._turn(plan[-1])
                return 0
            self._between()
            due = [i for i in range(index, len(plan) - 1) if plan[i].at <= now]
            if due:
                index = due[-1] + 1
                self._turn(plan[due[-1]])
                continue
            self.sleep(min(max((plan[index].at - now).total_seconds(), 0.5), POLL))

    def _turn(self, turn: Turn) -> None:
        with file_lock(self.paths.order_lock, 0, self.sleep) as held:
            if not held:
                self._record(turn, None, "주문 잠금을 다른 쪽이 쥐고 있어 건너뜀")
                return
            try:
                {"start": self._start, "end": self._end}.get(turn.name, self._trade)(turn)
            except Stopped:
                self._record(turn, None, "안전 정지")
            except toss.TossError as error:
                if toss_account.classify(error) == "stop" or error.code in toss_account.TOKEN_CODES:  # 조회·취소의 토큰 오류는 토큰을 바꿔 다시 보낸 뒤에도 이어진 것이다
                    self._stop("safety", f"토스 응답 오류({error.code} {error.remote})")
                self._record(turn, None, f"토스 오류로 이 차례를 건너뜀({error.code})")

    def _between(self) -> None:
        """차례 사이: 결과 모르는 낼 주문을 처리하고, 새 정지가 있으면 봇 주문을 취소한다."""
        with v5_book.connect(self.paths.live) as db:
            stops = v5_book.active_stops(db)
            unknown = v5_book.orders(db, "status IN ('sending', 'unknown')")
        fresh = [stop for stop in stops if stop["id"] not in self.handled_stops]
        if not unknown and not fresh:
            return
        with file_lock(self.paths.order_lock, 0, self.sleep) as held:
            if held:
                try:
                    self._resolve_unknown()
                    if fresh:
                        self._cancel_all()
                        self.handled_stops |= {stop["id"] for stop in fresh}
                except Stopped:
                    pass

    # 차례

    def _start(self, turn: Turn) -> None:
        now, config = self.clock(), read_config(self.paths.config)
        with v5_book.connect(self.paths.live) as db:
            if v5_book.day(db, self.today):
                return
            live = v5_book.ledger(db, "live")
        mode, account = ("live", live["account_seq"]) if live else ("dry", None)
        problem = None
        if config is None:
            problem = "설정 파일이 없거나 틀리다"
        elif config["orders"]:
            mode, account, problem = self._live_start(config, live)
        else:
            account = config["account_seq"]
            if live and self._live_busy(live):
                mode, account, problem = "live", live["account_seq"], "실장부에 보유나 끝나지 않은 주문이 있어 연습 운용으로 돌아가지 못한다"
        with v5_book.connect(self.paths.live) as db:
            if mode == "dry" and v5_book.ledger(db, "dry") is None:
                v5_book.create_ledger(db, "dry", account, config["budget"] if config else 0, now)
            db.execute("INSERT INTO days (date, mode, account_seq) VALUES (?, ?, ?)", (self.today, mode, account))
        if problem:
            self._stop("safety", problem)
            raise Stopped
        self._fetch_day_values()
        if mode == "live":
            self._scan_unknown_orders()
            with v5_book.connect(self.paths.live) as db:
                unresolved = v5_book.orders(db, "status IN ('sending', 'unknown', 'open') AND kind != 'dry'")
            if not unresolved:
                self._compare_holdings()
        self._record(turn, None, f"시작 점검({'실주문' if mode == 'live' else '연습 운용'})")

    def _live_start(self, config: dict, live: dict | None) -> tuple[str, int | None, str | None]:
        account = config["account_seq"]
        if live and live["account_seq"] != account:
            return "live", live["account_seq"], "설정의 account_seq가 실장부의 계좌와 다르다"
        found = toss_account.accounts(self.http, self.token)
        if not any(item["seq"] == account and item["type"] == "BROKERAGE" for item in found):
            return ("live", live["account_seq"], "설정의 account_seq가 종합매매 계좌가 아니다") if live else ("dry", account, "설정의 account_seq가 종합매매 계좌가 아니다")
        if live:
            return "live", account, None
        with v5_book.connect(self.paths.live) as db:
            busy = v5_book.active_stops(db) or v5_book.orders(db, "status IN ('sending', 'unknown', 'open') AND kind != 'dry'")
        if busy:
            return "dry", account, "정지 중이거나 끝나지 않은 봇 주문·결과를 모르는 낼 주문이 있어 실장부를 만들지 않는다"
        held = toss_account.holdings(self.http, self.token, account)
        pending = [order for symbol in SYMBOLS for order in toss_account.open_orders(self.http, self.token, account, symbol)]
        power = toss_account.buying_power(self.http, self.token, account)
        if any(held.values()) or pending or power < config["budget"]:
            return "dry", account, "실장부를 만들 조건(두 종목 0주, 대기 주문 없음, 매수 가능 금액 ≥ 예산)이 맞지 않는다"
        with v5_book.connect(self.paths.live) as db:
            v5_book.create_ledger(db, "live", account, config["budget"], self.clock())
        return "live", account, None

    def _live_busy(self, live: dict) -> bool:
        with v5_book.connect(self.paths.live) as db:
            _, held = v5_book.balance(db, live["id"])
            unknown = v5_book.orders(db, "status IN ('sending', 'unknown', 'open') AND kind = 'live'")
        pending = [order for symbol in SYMBOLS for order in toss_account.open_orders(self.http, self.token, live["account_seq"], symbol)]
        return bool(held or unknown or pending)

    def _trade(self, turn: Turn) -> None:
        record = self._day_record()
        self._resolve_unknown()
        if self._unknown():
            self._record(turn, None, "결과를 모르는 낼 주문이 있어 주문하지 않음")
            return
        open_orders = self._refresh(turn)
        if record["mode"] == "live" and not open_orders:
            self._compare_holdings()
        self._fetch_day_values()
        record = self._day_record()
        bars = {} if turn.name == "pre_open" else self._wait_bars(turn)
        self._update_prices(turn, bars)
        blocked = self._limits(record)
        stops = self._stops()
        result = None
        if open_orders:
            action = "끝나지 않은 봇 주문이 있어 새 주문을 내지 않음"
        elif stops and not self._sell_stop(stops):
            action = "정지 중"
        elif stops or blocked == "sell":
            action = self._sell_all(turn, "정지에 따른 다 팔기" if stops else "실계좌 손실 한도 매도")
        else:
            result = self._target(turn, bars, record)
            action = self._follow(turn, result, blocked) if result.reason is None else f"목표를 정하지 않음({result.reason})"
        self._record(turn, result, action)

    def _end(self, turn: Turn) -> None:
        self._resolve_unknown()
        with v5_book.connect(self.paths.live) as db:
            today = v5_book.orders(db, "date = ? AND order_id IS NOT NULL AND kind != 'dry'", (self.today,))
        for order in today:
            self._apply(order, turn.at)  # 그날 마감 봇 자산(c + 10분까지의 장부)에 들어가게 끝 차례 시각으로 쓴다
        with v5_book.connect(self.paths.live) as db:
            db.execute("UPDATE days SET ended = 1 WHERE date = ?", (self.today,))
        self._record(turn, None, "끝 차례: 그날 봇 주문을 다시 조회함")

    # 주문 상태와 장부

    def _refresh(self, turn: Turn) -> list[dict]:
        """끝나지 않은 봇 주문을 조회해 반영하고, 남은 주문 중 앞 차례 것은 취소를 요청한다. 남은 끝나지 않은 주문을 돌려준다."""
        with v5_book.connect(self.paths.live) as db:
            pending = v5_book.orders(db, "status = 'open'")
        remaining = [order for order in pending if not self._apply(order)]
        for order in remaining:
            if turn.name in ("intraday", "closing") and order["turn"] != turn.bar:  # 앞 차례(개장 전 포함)에 낸 주문. 종가 차례 주문은 그 뒤 차례가 없다
                self._cancel(order)
        return remaining

    def _apply(self, order: dict, at: datetime | None = None) -> bool:
        """상세 조회로 체결을 반영한다(at을 주면 그 시각으로 쓴다). 끝났으면 참이다."""
        snapshot = toss_account.order(self.http, self.token, order["account_seq"], order["order_id"])
        if snapshot["status"] in ("PENDING_REPLACE", "REPLACED", "REPLACE_REJECTED") or snapshot["status"] not in (
            "PENDING", "PENDING_CANCEL", "PARTIAL_FILLED", "FILLED", "CANCELED", "REJECTED", "CANCEL_REJECTED",
        ):
            self._stop("safety", f"정정 상태나 모르는 상태({snapshot['status']})")
            raise Stopped
        with v5_book.connect(self.paths.live) as db:
            current = v5_book.orders(db, "client_order_id = ?", (order["client_order_id"],))[0]
            if _mismatch(current, snapshot):
                problem = "주문 상세의 종목·방향·주문 수량이 낸 주문과 다르거나 체결 수량이 앞서 반영한 수량보다 적거나 주문 수량보다 많다"
            elif not v5_book.apply_fill(db, current, snapshot, at or self.clock()):
                problem = "체결 수량이 있는데 체결 금액과 평균 체결가가 모두 없다"
            else:
                problem = None
                final = toss_account.is_final(snapshot["status"])
                if final:
                    v5_book.update_order(db, order["client_order_id"], status="final")
                if snapshot["filled_quantity"] > current["applied_quantity"] and current["ledger_id"]:
                    v5_book.set_latch(db, current["ledger_id"], "rejections", "", 0)
                if snapshot["status"] == "REJECTED" and current["kind"] == "live":
                    self._count_rejection(db, current["ledger_id"])
        if problem:
            self._stop("safety", problem)
            raise Stopped
        return final

    def _cancel(self, order: dict) -> None:
        try:
            toss_account.cancel_order(self.http, self.token, order["account_seq"], order["order_id"])
        except toss.TossError as error:
            if error.code in toss_account.TOKEN_CODES:
                raise
            print(f"취소 요청 오류(상세 조회로 다시 본다): {error.code} {error.remote}")

    def _cancel_all(self) -> None:
        """봇의 끝나지 않은 주문마다 취소를 요청한다. 끝났는지는 다음 차례의 상세 조회로 정한다.

        정지를 따르는 중이므로 토큰 오류도 출력만 한다. 이어지는 토큰 오류는 다음 차례의 상세 조회에서 안전 정지가 된다.
        """
        with v5_book.connect(self.paths.live) as db:
            pending = v5_book.orders(db, "status = 'open' AND kind != 'dry'")
        for order in pending:
            try:
                self._cancel(order)
            except toss.TossError as error:
                print(f"취소 요청 오류: {error.code} {error.remote}")

    def _count_rejection(self, db: sqlite3.Connection, ledger_id: int) -> None:
        count = (v5_book.latch(db, ledger_id, "rejections", "") or 0) + 1
        v5_book.set_latch(db, ledger_id, "rejections", "", count)
        if count >= 3:
            v5_book.add_stop(db, "safety", "hold", "거부가 세 번 이어졌다", self.clock())

    # 다시 보내기

    def _resolve_unknown(self) -> None:
        with v5_book.connect(self.paths.live) as db:
            unknown = v5_book.orders(db, "status IN ('sending', 'unknown')")
        for order in unknown:
            now = self.clock()
            created = datetime.fromisoformat(order["created_at"])
            if now - created >= UNKNOWN_LIMIT:
                self._manual(order, "결과를 모르는 주문이 8분을 넘었다")
                continue
            if self._stops():
                self._manual(order, "결과를 모르는 주문을 다시 보내기 전에 정지가 들어왔다")
                continue
            if now < self.next_try.get(order["client_order_id"], now):
                continue
            self._send(order)

    def _manual(self, order: dict, reason: str) -> None:
        with v5_book.connect(self.paths.live) as db:
            if not any(stop["kind"] == "manual" for stop in v5_book.active_stops(db)):
                v5_book.add_stop(db, "manual", "hold", f"수동 확인 필요: {reason}(주문 번호 {order['client_order_id']})", self.clock())
        self.next_try.pop(order["client_order_id"], None)

    def _send(self, order: dict) -> None:
        """낼 주문을 한 번 보낸다. 결과에 따라 open, unknown, rejected가 된다."""
        try:
            order_id = toss_account.create_order(
                self.http, self.token, order["account_seq"], order["client_order_id"], order["symbol"], order["side"], order["tif"], order["quantity"]
            )
        except toss.TossError as error:
            kind = toss_account.classify(error, order["token_errors"])
            token_error = error.code in toss_account.TOKEN_CODES
            unread = error.code == "V5_TOSS_RESPONSE_INVALID"  # 성공 응답의 본문을 읽지 못했다. 주문이 생겼을 수 있다
            with v5_book.connect(self.paths.live) as db:
                if kind == "retry":
                    v5_book.update_order(db, order["client_order_id"], status="unknown", error=error.remote or error.code, token_errors=order["token_errors"] + token_error)
                    wait = timedelta(seconds=error.retry_after) if error.retry_after else RESEND
                    limit = datetime.fromisoformat(order["created_at"]) + UNKNOWN_LIMIT
                    self.next_try[order["client_order_id"]] = min(self.clock() + wait, limit)
                    return
                v5_book.update_order(db, order["client_order_id"], status="unknown" if unread else "rejected", error=error.remote or error.code)
                if kind == "reject" and order["kind"] == "live":
                    self._count_rejection(db, order["ledger_id"])
            if unread:
                self._manual(order, "주문 생성 응답을 읽을 수 없어 주문이 생겼는지 모른다")
                raise Stopped from error
            if kind == "stop":
                self._stop("safety", f"주문 오류({error.code} {error.remote})")
                raise Stopped from error
            return
        with v5_book.connect(self.paths.live) as db:
            v5_book.update_order(db, order["client_order_id"], status="open", order_id=order_id, error=None)
        self.next_try.pop(order["client_order_id"], None)

    def _unknown(self) -> bool:
        with v5_book.connect(self.paths.live) as db:
            return bool(v5_book.orders(db, "status IN ('sending', 'unknown')"))

    # 목표와 맞추기

    def _base(self) -> object:
        """정산한 시세(PRD 02 base). 정산한 날이 바뀔 때만 다시 읽는다."""
        settled = v3_daily.settled_days(self.paths.daily.paper)
        key = (self.today, len(settled), settled[-1] if settled else None)
        if self.cached is None or self.cached[0] != key:
            self.cached = (key, v5_target.base(self.paths.daily, self.period, self.today))
        return self.cached[1]

    def _target(self, turn: Turn, bars: dict[str, list[Bar]], record: dict) -> v5_target.Target:
        base = self._base()
        decisions = v4_head.read(self.paths.daily.paper)
        received = {symbol: tuple(value) for symbol, value in json.loads(record["limits"]).items()} if record["limits"] else None
        return self.target(base, self.period, decisions, self.today, turn.name, turn.bar, bars, received)

    def _follow(self, turn: Turn, result: v5_target.Target, blocked: str | None) -> str:
        ledger = self._ledger()
        with v5_book.connect(self.paths.live) as db:
            cash, held = v5_book.balance(db, ledger["id"])
        other = [symbol for symbol in held if symbol != result.symbol]
        if other:
            return self._order(turn, other[0], "SELL", held[other[0]], f"목표({result.symbol or '현금'})가 아닌 종목을 판다")
        if result.symbol is None or held:
            return "맞음"
        if blocked:
            return "실계좌 손실 한도로 사지 않음"
        record = self._day_record()
        config = read_config(self.paths.config)
        if config is None:
            self._stop("safety", "설정 파일이 없거나 틀리다")
            raise Stopped
        upper = json.loads(record["limits"])[result.symbol][0]
        rate = max(Decimal(record["commission"]), ENGINE_RATE)
        power = toss_account.buying_power(self.http, self.token, self._account()) if record["mode"] == "live" else cash
        count = quantity(min(cash, power), upper, rate, config["max_shares"])
        if not count:
            return "살 수 있는 수량이 0주"
        return self._order(turn, result.symbol, "BUY", count, "목표 종목을 산다")

    def _sell_all(self, turn: Turn, reason: str) -> str:
        with v5_book.connect(self.paths.live) as db:
            _, held = v5_book.balance(db, self._ledger()["id"])
        if not held:
            return f"{reason}: 판 것 없음"
        symbol = next(iter(held))
        return self._order(turn, symbol, "SELL", held[symbol], reason)

    def _order(self, turn: Turn, symbol: str, side: str, count: int, why: str) -> str:
        record, ledger, now = self._day_record(), self._ledger(), self.clock()
        tif = "OPG" if turn.name == "pre_open" else "DAY"
        client_order_id = f"v5-{self.today.replace('-', '')}-{(turn.bar or f'{turn.at:%H:%M}').replace(':', '')}-{symbol}-{side[0]}"
        if record["mode"] == "dry":
            return self._dry_fill(turn, client_order_id, ledger, symbol, side, tif, count, why)
        self._scan_unknown_orders()
        stops = self._stops()
        if stops and not (side == "SELL" and self._sell_stop(stops)):
            return "주문 직전에 정지가 들어와 보내지 않음"
        row = {
            "client_order_id": client_order_id, "kind": "live", "ledger_id": ledger["id"], "account_seq": record["account_seq"], "date": self.today,
            "turn": turn.bar or turn.name, "symbol": symbol, "side": side, "tif": tif, "quantity": count, "created_at": now.isoformat(timespec="seconds"),
        }
        try:
            with v5_book.connect(self.paths.live) as db:
                v5_book.add_intent(db, row)
        except sqlite3.IntegrityError:
            return "이 차례에 이미 낸 실주문이 있어 보내지 않음"
        with v5_book.connect(self.paths.live) as db:
            intent = v5_book.orders(db, "client_order_id = ?", (client_order_id,))[0]
        self._send(intent)
        return f"{why}: {symbol} {side} {count}주 {tif}"

    def _dry_fill(self, turn: Turn, client_order_id: str, ledger: dict, symbol: str, side: str, tif: str, count: int, why: str) -> str:
        price = self.prices[symbol]
        amount, now = price * count, self.clock()
        fee = engine._fee(amount)
        tax = 0
        with v5_book.connect(self.paths.live) as db:
            if side == "SELL" and symbol == engine.INVERSE:
                bought = v5_book.orders(db, "ledger_id = ? AND symbol = ? AND side = 'BUY'", (ledger["id"], symbol))
                gain = (price - int(Decimal(bought[-1]["average_price"]))) * count if bought else 0
                tax = gain * 154 // 1000 if gain > 0 else 0
            row = {
                "client_order_id": client_order_id, "kind": "dry", "ledger_id": ledger["id"], "account_seq": self._account(), "date": self.today,
                "turn": turn.bar or turn.name, "symbol": symbol, "side": side, "tif": tif, "quantity": count, "created_at": now.isoformat(timespec="seconds"),
            }
            v5_book.add_intent(db, row)
            order = v5_book.orders(db, "client_order_id = ?", (client_order_id,))[0]
            snapshot = {"status": "FILLED", "filled_quantity": count, "filled_amount": Decimal(amount), "average_price": Decimal(price), "commission": Decimal(fee), "tax": Decimal(tax)}
            v5_book.apply_fill(db, order, snapshot, now)
            v5_book.update_order(db, client_order_id, status="final")
        return f"{why}(연습): {symbol} {side} {count}주 {tif} {price:,}원"

    # 손실 한도

    def _limits(self, record: dict) -> str | None:
        """봇 자산으로 실계좌 손실 한도를 본다. 사지 않을 한도가 있으면 'block', 팔아야 하면 'sell', 없으면 None."""
        ledger = self._ledger()
        if ledger is None or any(symbol not in self.prices for symbol in SYMBOLS):
            return None
        with v5_book.connect(self.paths.live) as db:
            cash, held = v5_book.balance(db, ledger["id"])
            equity = cash + sum(count * self.prices[symbol] for symbol, count in held.items())
            week = "{}-W{:02d}".format(*datetime.fromisoformat(self.today).isocalendar()[:2])
            created = ledger["created_at"][:10]
            daily, weekly = v5_book.latch(db, ledger["id"], "daily", self.today), v5_book.latch(db, ledger["id"], "weekly", week)
            fresh = None
            if equity * 100 <= ledger["start_cash"] * 80:
                if not any(stop["kind"] == "limit" for stop in v5_book.active_stops(db)):
                    v5_book.add_stop(db, "limit", "sell", "누적 손실 한도", self.clock())
                return "sell"
            same_base = record["limits"] and self._same_base(record)
            daily_base = ledger["start_cash"] if created == self.today else self._close_equity(db, ledger, v5_target.previous_trading_day(self.today, self.period.holidays))
            if daily is None and same_base and daily_base is not None and equity * 100 <= daily_base * 98:
                v5_book.set_latch(db, ledger["id"], "daily", self.today, 1)
                daily, fresh = 1, "daily"
            weekly_base = ledger["start_cash"] if created >= self._week_start() else self._close_equity(db, ledger, self._last_week_day())
            if weekly is None and weekly_base is not None and equity * 100 <= weekly_base * 95:
                v5_book.set_latch(db, ledger["id"], "weekly", week, 1)
                weekly, fresh = 1, "weekly"
        if fresh or ((daily or weekly) and held):
            return "sell"
        return "block" if daily or weekly else None

    def _close_equity(self, db: sqlite3.Connection, ledger: dict, day: str) -> int | None:
        return close_equity(self.paths, db, ledger, day)

    def _week_start(self) -> str:
        day = datetime.fromisoformat(self.today)
        return (day - timedelta(days=day.weekday())).date().isoformat()

    def _last_week_day(self) -> str:
        return v5_target.previous_trading_day(self._week_start(), self.period.holidays)

    def _same_base(self, record: dict) -> bool:
        received = json.loads(record["limits"])
        previous = v5_target.previous_trading_day(self.today, self.period.holidays)
        base = self._base()
        if base is None or base.days[-1] != previous:
            return False
        return all(list(v5_target.limits(base.daily[symbol][previous][0], self.today)) == list(received[symbol]) for symbol in SYMBOLS)

    # 확인과 정지

    def _scan_unknown_orders(self) -> None:
        """두 종목의 진행 중 주문 목록에 봇이 기록하지 않은 주문이 있으면 안전 정지한다."""
        with v5_book.connect(self.paths.live) as db:
            known = {order["order_id"] for order in v5_book.orders(db, "order_id IS NOT NULL")}
        for symbol in SYMBOLS:
            for order in toss_account.open_orders(self.http, self.token, self._account(), symbol):
                if order["order_id"] not in known:
                    self._stop("safety", f"봇이 기록하지 않은 {symbol} 대기 주문이 있다")
                    raise Stopped

    def _compare_holdings(self) -> None:
        ledger = self._ledger()
        real = toss_account.holdings(self.http, self.token, self._account())
        with v5_book.connect(self.paths.live) as db:
            _, held = v5_book.balance(db, ledger["id"])
        matched = all(real[symbol] == held.get(symbol, 0) for symbol in SYMBOLS)
        self.real = (real, matched)
        if not matched:
            self._stop("safety", f"실계좌 수량 {real}이 봇 장부 {held}와 다르다")
            raise Stopped

    def _stops(self) -> list[dict]:
        with v5_book.connect(self.paths.live) as db:
            return v5_book.active_stops(db)

    def _sell_stop(self, stops: list[dict]) -> bool:
        """정지 중에 팔아도 되는가: sell 정지가 있고 manual 정지가 없다."""
        return any(stop["mode"] == "sell" for stop in stops) and not any(stop["kind"] == "manual" for stop in stops)

    def _stop(self, kind: str, reason: str) -> None:
        with v5_book.connect(self.paths.live) as db:
            v5_book.add_stop(db, kind, "hold", reason, self.clock())
        print(f"V5_STOP {kind}: {reason}")
        try:
            self._cancel_all()
        except (Stopped, toss.TossError):
            pass

    # 받기

    def _fetch_day_values(self) -> None:
        """두 종목의 오늘 상·하한가와 수수료율을 아직 받지 못했으면 받아 둔다."""
        record = self._day_record()
        if record["account_seq"] is None:
            return
        try:
            if not record["limits"]:
                limits = {symbol: list(toss_account.price_limits(self.http, self.token, symbol)) for symbol in SYMBOLS}
                with v5_book.connect(self.paths.live) as db:
                    db.execute("UPDATE days SET limits = ? WHERE date = ?", (json.dumps(limits), self.today))
            if not record["commission"]:
                rate = toss_account.commission_rate(self.http, self.token, record["account_seq"])
                with v5_book.connect(self.paths.live) as db:
                    db.execute("UPDATE days SET commission = ? WHERE date = ?", (str(rate), self.today))
        except toss.TossError as error:
            if error.code in toss_account.TOKEN_CODES:
                raise
            print(f"상·하한가·수수료율을 받지 못했다(다음 차례에 다시): {error.code}")

    def _wait_bars(self, turn: Turn) -> dict[str, list[Bar]]:
        limit = _at(self.today, turn.bar) + BAR_WAIT
        while True:
            try:
                bars = self.bars(self.today)
            except toss.TossError as error:
                if error.code in toss_account.TOKEN_CODES:
                    raise
                print(f"1분봉을 받지 못했다: {error.code}")
                bars = {}
            if all(any(bar.time == turn.bar for bar in bars.get(symbol, [])) for symbol in SYMBOLS) or self.clock() >= limit or self._stops():
                return bars
            self.sleep(2)

    def _fetch_bars(self, day: str) -> dict[str, list[Bar]]:
        tomorrow = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
        return {
            symbol: [Bar(time[11:], *values) for _, time, *values in toss.minute_rows(symbol, toss.candles(self.http, self.token, symbol, "1m", False, day), day, tomorrow)]
            for symbol in SYMBOLS
        }

    def _update_prices(self, turn: Turn, bars: dict[str, list[Bar]]) -> None:
        for symbol in SYMBOLS:
            used = [bar for bar in bars.get(symbol, []) if turn.bar is None or bar.time <= turn.bar]
            if used:
                self.prices[symbol] = used[-1].close
            elif symbol not in self.prices:
                base = self._base()
                if base is not None:
                    self.prices[symbol] = base.daily[symbol][base.days[-1]][0]

    # 기록

    def _record(self, turn: Turn, result: v5_target.Target | None, action: str) -> None:
        record = self._day_record() or {"mode": "dry"}
        ledger = self._ledger()
        row = {"time": self.clock().isoformat(timespec="seconds"), "turn": turn.bar or turn.name, "mode": record["mode"], "action": action}
        if result is not None:
            row["target"] = (result.symbol or "현금") if result.reason is None else None
            row["reason"] = result.reason
        if ledger is not None:
            with v5_book.connect(self.paths.live) as db:
                cash, held = v5_book.balance(db, ledger["id"])
                row["limits"] = {kind: bool(v5_book.latch(db, ledger["id"], kind, period)) for kind, period in (("daily", self.today), ("weekly", self._iso_week()))}
            row |= {"cash": cash, "holdings": held, "prices": dict(self.prices)}
            if all(symbol in self.prices for symbol in held):
                row["equity"] = cash + sum(count * self.prices[symbol] for symbol, count in held.items())
        if self.real:
            row["real"], row["matched"] = self.real[0], int(self.real[1])
            self.real = None
        with v5_book.connect(self.paths.live) as db:
            v5_book.add_turn(db, row)
        print(f"{row['time']} {row['turn']} {action}")

    def _iso_week(self) -> str:
        return "{}-W{:02d}".format(*datetime.fromisoformat(self.today).isocalendar()[:2])

    def _day_record(self) -> dict | None:
        with v5_book.connect(self.paths.live) as db:
            return v5_book.day(db, self.today)

    def _ledger(self) -> dict | None:
        record = self._day_record()
        with v5_book.connect(self.paths.live) as db:
            return v5_book.ledger(db, record["mode"] if record else "dry")

    def _account(self) -> int:
        return self._day_record()["account_seq"]


# 명령


def stop(paths: Paths, sell: bool, clock: Callable[[], datetime]) -> int:
    """v5-stop [--sell]: 주문 잠금을 기다리지 않고 사람의 정지를 쓴다."""
    with v5_book.connect(paths.live) as db:
        number = v5_book.add_stop(db, "human", "sell" if sell else "hold", "사람의 정지(v5-stop --sell)" if sell else "사람의 정지(v5-stop)", clock())
    print(f"정지 {number}: {'멈추고 다 팔기' if sell else '멈춤(보유 그대로)'}. 실행기가 5초 안에 따른다")
    return 0


def resume(paths: Paths, http: httpx.Client, token: toss_account.Token, clock: Callable[[], datetime], sleep: Callable[[float], None], new_ledger: bool) -> int:
    """v5-resume [--new-ledger]: 주문 잠금을 쥐고, 시작할 때까지 들어온 정지만 푼다."""
    with file_lock(paths.order_lock, LOCK_WAIT, sleep) as held:
        if not held:
            print("V5_ORDER_LOCK_BUSY: 주문 잠금을 30초 안에 쥐지 못했다")
            return 1
        with v5_book.connect(paths.live) as db:
            stops = v5_book.active_stops(db)
            up_to = max((stop["id"] for stop in stops), default=0)
            live, today = v5_book.ledger(db, "live"), clock().date().isoformat()
            record = v5_book.day(db, today)
            unresolved = v5_book.orders(db, "status IN ('sending', 'unknown', 'open') AND kind != 'dry'")
        config = read_config(paths.config)
        if new_ledger:
            if config is None:
                print("설정 파일이 없거나 틀려 새 장부를 만들지 않는다")
                return 1
            account = config["account_seq"]
            if record and record["account_seq"] not in (None, account):
                print("오늘 계좌와 다른 account_seq로는 새 장부를 만들지 않는다(다음 거래일 시작 점검 전에 한다)")
                return 1
            real = toss_account.holdings(http, token, account)
            pending = [order for symbol in SYMBOLS for order in toss_account.open_orders(http, token, account, symbol)]
            if any(real.values()) or pending:
                print(f"두 종목이 0주이고 대기 주문이 없어야 새 장부를 만든다(지금 {real}, 대기 {len(pending)}건)")
                return 1
            with v5_book.connect(paths.live) as db:
                db.execute("UPDATE orders SET status = 'abandoned' WHERE status IN ('sending', 'unknown', 'open') AND kind != 'dry'")
                v5_book.create_ledger(db, "live", account, config["budget"], clock())
                v5_book.clear_stops(db, up_to, clock(), manual=True)
            print(f"새 실장부를 {config['budget']:,}원으로 만들고 정지 {up_to}번까지 풀었다")
            return 0
        problems = []
        if any(stop["kind"] == "manual" for stop in stops):
            problems.append("수동 확인 필요 정지가 있다(토스 앱에서 정리한 뒤 --new-ledger)")
        if unresolved:
            problems.append("끝나지 않은 봇 주문이나 결과를 모르는 낼 주문이 있다")
        if live:
            account = live["account_seq"]
            real = toss_account.holdings(http, token, account)
            with v5_book.connect(paths.live) as db:
                _, held = v5_book.balance(db, live["id"])
            if any(real[symbol] != held.get(symbol, 0) for symbol in SYMBOLS):
                problems.append(f"실계좌 수량 {real}이 장부 {held}와 다르다")
        else:  # 연습 운용: 그날 계좌, 그날 기록 전이면 설정 계좌의 진행 중 목록을 본다
            account = record["account_seq"] if record else config["account_seq"] if config else None
        with v5_book.connect(paths.live) as db:
            known = {order["order_id"] for order in v5_book.orders(db, "order_id IS NOT NULL")}
        if account is not None and any(order["order_id"] not in known for symbol in SYMBOLS for order in toss_account.open_orders(http, token, account, symbol)):
            problems.append("봇이 기록하지 않은 대기 주문이 있다")
        if problems:
            print("풀지 않았다: " + "; ".join(problems))
            return 1
        with v5_book.connect(paths.live) as db:
            v5_book.clear_stops(db, up_to, clock(), manual=False)
        print(f"정지 {up_to}번까지 풀었다")
        return 0


def check(paths: Paths, http: httpx.Client, token: toss_account.Token, clock: Callable[[], datetime], sleep: Callable[[float], None], order: bool) -> int:
    """v5-check [--order]: 계좌 조회, 그리고 사람이 지켜보는 실주문 점검(PRD 03)."""
    config = read_config(paths.config)
    found = toss_account.accounts(http, token)
    for item in found:
        print(f"계좌 seq {item['seq']} {item['type']} 끝 네 자리 {item['tail']}")
    if config is None:
        print("설정 파일이 없거나 틀리다(account_seq를 정해 data/v5/config.json에 적는다)")
        return 1 if order else 0
    today = v5_book.read(paths.live, "SELECT account_seq FROM days WHERE date = ?", (clock().date().isoformat(),))
    account = today[0]["account_seq"] if today and today[0]["account_seq"] is not None else config["account_seq"]  # 그날 기록이 있으면 그날 계좌(SPEC 4.7)
    print(
        f"계좌 seq {account}: 보유 {toss_account.holdings(http, token, account)}, 매수 가능 금액 {toss_account.buying_power(http, token, account):,}원, "
        f"수수료율 {toss_account.commission_rate(http, token, account)}"
    )
    for symbol in SYMBOLS:
        print(f"{symbol} 오늘 상·하한가 {toss_account.price_limits(http, token, symbol)}")
    if not order:
        return 0
    with file_lock(paths.order_lock, LOCK_WAIT, sleep) as held:
        if not held:
            print("V5_ORDER_LOCK_BUSY: 주문 잠금을 30초 안에 쥐지 못했다")
            return 1
        return _check_order(paths, http, token, clock, sleep, config, found)


def _check_order(
    paths: Paths, http: httpx.Client, token: toss_account.Token, clock: Callable[[], datetime], sleep: Callable[[float], None], config: dict, found: list[dict]
) -> int:
    """점검 주문. 계좌는 그날 기록이 있으면 그날 account_seq(SPEC 4.7: 시작 점검 때만 읽는다), 없으면 설정의 account_seq다."""
    now = clock()
    today = now.date().isoformat()
    with v5_book.connect(paths.live) as db:
        record = v5_book.day(db, today)
        unresolved = v5_book.orders(db, "status IN ('sending', 'unknown', 'open') AND kind != 'dry'")
    if (record["mode"] if record else ("live" if config["orders"] else "dry")) != "dry":
        print("연습 운용 중에만 점검 주문을 낸다")
        return 1
    account = record["account_seq"] if record else config["account_seq"]
    if not any(item["seq"] == account and item["type"] == "BROKERAGE" for item in found):
        print(f"점검할 계좌(seq {account})가 계좌 목록의 종합매매(BROKERAGE) 계좌가 아니다")
        return 1
    real = toss_account.holdings(http, token, account)
    pending = [item for symbol in SYMBOLS for item in toss_account.open_orders(http, token, account, symbol)]
    if any(real.values()) or pending or unresolved:
        print(f"두 종목이 0주이고 대기 주문·결과 모르는 주문이 없어야 점검한다(지금 {real}, 대기 {len(pending)}건)")
        return 1
    session = sessions.session(today, sessions.exceptions())
    opening, close = _at(today, session.open), _at(today, session.close)
    if opening - timedelta(minutes=30) <= now <= opening - timedelta(minutes=1):
        steps = [("BUY", "OPG", "CANCELED")]
    elif opening + timedelta(minutes=1) <= now <= close - timedelta(minutes=11):
        steps = [("BUY", "DAY", "FILLED"), ("SELL", "DAY", "FILLED")]
    else:
        print("점검 시간이 아니다(개장 30분 전~1분 전, 개장 1분 뒤~마감 11분 전)")
        return 1
    for side, tif, expected in steps:
        with v5_book.connect(paths.live) as db:
            known = {item["order_id"] for item in v5_book.orders(db, "order_id IS NOT NULL")}
        if any(item["order_id"] not in known for symbol in SYMBOLS for item in toss_account.open_orders(http, token, account, symbol)):
            with v5_book.connect(paths.live) as db:
                v5_book.add_stop(db, "safety", "hold", "점검 주문 직전에 봇이 기록하지 않은 대기 주문이 있다", clock())
            print("봇이 기록하지 않은 대기 주문이 있어 안전 정지했다")
            return 1
        with v5_book.connect(paths.live) as db:
            stops = v5_book.active_stops(db)
        if stops:
            print("정지가 있어 점검 주문을 보내지 않았다")
            return 1
        client_order_id = f"v5c-{clock():%Y%m%d-%H%M%S}-{side[0]}"
        with v5_book.connect(paths.live) as db:
            v5_book.add_intent(
                db,
                {
                    "client_order_id": client_order_id, "kind": "check", "ledger_id": None, "account_seq": account, "date": today, "turn": "check",
                    "symbol": "069500", "side": side, "tif": tif, "quantity": 1, "created_at": clock().isoformat(timespec="seconds"),
                },
            )
        try:
            order_id = toss_account.create_order(http, token, account, client_order_id, "069500", side, tif, 1)
        except toss.TossError as error:
            kind = toss_account.classify(error)
            unread = error.code == "V5_TOSS_RESPONSE_INVALID"  # 성공 응답의 본문을 읽지 못했다. 주문이 생겼을 수 있다
            with v5_book.connect(paths.live) as db:
                v5_book.update_order(db, client_order_id, status="unknown" if kind == "retry" or unread else "rejected", error=error.remote or error.code)
                if unread:
                    v5_book.add_stop(db, "manual", "hold", f"수동 확인 필요: 점검 주문 생성 응답을 읽을 수 없어 주문이 생겼는지 모른다(주문 번호 {client_order_id})", clock())
                elif kind == "stop":
                    v5_book.add_stop(db, "safety", "hold", f"점검 주문 오류({error.code} {error.remote})", clock())
            print(f"점검 주문 오류 {error.code} {error.remote}: 다음 단계로 가지 않는다")
            return 1
        with v5_book.connect(paths.live) as db:
            v5_book.update_order(db, client_order_id, status="open", order_id=order_id)
        try:
            if expected == "CANCELED":
                try:
                    toss_account.cancel_order(http, token, account, order_id)
                except toss.TossError as error:
                    if error.code in toss_account.TOKEN_CODES:
                        raise
                    print(f"취소 요청 오류 {error.code} {error.remote}")
            status, waited = None, 0
            while waited <= CHECK_WAIT:
                snapshot = toss_account.order(http, token, account, order_id)
                status = snapshot["status"]
                with v5_book.connect(paths.live) as db:
                    current = v5_book.orders(db, "client_order_id = ?", (client_order_id,))[0]
                    readable = not _mismatch(current, snapshot) and v5_book.apply_fill(db, current, snapshot, clock())
                    if readable:
                        v5_book.update_order(db, client_order_id, status="final" if toss_account.is_final(status) else "open")
                    else:
                        v5_book.add_stop(db, "safety", "hold", "점검 주문 상세가 낸 주문과 맞지 않거나 체결 금액을 읽을 수 없다", clock())
                if not readable:
                    print("점검 주문 상세를 믿을 수 없어 안전 정지했다. 다음 단계로 가지 않는다")
                    return 1
                if toss_account.is_final(status):
                    break
                sleep(2)
                waited += 2
        except toss.TossError as error:
            if toss_account.classify(error) == "stop" or error.code in toss_account.TOKEN_CODES:
                with v5_book.connect(paths.live) as db:
                    v5_book.add_stop(db, "safety", "hold", f"점검 주문 뒤 토스 응답 오류({error.code} {error.remote})", clock())
            print(f"점검 주문 뒤 토스 오류 {error.code} {error.remote}: 다음 단계로 가지 않는다. 실행기가 이어서 조회·취소한다")
            return 1
        wanted = 0 if expected == "CANCELED" else 1
        print(f"점검 {side} {tif} 1주: {status}, 체결 {snapshot['filled_quantity']}주")
        if status != expected or snapshot["filled_quantity"] != wanted:
            print(f"기대한 상태({expected}, 체결 {wanted}주)가 아니어서 다음 단계로 가지 않는다. 실행기가 이어서 조회·취소한다")
            return 1
    print(f"끝: 보유 {toss_account.holdings(http, token, account)}, 대기 주문 {sum(len(toss_account.open_orders(http, token, account, symbol)) for symbol in SYMBOLS)}건")
    return 0


def _mismatch(order: dict, snapshot: dict) -> bool:
    """상세가 낸 주문과 종목·방향·주문 수량이 다르거나 체결 수량이 앞서 반영한 수량보다 적거나 주문 수량보다 많은가(읽을 수 없는 응답, PRD 03)."""
    return (snapshot["symbol"], snapshot["side"], snapshot["quantity"]) != (order["symbol"], order["side"], order["quantity"]) or not (
        order["applied_quantity"] <= snapshot["filled_quantity"] <= order["quantity"]
    )


def _fee(amount: int, rate: Decimal) -> int:
    return int((amount * rate).to_integral_value(rounding=ROUND_CEILING))


def _at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=KST)

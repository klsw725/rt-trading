"""v6 시장 세션과 후보 종목(v6 SPEC 2절, PRD 01): 토스 요청, 장 운영 정보, 판단 차례, 종목 정보, 후보."""

import json
import os
from collections.abc import Callable
from datetime import date as Date
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from rt_trading import toss, v3_head
from rt_trading.toss import KST

TIMEOUT = 10  # v6가 쓰는 httpx.Client의 시간 제한(초)
RANK_COUNT = 50
PICKS = 10  # 열린 시장마다 고르는 후보 수
TRADING_DAYS = 10  # 검증 구간의 국내 거래일 수
FILL_AFTER = timedelta(minutes=5)  # 체결 시각 E = 판단 시각 T + 5분
DAYS = ("previousBusinessDay", "today", "nextBusinessDay")
SESSIONS = {
    "KR": {"preMarket": "pre", "regularMarket": "regular", "afterMarket": "after"},
    "US": {"dayMarket": "day", "preMarket": "pre", "regularMarket": "regular", "afterMarket": "after"},
}
MARKETS = {"KOSPI": "KR", "KOSDAQ": "KR", "KR_ETC": "KR", "NYSE": "US", "NASDAQ": "US", "AMEX": "US", "US_ETC": "US"}
INSTRUMENT_COLUMNS = (
    "symbol",
    "name",
    "market",
    "security_type",
    "status",
    "currency",
    "leverage",
    "nxt_supported",
    "nxt_suspended",
    "krx_suspended",
    "liquidation",
    "observed_at",
)


class V6Error(Exception):
    """오류 코드와 설명. 인증 정보와 토큰은 담지 않는다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def request(
    http: httpx.Client,
    token: toss.SharedToken,
    path: str,
    params: dict,
    deadline: datetime,
    clock: Callable[[], datetime],
    events: list[dict],
) -> tuple[object, datetime]:
    """GET 하나를 보내 (result, 받은 시각)을 돌려준다. 저장소에는 쓰지 않는다.

    다시 보내기는 _send의 것(5xx·네트워크 1·2·4초, 토큰 오류 한 번)에 429 한 번이고, 429는 events에 남긴다.
    마감이 지나면 보내지 않고, 마감보다 늦게 받은 응답은 버린다.
    """

    def notify(where: str) -> None:
        events.append({"at": clock().isoformat(timespec="seconds"), "path": where})

    marks = (toss.INTERRUPT.set(lambda: clock() >= deadline), toss.RATE_LIMIT_RETRIES.set(1), toss.ON_RATE_LIMIT.set(notify))
    try:
        response = toss._send(http, "GET", path, token, params=params)
    except toss.TossError as error:
        raise V6Error("V6_TOSS_FAILED", f"{path}: {error.code}, HTTP {error.status}") from error
    except toss.Interrupted as error:
        raise V6Error("V6_TOSS_FAILED", f"{path}: 마감이 지나 보내지 않았다") from error
    finally:
        toss.ON_RATE_LIMIT.reset(marks[2])
        toss.RATE_LIMIT_RETRIES.reset(marks[1])
        toss.INTERRUPT.reset(marks[0])
    observed = clock()
    if observed > deadline:
        raise V6Error("V6_TOSS_FAILED", f"{path}: 마감 뒤에 받은 응답이라 쓰지 않는다")
    try:
        return response.json()["result"], observed
    except (ValueError, KeyError, TypeError) as error:
        raise V6Error("V6_TOSS_RESPONSE_INVALID", f"{path}: 응답에 result가 없다") from error


def fetch_calendar(
    http: httpx.Client,
    token: toss.SharedToken,
    market: str,
    day: str,
    deadline: datetime,
    clock: Callable[[], datetime],
    events: list[dict],
) -> dict[str, dict]:
    """세 날의 세션 {날짜: {이름: [시작, 끝]}}. 미국은 한국 날짜 day의 세션이 미국 날짜 day − 1과 day에 걸치므로 day − 1로 부른다."""
    asked = day if market == "KR" else (Date.fromisoformat(day) - timedelta(days=1)).isoformat()
    result, _ = request(http, token, f"/api/v1/market-calendar/{market}", {"date": asked}, deadline, clock, events)
    days = {}
    try:
        for key in DAYS:  # 세 날이 다 있어야 그 사이의 빠진 날을 휴장일로 볼 수 있다
            item = result[key]
            hours = item["integrated"] if market == "KR" else item
            days[Date.fromisoformat(item["date"]).isoformat()] = {
                name: [_kst(hours[field]["startTime"]), _kst(hours[field]["endTime"])]
                for field, name in SESSIONS[market].items()
                if hours and hours.get(field)
            }
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise V6Error("V6_TOSS_RESPONSE_INVALID", f"{market} 장 운영 정보 형식 오류({error!r})") from error
    return days


def merge_calendar(calendar: dict, market: str, fetched: dict[str, dict]) -> dict:
    """받은 세 날을 넣고 그 사이에 빠진 날은 휴장일(빈 객체)로 넣는다. 이미 있는 날은 새 값으로 바꾼다."""
    known = calendar.setdefault(market, {})
    day, last = Date.fromisoformat(min(fetched)), max(fetched)
    while (text := day.isoformat()) <= last:
        known[text] = fetched.get(text, {})
        day += timedelta(days=1)
    return calendar


def load_calendar(path: Path) -> dict:
    """저장한 장 운영 정보. 파일이 없으면 빈 값이다."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"KR": {}, "US": {}}


def save_calendar(path: Path, calendar: dict) -> None:
    """임시 파일에 쓴 뒤 바꿔 끼운다. PRD 04가 잠금 안에서 부른다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(calendar, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def sessions(calendar: dict, at: datetime) -> dict | None:
    """at에 열린 세션 {"KR": 이름 또는 None, "US": 이름 또는 None}. 국내 d, 미국 d − 1·d 중 하나라도 없으면 None(모름)이다."""
    spans = _spans(calendar, at)
    return None if spans is None else {market: span[0] if span else None for market, span in spans.items()}


def session_ends(calendar: dict, at: datetime) -> dict | None:
    """at에 열린 세션이 끝나는 시각 {"KR": ISO 시각 또는 None, "US": …}. 모르면 None이다."""
    spans = _spans(calendar, at)
    return None if spans is None else {market: span[1] if span else None for market, span in spans.items()}


def trading_day(calendar: dict, day: str) -> bool | None:
    """국내 거래일(정규장이 있는 날)인지. 그날 장 운영 정보가 없으면 None이다."""
    hours = calendar.get("KR", {}).get(day)
    return None if hours is None else "regular" in hours


def fill_time(day: str, clock: str) -> datetime:
    """판단 차례 day clock(T)의 체결 시각 E = T + 5분. 판단 차례와 tradable_now는 이 시각으로 정한다."""
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=KST) + FILL_AFTER


def slots(calendar: dict, day: str) -> list[tuple[str, str]]:
    """그날의 (차례 HH:MM, 종류). 종류는 E에 한 시장이라도 열리면 slot, 장 운영 정보가 없어 모르면 missing이다. 차례가 아닌 시각은 넣지 않는다."""
    result = []
    for hour in range(24):
        clock = f"{hour:02d}:40"
        open_at = sessions(calendar, fill_time(day, clock))
        if open_at is None:
            result.append((clock, "missing"))
        elif open_at["KR"] or open_at["US"]:
            result.append((clock, "slot"))
    return result


def end_time(calendar: dict, start: str, fill: Callable[[str], None]) -> datetime:
    """start부터 국내 거래일을 세어 10번째 날의 다음 날 00:00. 장 운영 정보에 없는 날은 fill(그날)로 채운다."""
    day, count = Date.fromisoformat(start), 0
    while True:
        text = day.isoformat()
        if text not in calendar.get("KR", {}):
            fill(text)
        if text not in calendar.get("KR", {}):
            raise V6Error("V6_CALENDAR_MISSING", f"{text}의 국내 장 운영 정보를 받지 못했다")
        if "regular" in calendar["KR"][text]:
            count += 1
            if count == TRADING_DAYS:
                return datetime.combine(day + timedelta(days=1), time(0), KST)
        day += timedelta(days=1)


def fetch_rankings(
    http: httpx.Client, token: toss.SharedToken, market: str, deadline: datetime, clock: Callable[[], datetime], events: list[dict]
) -> tuple[list[dict], datetime]:
    """거래대금 랭킹 50위까지 [{"rank", "symbol", "change_rate"}](순위 순)와 받은 시각."""
    params = {"type": "MARKET_TRADING_AMOUNT", "marketCountry": market, "duration": "1d", "excludeInvestmentCaution": "true", "count": RANK_COUNT}
    result, observed = request(http, token, "/api/v1/rankings", params, deadline, clock, events)
    try:
        items = [
            {"rank": int(item["rank"]), "symbol": item["symbol"], "change_rate": _rate(_object(item.get("price")).get("changeRate"))} for item in result["rankings"]
        ]
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation) as error:
        raise V6Error("V6_TOSS_RESPONSE_INVALID", f"{market} 랭킹 형식 오류({error!r})") from error
    return sorted(items, key=lambda item: item["rank"]), observed


def fetch_instruments(
    http: httpx.Client, token: toss.SharedToken, symbols: list[str], deadline: datetime, clock: Callable[[], datetime], events: list[dict]
) -> dict[str, dict]:
    """종목 정보 {종목: instruments 행}. 시장이 국내·미국이 아닌 종목은 뺀다. 저장은 PRD 04가 한다."""
    if not symbols:
        return {}
    result, observed = request(http, token, "/api/v1/stocks", {"symbols": ",".join(symbols)}, deadline, clock, events)
    infos = {}
    try:
        for item in result:
            market = MARKETS.get(item["market"])
            if market is None:
                continue
            if market == "KR" and not isinstance(item.get("koreanMarketDetail"), dict):
                raise TypeError(f"{item['symbol']}에 koreanMarketDetail이 없다")
            detail = _object(item.get("koreanMarketDetail"))
            infos[item["symbol"]] = {
                "symbol": item["symbol"],
                "name": item["name"],
                "market": market,
                "security_type": item["securityType"],
                "status": item["status"],
                "currency": item["currency"],
                "leverage": item.get("leverageFactor") or "",
                "nxt_supported": int(detail.get("nxtSupported") is True),
                "nxt_suspended": int(detail.get("nxtTradingSuspended") is True),
                "krx_suspended": int(detail.get("krxTradingSuspended") is True),
                "liquidation": int(detail.get("liquidationTrading") is True),
                "observed_at": observed.isoformat(timespec="seconds"),
            }
    except (KeyError, TypeError, AttributeError) as error:
        raise V6Error("V6_TOSS_RESPONSE_INVALID", f"종목 정보 형식 오류({error!r})") from error
    return infos


def eligible(info: dict) -> bool:
    """후보 조건(SPEC 2.2): 주식 또는 배수 ±1 ETF, 상장 중, 국내는 KRX 거래정지·정리매매가 아니다."""
    if info["security_type"] not in ("STOCK", "ETF") or info["status"] != "ACTIVE":
        return False
    if info["security_type"] == "ETF" and _leverage(info) not in (1, -1):
        return False
    return not (info["market"] == "KR" and (info["krx_suspended"] or info["liquidation"]))


def tradable(info: dict, open_at: dict) -> bool:
    """open_at(sessions의 값)에 거래할 수 있는지. 국내 장전·장후는 NXT 지원이고 NXT 거래정지가 아니어야 한다."""
    session = open_at[info["market"]]
    if session is None:
        return False
    if info["market"] == "US":
        return True
    if session == "regular":
        return not info["krx_suspended"]
    return bool(info["nxt_supported"]) and not info["nxt_suspended"]


def candidates(rankings: dict[str, list[dict]], infos: dict[str, dict], holdings: dict[str, int], open_at: dict) -> list[dict]:
    """후보 목록. 시장 KR·US 순으로 랭킹에서 조건에 맞고 거래할 수 있는 종목을 10개까지 고른 뒤, 그 밖의 보유 종목을 종목 코드 순으로 더한다."""
    ranked = {item["symbol"]: item for market in ("KR", "US") for item in rankings.get(market, [])}
    chosen = []
    for market in ("KR", "US"):
        picked = [
            item["symbol"]
            for item in rankings.get(market, [])
            if (info := infos.get(item["symbol"])) and info["market"] == market and eligible(info) and tradable(info, open_at)
        ][:PICKS]
        chosen += [symbol for symbol in picked if symbol not in chosen]
    held = sorted(symbol for symbol, quantity in holdings.items() if quantity > 0 and symbol not in chosen and symbol in infos)
    return [_candidate(infos[symbol], ranked.get(symbol), holdings.get(symbol, 0) > 0, open_at) for symbol in chosen + held]


def _candidate(info: dict, ranking: dict | None, held: bool, open_at: dict) -> dict:
    rate = ranking["change_rate"] if ranking else None
    return {
        "symbol": info["symbol"],
        "name": info["name"],
        "market": info["market"],
        "type": info["security_type"],
        "leverage": _number(_leverage(info)),
        "tradable_now": tradable(info, open_at),
        "exit_only": held and not eligible(info),
        "change_pct": None if rate is None else v3_head.pct(Decimal(rate)),
        "rank": ranking["rank"] if ranking else None,
        "held": held,
    }


def _object(value: object) -> dict:
    """응답의 객체 값. None이면 빈 객체이고, 객체가 아니면 TypeError다."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"객체가 아니다({type(value).__name__})")
    return value


def _rate(value: object) -> str | None:
    """랭킹 changeRate. 없으면 None이고, 글자가 아니면 TypeError, 수로 읽을 수 없으면 InvalidOperation이다."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"changeRate가 글자가 아니다({type(value).__name__})")
    Decimal(value)
    return value


def _leverage(info: dict) -> Decimal | None:
    try:
        return Decimal(info["leverage"])
    except InvalidOperation:
        return None


def _number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _spans(calendar: dict, at: datetime) -> dict | None:
    day = at.astimezone(KST).date()
    kr = calendar.get("KR", {}).get(day.isoformat())
    us = [calendar.get("US", {}).get((day - timedelta(days=back)).isoformat()) for back in (1, 0)]
    if kr is None or None in us:
        return None
    return {"KR": _open(kr, at), "US": next((span for hours in us if (span := _open(hours, at))), None)}


def _open(hours: dict, at: datetime) -> tuple[str, str] | None:
    for name, (start, end) in hours.items():
        if datetime.fromisoformat(start) <= at < datetime.fromisoformat(end):
            return name, end
    return None


def _kst(text: str) -> str:
    return datetime.fromisoformat(text).astimezone(KST).isoformat(timespec="seconds")

"""토스 Open API 토큰 발급과 캔들 조회."""

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

BASE_URL = "https://openapi.tossinvest.com"
KST = timezone(timedelta(hours=9))
IP_HELP = "토스 WTS의 설정 > Open API > 허용 IP 관리에 이 컴퓨터의 공인 IP를 등록해야 한다"
RETRY_WAITS = (1, 2, 4)  # 5xx·네트워크 오류 뒤 다시 보내기 전에 기다리는 초
PRICE_KEYS = ("openPrice", "highPrice", "lowPrice", "closePrice", "volume")
_sleep = time.sleep  # acceptance가 기다린 시간을 기록하려고 바꿔 끼운다


class TossError(Exception):
    """오류 코드와 설명. 인증 정보와 토큰은 담지 않는다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def read_credentials(root: Path) -> tuple[str, str]:
    """환경 변수에서 토스 인증 정보를 읽고, 없으면 저장소 루트의 .env에서 읽는다."""
    values = {}
    if (root / ".env").exists():
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    client_id, client_secret = (
        os.environ.get(key) or values[key] for key in ("TOSS_INVEST_CLIENT_ID", "TOSS_INVEST_CLIENT_SECRET")
    )
    return client_id, client_secret


def issue_token(http: httpx.Client, client_id: str, client_secret: str) -> str:
    """토큰을 발급한다. 같은 키로 먼저 발급된 토큰은 끊긴다."""
    response = _send(
        http,
        "POST",
        "/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
    )
    try:
        return response.json()["access_token"]
    except (ValueError, KeyError, TypeError) as error:
        raise TossError("V1_TOSS_RESPONSE_INVALID", "토큰 응답에 access_token이 없다") from error


def candles(
    http: httpx.Client, token: str, symbol: str, interval: str, adjusted: bool, since: str | None = None
) -> list[dict]:
    """과거로 넘기며 캔들을 받는다. 최신 봉이 먼저 온다.

    받은 봉이 없거나, result.nextBefore가 없거나, since(YYYY-MM-DD)보다 앞선 봉이 나오면 멈춘다.
    """
    params = {"symbol": symbol, "interval": interval, "count": 200, "adjusted": "true" if adjusted else "false"}
    received = []
    while True:
        response = _send(http, "GET", "/api/v1/candles", params=params, headers={"Authorization": f"Bearer {token}"})
        try:
            result = response.json()["result"]
            page, next_before = result["candles"], result.get("nextBefore")
            oldest = min((_kst(candle["timestamp"]).date().isoformat() for candle in page), default=None)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise TossError("V1_TOSS_RESPONSE_INVALID", f"{symbol} {interval} 캔들 응답 형식 오류({error!r})") from error
        received += page
        if not page or not next_before or (since and oldest < since):
            return received
        params["before"] = next_before


def minute_rows(symbol: str, candles: list[dict], since: str, today: str) -> list[tuple]:
    """1분봉을 (종목, 봉이 끝나는 한국 시각, 시가, 고가, 저가, 종가, 거래량)으로 바꾼다. since 전과 today 이후 봉은 버린다."""
    rows = sorted(
        (symbol, at.strftime("%Y-%m-%d %H:%M"), *values)
        for at, values in _parse(symbol, candles)
        if since <= at.date().isoformat() < today
    )
    if len({row[1] for row in rows}) != len(rows):
        raise TossError("V1_TOSS_RESPONSE_INVALID", f"{symbol} 1분봉 시각이 중복된다")
    return rows


def daily_rows(symbol: str, raw: list[dict], adjusted: list[dict], since: str, today: str) -> list[tuple]:
    """원가격과 수정 가격 일봉을 날짜로 합쳐 (종목, 날짜, 시가, 고가, 저가, 종가, 거래량, 수정 종가)로 바꾼다."""

    def by_date(candles: list[dict]) -> dict[str, tuple[int, ...]]:
        parsed = [(at.date().isoformat(), values) for at, values in _parse(symbol, candles)]
        if len({date for date, _ in parsed}) != len(parsed):
            raise TossError("V1_TOSS_RESPONSE_INVALID", f"{symbol} 일봉 날짜가 중복된다")
        return {date: values for date, values in parsed if since <= date < today}

    raw_by_date, adjusted_by_date = by_date(raw), by_date(adjusted)
    if raw_by_date.keys() != adjusted_by_date.keys():
        raise TossError("V1_TOSS_RESPONSE_INVALID", f"{symbol} 원가격과 수정 가격 일봉의 날짜가 다르다")
    return [(symbol, date, *raw_by_date[date], adjusted_by_date[date][3]) for date in sorted(raw_by_date)]


def _parse(symbol: str, candles: list[dict]) -> list[tuple[datetime, tuple[int, ...]]]:
    """봉마다 (한국 시각, (시가, 고가, 저가, 종가, 거래량))을 만든다. 정수로 바꿀 수 없으면 실패한다."""
    try:
        return [(_kst(candle["timestamp"]), tuple(int(candle[key]) for key in PRICE_KEYS)) for candle in candles]
    except (ValueError, KeyError, TypeError) as error:
        raise TossError("V1_TOSS_RESPONSE_INVALID", f"{symbol} 캔들을 읽을 수 없다({error!r})") from error


def _kst(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp).astimezone(KST)


def _send(http: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    """요청을 보내고 오류를 코드로 나눈다.

    429는 Retry-After초 기다려 다시 보내고, 5xx와 네트워크 오류는 1·2·4초 뒤 세 번까지 다시 보낸다.
    """
    waits = iter(RETRY_WAITS)
    while True:
        _sleep(1 / 15)  # 초당 15회 이하
        try:
            response = http.request(method, path, **kwargs)
            failure = f"HTTP {response.status_code}" if response.status_code >= 500 else None
        except httpx.TransportError as error:
            failure = type(error).__name__
        if failure:
            wait = next(waits, None)
            if wait is None:
                raise TossError("V1_TOSS_UNAVAILABLE", f"세 번 다시 보내도 실패했다({failure})")
            _sleep(wait)
            continue
        if response.status_code == 429:
            _sleep(float(response.headers.get("Retry-After", 1)))
            continue
        if response.status_code == 401:
            error = _error(response)
            if isinstance(error, dict) and error.get("code") == "token-revoked":
                raise TossError(
                    "V1_TOKEN_REVOKED",
                    f"{_describe(response)}. 같은 토스 키를 쓰는 다른 프로그램이 토큰을 새로 받았을 수 있다."
                    " 재발급하지 않고 멈춘다",
                )
            raise TossError("V1_TOKEN_INVALID", _describe(response))
        if response.status_code == 403:
            raise TossError("V1_IP_NOT_ALLOWED", f"{_describe(response)}. {IP_HELP}")
        if response.is_error:
            raise TossError("V1_TOSS_RESPONSE_INVALID", _describe(response))
        return response


def _error(response: httpx.Response) -> object:
    """응답 본문의 error. Open API는 code·requestId를 담은 객체, 토큰 발급은 OAuth2 형식의 문자열이다."""
    try:
        return response.json().get("error")
    except (ValueError, AttributeError):
        return None


def _describe(response: httpx.Response) -> str:
    """상태 코드와 오류 코드를 적는다. requestId는 응답에 있을 때만 넣고, 인증 정보와 토큰은 넣지 않는다."""
    error = _error(response)
    if isinstance(error, dict):
        text = f"HTTP {response.status_code} {error.get('code')}"
        return f"{text}, requestId {error['requestId']}" if error.get("requestId") else text
    return f"HTTP {response.status_code} {error}" if error else f"HTTP {response.status_code}"

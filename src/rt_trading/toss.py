"""토스 Open API 토큰 발급과 캔들 조회, 서버의 토스 호출이 함께 쓰는 토큰(v5 PRD 01)."""

import fcntl
import json
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

BASE_URL = "https://openapi.tossinvest.com"
KST = timezone(timedelta(hours=9))
IP_HELP = "토스 WTS의 설정 > Open API > 허용 IP 관리에 이 컴퓨터의 공인 IP를 등록해야 한다"
RETRY_WAITS = (1, 2, 4)  # 5xx·네트워크 오류 뒤 다시 보내기 전에 기다리는 초
PRICE_KEYS = ("openPrice", "highPrice", "lowPrice", "closePrice", "volume")
TOKEN_ERRORS = ("token-revoked", "expired-token", "invalid-token")
TOKEN_MARGIN = timedelta(minutes=10)  # 만료까지 이보다 적게 남은 토큰은 새로 받는다
_sleep = time.sleep  # acceptance가 기다린 시간을 기록하려고 바꿔 끼운다
INTERRUPT: ContextVar[Callable[[], bool] | None] = ContextVar("INTERRUPT", default=None)  # v5 실행기의 새 정지 확인. 없으면 보내기와 기다리기가 이전과 같다
DEADLINE: ContextVar[float | None] = ContextVar("DEADLINE", default=None)  # v5 실행기가 요청 하나의 응답을 기다리는 벽시계 상한(초). 없으면 httpx 시간 제한만 쓴다


class Interrupted(Exception):
    """v5 실행기의 차례 중에 새 정지가 들어와 토스 요청을 보내지 않고 그만뒀다. 토스 오류가 아니다(v5 SPEC 4.6의 5초)."""


class TossError(Exception):
    """오류 코드와 설명. 인증 정보와 토큰은 담지 않는다.

    remote는 응답 본문의 토스 오류 코드, status는 HTTP 상태, retry_after는 429의 Retry-After 초다. 없으면 None이다.
    """

    def __init__(self, code: str, message: str, remote: str | None = None, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.remote, self.status, self.retry_after = code, remote, status, retry_after


class SharedToken:
    """서버의 모든 토스 호출이 함께 쓰는 토큰. 토큰 파일 하나를 잠금 파일로 지킨다(v5 SPEC 5.3).

    str()로 값을 꺼낸다. 파일의 토큰이 만료까지 10분보다 많이 남았으면 그것을 쓰고, 아니면 잠금 안에서 다시 확인한 뒤 새로 받아 저장한다.
    """

    def __init__(self, http: httpx.Client, credentials: tuple[str, str], path: Path, clock: Callable[[], datetime] = lambda: datetime.now(KST)) -> None:
        self.http, self.credentials, self.path, self.clock = http, credentials, path, clock
        self.value: str | None = None

    def __str__(self) -> str:
        if self.value is None:
            saved = self._saved()
            if saved is None:
                with self._lock():
                    saved = self._saved() or self._issue()
            self.value = saved
        return self.value

    def renew(self) -> None:
        """토큰 오류 뒤에 부른다. 파일의 토큰이 쓰던 것과 다르고 아직 쓸 수 있으면 그것으로, 아니면 새로 받아 바꾼다."""
        stale = self.value
        with self._lock():
            saved = self._saved()
            self.value = saved if saved is not None and saved != stale else self._issue()

    def _saved(self) -> str | None:
        """파일의 토큰. 없거나, 읽을 수 없거나, 만료까지 10분 이하로 남았으면 None이다. 읽기 전에 폴더 700·파일 600으로 맞추고, 맞추지 못하면 쓰지 않는다."""
        try:
            os.chmod(self.path.parent, 0o700)
            os.chmod(self.path, 0o600)
            content = json.loads(self.path.read_text(encoding="utf-8"))
            if datetime.fromisoformat(content["expires_at"]) - self.clock() > TOKEN_MARGIN:
                return str(content["access_token"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def _issue(self) -> str:
        """토큰을 받아 임시 파일에 쓴 뒤 바꿔 끼운다. 잠금 안에서만 부른다."""
        issued, content = self.clock(), _token_response(self.http, *self.credentials)
        try:
            expires_at = issued + timedelta(seconds=int(content["expires_in"]))
        except (KeyError, TypeError, ValueError) as error:
            raise TossError("V1_TOSS_RESPONSE_INVALID", "토큰 응답에 expires_in이 없다") from error
        temp = self.path.with_name(self.path.name + ".tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump({"access_token": content["access_token"], "expires_at": expires_at.isoformat()}, file)
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)
        return content["access_token"]

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        with open(self.path.with_name("token.lock"), "a") as file:
            if INTERRUPT.get() is None:
                fcntl.flock(file, fcntl.LOCK_EX)
            else:
                while True:  # v5 실행기: 다른 쪽이 토큰을 받는 동안 기다리며 새 정지를 본다
                    try:
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        _check()
                        _sleep(0.2)
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)


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
    return _token_response(http, client_id, client_secret)["access_token"]


def _token_response(http: httpx.Client, client_id: str, client_secret: str) -> dict:
    """토큰 발급 응답 본문. access_token이 없으면 V1_TOSS_RESPONSE_INVALID다."""
    response = _send(
        http,
        "POST",
        "/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
    )
    try:
        content = response.json()
        return content | {"access_token": content["access_token"]}
    except (ValueError, KeyError, TypeError) as error:
        raise TossError("V1_TOSS_RESPONSE_INVALID", "토큰 응답에 access_token이 없다") from error


def candles(
    http: httpx.Client, token: "str | SharedToken", symbol: str, interval: str, adjusted: bool, since: str | None = None
) -> list[dict]:
    """과거로 넘기며 캔들을 받는다. 최신 봉이 먼저 온다.

    받은 봉이 없거나, result.nextBefore가 없거나, since(YYYY-MM-DD)보다 앞선 봉이 나오면 멈춘다.
    """
    params = {"symbol": symbol, "interval": interval, "count": 200, "adjusted": "true" if adjusted else "false"}
    shared = token if isinstance(token, SharedToken) else None
    received = []
    while True:
        response = _send(http, "GET", "/api/v1/candles", shared, params=params, headers={"Authorization": f"Bearer {token}"})
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


def _check() -> None:
    """INTERRUPT가 있고 참이면 Interrupted다. 요청을 보내기 직전과 기다리는 동안 1초마다 본다."""
    interrupt = INTERRUPT.get()
    if interrupt is not None and interrupt():
        raise Interrupted("정지가 들어와 토스 요청을 보내지 않는다")


def _request(http: httpx.Client, method: str, path: str, kwargs: dict) -> httpx.Response:
    """요청 하나. DEADLINE이 있으면 따로 보내고 응답을 그 초(벽시계)까지만 기다린다. 넘으면 더 기다리지 않고 네트워크 오류(httpx.TimeoutException)다.

    보낸 요청은 끝날 때까지 따로 돌고 그 응답은 쓰지 않는다. 단계별(연결·쓰기·읽기) httpx 시간 제한만으로는 기다림이 그 안에 끝난다고 할 수 없어서다(v5 SPEC 4.6의 5초).
    """
    deadline = DEADLINE.get()
    if deadline is None:
        return http.request(method, path, **kwargs)
    outcome: list = []

    def attempt() -> None:
        try:
            outcome.append(http.request(method, path, **kwargs))
        except BaseException as error:  # 따로 보낸 요청의 오류는 부른 쪽에서 다시 낸다
            outcome.append(error)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(deadline)
    if not outcome:
        raise httpx.TimeoutException(f"{deadline:g}초 안에 응답이 끝나지 않았다")
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    return outcome[0]


def _wait(seconds: float) -> None:
    """다시 보내기 전에 seconds초 기다린다. INTERRUPT가 있으면 1초마다 본다."""
    if INTERRUPT.get() is None:
        _sleep(seconds)
        return
    while seconds > 0:
        _check()
        _sleep(min(seconds, 1))
        seconds -= 1


def _send(
    http: httpx.Client, method: str, path: str, token: SharedToken | None = None, retry: bool = True, **kwargs
) -> httpx.Response:
    """요청을 보내고 오류를 코드로 나눈다.

    retry면 429는 Retry-After초 기다려 다시 보내고, 5xx와 네트워크 오류는 1·2·4초 뒤 세 번까지 다시 보낸다.
    token(SharedToken)을 주면 보낼 때마다 그 값을 Authorization 헤더에 넣고, 토큰 오류를 받으면 토큰을 바꿔 한 번 다시 보낸다.
    retry가 거짓이면 한 번만 보낸다(v5 주문 생성). 토큰 오류면 토큰을 바꿔 두기만 하고 오류를 낸다.
    INTERRUPT(v5 실행기)가 참이면 보내기 직전과 다시 보내기를 기다리는 동안 Interrupted로 그만둔다. DEADLINE이 있으면 요청 하나를 그 초까지만 기다린다.
    """
    waits, renewed = iter(RETRY_WAITS), False
    while True:
        if token is not None:
            kwargs["headers"] = {**kwargs.get("headers", {}), "Authorization": f"Bearer {token}"}
        _sleep(1 / 15)  # 초당 15회 이하
        _check()
        try:
            response = _request(http, method, path, kwargs)
            failure = f"HTTP {response.status_code}" if response.status_code >= 500 else None
        except httpx.TransportError as error:
            response, failure = None, type(error).__name__
        if failure:
            wait = next(waits, None) if retry else None
            if wait is None:
                status = response.status_code if response is not None else None
                message = f"세 번 다시 보내도 실패했다({failure})" if retry else f"보내지 못했다({failure})"
                raise TossError("V1_TOSS_UNAVAILABLE", message, _remote(response) if response is not None else None, status)
            _wait(wait)
            continue
        if response.status_code == 429:
            seconds = float(response.headers.get("Retry-After", 1))
            if not retry:
                raise TossError("V5_TOSS_RATE_LIMITED", f"요청 한도를 넘었다({seconds:g}초 뒤 다시)", _remote(response), 429, seconds)
            _wait(seconds)
            continue
        if response.status_code == 401:
            remote = _remote(response)
            if token is not None and remote in TOKEN_ERRORS and not renewed:
                token.renew()
                renewed = True
                if retry:
                    continue
            if remote == "token-revoked":
                raise TossError(
                    "V1_TOKEN_REVOKED",
                    f"{_describe(response)}. 토큰을 바꿔 다시 보냈는데도 끊겼다"
                    if renewed and retry
                    else f"{_describe(response)}. 같은 토스 키를 쓰는 다른 프로그램이 토큰을 새로 받았을 수 있다. 재발급하지 않고 멈춘다",
                    remote,
                    401,
                )
            raise TossError("V1_TOKEN_INVALID", _describe(response), remote, 401)
        if response.status_code == 403:
            raise TossError("V1_IP_NOT_ALLOWED", f"{_describe(response)}. {IP_HELP}", _remote(response), 403)
        if response.is_error:
            raise TossError("V1_TOSS_RESPONSE_INVALID", _describe(response), _remote(response), response.status_code)
        return response


def _remote(response: httpx.Response) -> str | None:
    """응답 본문의 토스 오류 코드(Open API는 error.code, 토큰 발급은 OAuth2 error 문자열)."""
    error = _error(response)
    if isinstance(error, dict):
        return error.get("code")
    return error if isinstance(error, str) else None


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

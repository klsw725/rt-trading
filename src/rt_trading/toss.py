"""토스 Open API 토큰 발급과 일봉 조회."""

import time
from pathlib import Path

import httpx

BASE_URL = "https://openapi.tossinvest.com"
IP_HELP = "토스 WTS의 설정 > Open API > 허용 IP 관리에 이 컴퓨터의 공인 IP를 등록해야 한다"


class TossError(Exception):
    pass


def read_credentials(root: Path) -> tuple[str, str]:
    """저장소 루트의 .env에서 토스 인증 정보를 읽는다."""
    values = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values["TOSS_INVEST_CLIENT_ID"], values["TOSS_INVEST_CLIENT_SECRET"]


def issue_token(http: httpx.Client, client_id: str, client_secret: str) -> str:
    """토큰을 발급한다. 같은 키로 먼저 발급된 토큰은 끊긴다."""
    response = http.post(
        "/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
    )
    if response.status_code == 403:
        raise TossError(f"토큰 발급이 거부됐다({_describe(response)}). {IP_HELP}")
    if response.is_error:
        raise TossError(f"토큰 발급 실패({_describe(response)})")
    return response.json()["access_token"]


def daily_candles(http: httpx.Client, token: str, symbol: str, adjusted: bool) -> list[dict]:
    """토스가 주는 가장 이른 날까지 과거로 넘기며 일봉을 받는다. 최신 봉이 먼저 온다."""
    params = {"symbol": symbol, "interval": "1d", "count": 200, "adjusted": "true" if adjusted else "false"}
    candles = []
    while True:
        result = _get(http, token, "/api/v1/candles", params)["result"]
        candles.extend(result["candles"])
        if not result["candles"] or not result.get("nextBefore"):
            return candles
        params["before"] = result["nextBefore"]


def _get(http: httpx.Client, token: str, path: str, params: dict) -> dict:
    while True:
        time.sleep(1 / 15)  # 초당 15회 이하
        response = http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 429:
            time.sleep(float(response.headers.get("Retry-After", 1)))
            continue
        if response.status_code == 401:
            raise TossError(
                f"토큰이 거부됐다({_describe(response)}). 같은 토스 키를 쓰는 다른 프로그램이"
                " 토큰을 새로 받았을 수 있다. 재발급하지 않고 멈춘다"
            )
        if response.status_code == 403:
            raise TossError(f"조회가 거부됐다({_describe(response)}). {IP_HELP}")
        if response.is_error:
            raise TossError(f"조회 실패({_describe(response)})")
        return response.json()


def _describe(response: httpx.Response) -> str:
    """오류 응답을 상태 코드와 오류 코드로 줄인다. 인증 정보와 토큰은 넣지 않는다."""
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    if isinstance(error, dict):  # Open API 형식
        return f"HTTP {response.status_code} {error.get('code')}, requestId {error.get('requestId')}"
    return f"HTTP {response.status_code} {error}"  # 토큰 발급의 OAuth2 형식

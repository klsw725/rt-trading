"""토스 실계좌 조회와 주문, 주문 생성 오류 분류(v5 SPEC 4.3, PRD 01)."""

from decimal import Decimal, InvalidOperation

import httpx

from rt_trading import toss

SYMBOLS = ("069500", "114800")
FINAL = ("FILLED", "CANCELED", "REJECTED")
STOP_CODES = {
    "forbidden",
    "prerequisite-required",
    "investor-exchange-not-integrated",
    "account-restricted",
    "account-header-required",
    "login-user-not-found",
    "idempotency-key-conflict",
    "opposite-pending-order-exists",
    "insufficient-sellable-quantity",
}
TOKEN_CODES = ("V1_TOKEN_REVOKED", "V1_TOKEN_INVALID")
Token = str | toss.SharedToken


def accounts(http: httpx.Client, token: Token) -> list[dict]:
    """계좌 목록. 계좌번호는 끝 네 자리(tail)만 넘긴다."""
    result = _get(http, token, "/api/v1/accounts")
    try:
        return [{"seq": int(item["accountSeq"]), "type": item["accountType"], "tail": str(item["accountNo"])[-4:]} for item in result]
    except (KeyError, TypeError, ValueError) as error:
        raise _invalid("계좌 목록", error) from error


def holdings(http: httpx.Client, token: Token, account: int) -> dict[str, int]:
    """두 종목의 보유 수량. 없으면 0이다."""
    result = _get(http, token, "/api/v1/holdings", account)
    found = dict.fromkeys(SYMBOLS, 0)
    try:
        for item in result["items"]:
            if item["symbol"] in found:
                found[item["symbol"]] = _whole(item["quantity"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise _invalid("보유 주식", error) from error
    return found


def buying_power(http: httpx.Client, token: Token, account: int) -> int:
    """원화 현금 매수 가능 금액(미수 미발생 기준)."""
    result = _get(http, token, "/api/v1/buying-power", account, {"currency": "KRW"})
    try:
        return _whole(result["cashBuyingPower"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise _invalid("매수 가능 금액", error) from error


def commission_rate(http: httpx.Client, token: Token, account: int) -> Decimal:
    """국내 주식 매매 수수료율."""
    result = _get(http, token, "/api/v1/commissions", account)
    try:
        return next(Decimal(item["commissionRate"]) for item in result if item["marketCountry"] == "KR")
    except (KeyError, TypeError, ValueError, InvalidOperation, StopIteration) as error:
        raise _invalid("매매 수수료", error) from error


def price_limits(http: httpx.Client, token: Token, symbol: str) -> tuple[int, int]:
    """그 종목의 오늘 (상한가, 하한가)."""
    result = _get(http, token, "/api/v1/price-limits", None, {"symbol": symbol})
    try:
        return _whole(result["upperLimitPrice"]), _whole(result["lowerLimitPrice"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise _invalid("상·하한가", error) from error


def create_order(http: httpx.Client, token: Token, account: int, client_order_id: str, symbol: str, side: str, time_in_force: str, quantity: int) -> str:
    """시장가 주문을 한 번만 보내고 토스 주문 id를 돌려준다. 다시 보낼지는 부르는 쪽이 정한다(PRD 01)."""
    body = {"clientOrderId": client_order_id, "symbol": symbol, "side": side, "orderType": "MARKET", "timeInForce": time_in_force, "quantity": str(quantity)}
    response = toss._send(http, "POST", "/api/v1/orders", _shared(token), retry=False, json=body, headers=_headers(token, account))
    try:
        return str(response.json()["result"]["orderId"])
    except (ValueError, KeyError, TypeError) as error:
        raise _invalid("주문 생성", error) from error


def cancel_order(http: httpx.Client, token: Token, account: int, order_id: str) -> None:
    """취소를 요청한다. 주문이 끝났는지는 알리지 않는다(상세 조회로 정한다)."""
    toss._send(http, "POST", f"/api/v1/orders/{order_id}/cancel", _shared(token), headers=_headers(token, account))


def order(http: httpx.Client, token: Token, account: int, order_id: str) -> dict:
    """주문 상세. 체결 금액·평균 체결가·수수료·세금은 비어 있으면 None이다."""
    result = _get(http, token, f"/api/v1/orders/{order_id}", account)
    try:
        execution = result["execution"]
        return {
            "status": result["status"],
            "symbol": result["symbol"],
            "side": result["side"],
            "quantity": _whole(result["quantity"]),
            "filled_quantity": _whole(execution["filledQuantity"]),
            "filled_amount": _number(execution.get("filledAmount")),
            "average_price": _number(execution.get("averageFilledPrice")),
            "commission": _number(execution.get("commission")),
            "tax": _number(execution.get("tax")),
        }
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise _invalid("주문 상세", error) from error


def open_orders(http: httpx.Client, token: Token, account: int, symbol: str) -> list[dict]:
    """그 종목의 진행 중 주문(토스 주문 id, 방향, 수량, 상태)."""
    result = _get(http, token, "/api/v1/orders", account, {"status": "OPEN", "symbol": symbol})
    try:
        return [{"order_id": str(item["orderId"]), "side": item["side"], "quantity": _whole(item["quantity"]), "status": item["status"]} for item in result["orders"]]
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise _invalid("주문 목록", error) from error


def is_final(status: str) -> bool:
    """끝난 주문은 상세 조회 상태가 FILLED·CANCELED·REJECTED인 주문뿐이다."""
    return status in FINAL


def classify(error: toss.TossError, token_errors_before: int = 0) -> str:
    """주문 생성 오류를 retry·stop·reject로 나눈다(SPEC 4.3). token_errors_before는 같은 낼 주문에서 앞서 이어진 토큰 오류 수다."""
    if error.code in TOKEN_CODES:
        return "stop" if token_errors_before else "retry"
    if error.code in ("V1_TOSS_UNAVAILABLE", "V5_TOSS_RATE_LIMITED") or error.remote == "request-in-progress":
        return "retry"
    if error.code == "V1_TOSS_RESPONSE_INVALID" and error.status and 400 <= error.status < 500 and error.remote not in STOP_CODES:
        return "reject"
    return "stop"


def _get(http: httpx.Client, token: Token, path: str, account: int | None = None, params: dict | None = None) -> object:
    response = toss._send(http, "GET", path, _shared(token), params=params, headers=_headers(token, account))
    try:
        return response.json()["result"]
    except (ValueError, KeyError, TypeError) as error:
        raise _invalid(path, error) from error


def _headers(token: Token, account: int | None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if account is not None:
        headers["X-Tossinvest-Account"] = str(account)
    return headers


def _shared(token: Token) -> toss.SharedToken | None:
    return token if isinstance(token, toss.SharedToken) else None


def _whole(value: object) -> int:
    """정수 값. 소수가 있으면 ValueError다."""
    number = Decimal(str(value))
    if number != number.to_integral_value():
        raise ValueError("정수가 아니다")
    return int(number)


def _number(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _invalid(what: str, error: Exception) -> toss.TossError:
    """응답 형식 오류. 값(계좌번호 등)이 섞이지 않게 오류 종류만 적는다."""
    return toss.TossError("V5_TOSS_RESPONSE_INVALID", f"{what} 응답 형식 오류({type(error).__name__})")

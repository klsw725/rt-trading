# PRD: v5 PRD 01 토스 토큰·계좌·주문
> **상태**: 승인(2026-11-04, 사용자가 정한 대로 오라클 검토 통과로 승인)
> 상위 SPEC: [v5 SPEC](../SPEC.md)

## 의존성

- v1 토스 클라이언트(`toss.py`의 `read_credentials`, `issue_token`, `candles`, `_send`, `TossError`)와 그것을 쓰는 v1 `fetch`(`cli.run_fetch`), `v3-us-fetch`(`v3_us.run`), v4 당일 1분봉 받기(`v4_daily.fetch_bars`)
- 토스 Open API 공식 문서 1.2.28(2026-11-04 확인). 토큰 응답은 `access_token`, `token_type`, `expires_in`(만료까지 남은 초)이고 refresh token은 없다. client당 유효한 토큰은 하나이고 다시 받으면 앞 토큰은 바로 끊긴다.

## 문제

토스 키가 하나뿐인데, 지금은 명령마다 토큰을 새로 받아 앞서 돌던 명령의 토큰을 끊는다. 실행기는 장중 내내 주문하고 조회해야 하므로 서버의 모든 토스 호출이 토큰 하나를 함께 써야 한다(SPEC 5.3).
또 실계좌 조회와 주문을 부를 클라이언트와, 실행기가 오류마다 할 일을 정할 오류 분류(SPEC 4.3)가 없다.

## 목표

1. 토큰 파일 하나를 잠금으로 지키며 함께 쓰고, 토큰 오류를 받으면 한 번만 바꿔 다시 보낸다.
2. 실제 `fetch`, `v3-us-fetch`, 당일 1분봉 받기 명령이 함께 쓰는 토큰을 쓴다.
3. 계좌 목록, 보유, 매수 가능 금액, 수수료율, 상·하한가를 조회한다.
4. 주문 생성·취소·상세 조회·진행 중 목록 조회를 하고, 주문 생성 오류를 SPEC 4.3 표대로 나눈다.

## 범위 밖

- 언제 무엇을 몇 주 주문할지, 낼 주문 기록과 다시 보내기 판단, 8분 규칙, 장부(PRD 03)
- 계좌번호를 화면에 보이는 방식(`v5-check`, PRD 03). 이 PRD는 끝 네 자리만 넘긴다.

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/toss.py` | 함께 쓰는 토큰(`SharedToken`), 토큰 오류 뒤 한 번 다시 보내기, `TossError`에 토스 오류 코드 |
| `src/rt_trading/toss_account.py` | 계좌·보유·매수 가능 금액·수수료율·상하한가 조회, 주문 생성·취소·상세·목록, 오류 분류 |
| `src/rt_trading/cli.py` | `fetch`, `v3-us-fetch`가 함께 쓰는 토큰을 넘긴다 |
| `src/rt_trading/v3_us.py` | `run`에 선택 인자 `token` |
| `src/rt_trading/v4_daily.py` | `fetch_bars`가 함께 쓰는 토큰을 쓴다 |
| `src/rt_trading/v5_acceptance.py` | PRD 01 사례 |
| `docs/specs/v5/fixtures/prd01-toss.json` | 가짜 토스 응답, 손으로 적은 요청과 기대 |

## 함께 쓰는 토큰

- 토큰 파일은 `data/toss/token.json`(`{"access_token": ..., "expires_at": ISO 8601}`), 잠금 파일은 `data/toss/token.lock`이다. 폴더는 소유자만 열고, 파일은 소유자만 읽고 쓴다(700, 600). 파일은 같은 폴더의 임시 파일에 쓴 뒤 바꿔 끼운다.
- `SharedToken(http, credentials, path, clock)`은 토큰 값을 꺼낼 때 파일의 토큰이 있고 만료까지 10분보다 많이 남았으면 그 값을 쓴다. 아니면 잠금(`fcntl.flock`)을 잡고 파일을 다시 읽어, 그래도 없거나 10분 이하로 남았으면 토큰을 받아(`expires_at` = 받은 시각 + `expires_in`) 저장한다.
- 토큰 오류(401의 `token-revoked`, `expired-token`, `invalid-token`)를 받으면 잠금을 잡고 파일을 다시 읽는다. 쓰던 토큰과 다르면 그것으로, 같으면 새로 받아 저장한 것으로 바꿔 그 요청을 한 번 다시 보낸다. 다시 보낸 요청도 토큰 오류면 v1처럼 `V1_TOKEN_REVOKED` 또는 `V1_TOKEN_INVALID`다.
- `toss.candles`와 `_send`는 토큰으로 문자열과 `SharedToken`을 모두 받는다. 문자열이면 지금과 같고(다시 보내지 않는다), `SharedToken`이면 위처럼 한 번 다시 보낸다. 주문 생성은 예외다(아래 "주문 생성은 한 번만 보낸다").
- `cli.run_fetch`와 `v3_us.run`에 선택 인자 `token`을 더한다. 주지 않으면 지금처럼 토큰을 받는다(v1 acceptance가 이 경로를 시험한다). `fetch`·`v3-us-fetch` 명령은 `SharedToken`을 넘긴다. `v4_daily.fetch_bars`는 `SharedToken`을 쓴다.
- 토큰 값과 인증 정보는 출력, 기록, 오류 문구에 쓰지 않는다.

## 계좌와 주문 클라이언트

`toss_account.py`의 함수는 모두 `http`, `token`(문자열 또는 `SharedToken`)을 받고, 계좌가 필요한 API는 `account`(`accountSeq`)를 받아 `X-Tossinvest-Account` 헤더로 보낸다.

| 함수 | API | 돌려주는 것 |
| --- | --- | --- |
| `accounts` | `GET /api/v1/accounts` | `[{"seq": accountSeq, "type": accountType, "tail": 계좌번호 끝 네 자리}]`. 계좌번호 전체는 돌려주지 않는다 |
| `holdings` | `GET /api/v1/holdings` | 두 종목의 보유 수량(`{"069500": n, "114800": n}`, 없으면 0) |
| `buying_power` | `GET /api/v1/buying-power?currency=KRW` | `cashBuyingPower`(원, 정수) |
| `commission_rate` | `GET /api/v1/commissions` | `marketCountry`가 `KR`인 `commissionRate`(Decimal) |
| `price_limits` | `GET /api/v1/price-limits?symbol=` | (상한가, 하한가) 정수 |
| `create_order` | `POST /api/v1/orders` | 토스 주문 id(`orderId`). 본문은 `clientOrderId`, `symbol`, `side`, `orderType`(`MARKET`), `timeInForce`(`DAY` 또는 `OPG`), `quantity`(정수 문자열)만 보낸다 |
| `cancel_order` | `POST /api/v1/orders/{orderId}/cancel` | 없음. 주문 상태를 알리지 않는다 |
| `order` | `GET /api/v1/orders/{orderId}` | 상태, 종목, 방향, 수량, 체결 수량, 체결 금액, 평균 체결가, 수수료, 세금(비어 있으면 `None`) |
| `open_orders` | `GET /api/v1/orders?status=OPEN&symbol=` | 그 종목의 진행 중 주문 id와 방향·수량·상태 |

- 응답 형식이 맞지 않으면 `V5_TOSS_RESPONSE_INVALID`다.

### 주문 생성은 한 번만 보낸다

- `create_order`는 요청을 정확히 한 번 보낸다. `_send`에 선택 인자 `retry`(기본 참)를 더하고, 주문 생성은 거짓으로 부른다. 거짓이면 네트워크 오류·5xx·429·토큰 오류 뒤에 스스로 다시 보내지 않고 바로 `TossError`를 낸다. 다시 보낼지는 실행기가 정지 상태와 8분을 보고 정한다(PRD 03).
- 이때 `TossError`에는 HTTP 상태, 토스 오류 코드, 429의 `Retry-After` 초가 있다. 토큰 오류면 `SharedToken`을 바꿔 두기만 하고(다음 보내기가 새 토큰을 쓴다) 다시 보내지 않는다.
- 주문 생성 말고는 지금처럼 `_send`가 다시 보낸다.
- 끝난 주문은 상세 조회 상태가 `FILLED`·`CANCELED`·`REJECTED`인 주문뿐이다(`is_final`). 취소 응답은 성공이든 오류든 주문을 끝내지 않는다.
- `TossError`에 토스 오류 코드(`error.code` 응답 값, 없으면 `None`)를 담는 속성을 더한다. 기존 오류 코드와 문구는 그대로다.

## 오류 분류

`classify(error)`는 주문 생성 오류를 SPEC 4.3 표대로 `retry`, `stop`, `reject` 중 하나로 나눈다.

| 나눔 | 오류 |
| --- | --- |
| `retry` | 네트워크 오류, 시간 초과, 5xx(`internal-error`, `maintenance` 포함), 429(`Retry-After` 초를 함께 넘긴다), `request-in-progress`, 토큰 오류(`SharedToken`을 바꾼 뒤) |
| `stop` | 403(`forbidden`, 허용 IP), `prerequisite-required`, `investor-exchange-not-integrated`, `account-restricted`, `account-header-required`, `login-user-not-found`, `idempotency-key-conflict`, `opposite-pending-order-exists`, `insufficient-sellable-quantity`, 같은 낼 주문에서 두 번 이어진 토큰 오류, 응답 형식 오류 |
| `reject` | 그 밖의 4xx(`insufficient-buying-power`, `order-hours-closed`, `stock-restricted`, `invalid-request` 등) |

## 가상 fixture

- 가짜 토스는 `httpx.MockTransport`로, 경로·방법별로 미리 적은 응답(상태 코드, 본문, 헤더)을 차례로 돌려주고 요청(경로, 쿼리, 헤더, 본문)을 기록한다.
- 토큰 사례는 임시 폴더의 토큰 파일로 두 `SharedToken`(두 프로세스 대신)을 만든다. 가짜 시계로 만료 10분 전·후를 만든다. 동시에 만료된 두 쪽이 차례로 잠금을 잡는 사례는 스레드 두 개로 한다.
- 인증 정보, 토큰 값, 계좌번호는 fixture의 가짜 값이고, 사례가 끝나면 모든 출력과 저장 파일에서 이 값을 찾는다. 토큰 값은 `data/toss/token.json`에만 있어야 한다.
- 명령 경로 사례는 `httpx.Client`를 가짜 토스를 쓰는 것으로 바꿔 끼우고 `cli.fetch`, `cli.fetch_us`, `v4_daily.fetch_bars`를 그대로 부른다. 임시 루트에 유효한 토큰 파일과 가짜 `.env`를 둔다.

## 필수 Acceptance

SPEC 9절의 "토큰과 주문" 항목을 맡는다.

- 계좌·보유·매수 가능 금액·수수료율·상하한가 조회와 주문 생성·취소·상세·목록이 정해 둔 주소, 쿼리, 본문, 두 헤더(`Authorization`, `X-Tossinvest-Account`)로 간다. 계좌 목록은 끝 네 자리만 넘긴다. 가짜 인증 정보와 계좌번호 전체는 출력과 저장 파일 어디에도 없고, 토큰 값은 `token.json` 말고는 없다.
- 함께 쓰는 토큰: 파일의 토큰이 10분보다 많이 남았으면 받지 않고 쓰고, 두 쪽이 동시에 만료를 보면 토큰을 한 번만 받는다. `token-revoked`를 받으면 파일의 새 토큰으로 한 번 다시 보내고, 파일도 같은 토큰이면 새로 받아 다시 보내며, 다시 보낸 것도 토큰 오류면 `V1_TOKEN_REVOKED`다. 파일과 폴더 권한이 600·700이다.
- 명령 경로: 유효한 토큰 파일이 있을 때 `cli.fetch`, `cli.fetch_us`, `v4_daily.fetch_bars`가 토큰 발급 요청 없이 저장된 토큰으로 캔들을 받는다. `cli.run_fetch`에 `token`을 넘기지 않으면 v1과 같다(v1 acceptance가 그대로 통과한다).
- 주문 생성은 네트워크 오류·5xx·429·토큰 오류에서 요청을 한 번만 보내고 오류를 낸다(429면 `Retry-After` 초가 담긴다). 주문 생성 오류가 위 표대로 나뉜다(표의 각 줄에서 하나 이상). 취소 응답(성공, `already-filled`, `already-canceled`, `cancel-restricted`, `order-hours-closed`)은 주문을 끝내지 않고, `is_final`은 `FILLED`·`CANCELED`·`REJECTED`에서만 참이다.

## 완료 조건

- `uv run rt-trading v5-acceptance --prd 01`이 exit 0이다.
- `toss.py`와 세 받기 경로를 바꾸므로 `uv run rt-trading acceptance`, `v0-acceptance`, `v2-acceptance`, `v3-acceptance`, `v4-acceptance`, `web-acceptance`가 그대로 통과한다.
- 서버 배포는 v4 운용이 끝난 뒤 PRD 05에서 한다.

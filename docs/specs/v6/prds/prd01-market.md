# PRD: v6 PRD 01 시장 세션과 후보 종목
> **상태**: 승인(2026-11-09, 사용자가 정한 대로 오라클 검토 통과로 승인)
> 상위 SPEC: [v6 SPEC](../SPEC.md)

## 의존성

- 토스 클라이언트(`toss._send`, `toss.SharedToken`, `toss.TossError`, `toss.Interrupted`, `toss.INTERRUPT`, `toss.KST`)와 서버의 함께 쓰는 토큰(`cli.shared_token`, `data/toss/token.json`)
- 토스 Open API 1.2.28(2026-11-09 확인). 이 PRD가 쓰는 API와 값은 다음과 같다. 요청 한도는 묶음(Rate Limits Group)마다 따로이고, 429에는 `Retry-After`가 온다.

| API | 쓰는 값 |
| --- | --- |
| `GET /api/v1/market-calendar/KR?date=` | `result`의 `previousBusinessDay`, `today`, `nextBusinessDay`마다 `date`(한국 날짜)와 `integrated`의 `preMarket`·`regularMarket`·`afterMarket`의 `startTime`·`endTime`(한국 시각). 휴장일은 `integrated`가 `null`이다 |
| `GET /api/v1/market-calendar/US?date=` | 같은 세 날마다 `date`(미국 현지 날짜)와 `dayMarket`·`preMarket`·`regularMarket`·`afterMarket`의 `startTime`·`endTime`(한국 시각). 휴장일은 넷 다 `null`이다 |
| `GET /api/v1/rankings` | `type=MARKET_TRADING_AMOUNT`, `marketCountry`(`KR`·`US`), `duration=1d`, `excludeInvestmentCaution=true`, `count=50`. `result.rankings`의 `rank`, `symbol`, `price.changeRate`(소수 비율 문자열, 없으면 `null`), `price.lastPrice`(현재가 문자열) |
| `GET /api/v1/stocks?symbols=` | 쉼표로 이은 최대 200개. 종목마다 `symbol`, `name`, `market`, `securityType`, `status`, `currency`, `leverageFactor`(문자열, 없으면 `null`), `koreanMarketDetail`(`liquidationTrading`, `nxtSupported`, `krxTradingSuspended`, `nxtTradingSuspended`. 미국 종목은 `null`) |

## 문제

v6는 국내·미국 세션이 날마다 다르고(휴장일, 단축 거래, 서머타임), 국내 장전·장후에는 NXT 지원 종목만 거래된다. 판단 차례와 거래 가능 여부를 체결 시각 E로 정해야 한다.
후보는 차례마다 랭킹에서 새로 만들되 레버리지 상품 같은 금지 상품을 거르고, 지금 보유 종목은 조건을 어겨도 팔 수 있게 넣어야 한다.
토스 요청은 v5와 같은 키·토큰을 쓰므로 429에 매달리지 않고, 그 일의 마감 뒤에는 다시 보내거나 늦은 응답을 쓰지 않아야 한다.

## 목표

1. v6의 토스 요청 함수(다시 보내기 규칙, 마감, 429 알림)를 만든다.
2. 장 운영 정보를 받아 합치고, 시각마다의 세션, 국내 거래일, 하루의 판단 차례, 검증 구간 끝 시각을 정한다.
3. 종목 정보를 받고, 조건과 거래 가능 여부로 후보 종목(`tradable_now`, `exit_only`)을 만든다.
4. `v6-acceptance [--prd NN]` 실행기를 만든다. PRD 05까지 모든 PRD를 실행하게 완성한다.

## 범위 밖

- 헤드 입력의 꼴, C까지 받은 값 고르기, Codex(PRD 02), 현재가·호가·환율 받기와 평가·체결(PRD 03)
- 언제 무엇을 부를지와 일의 마감(C, E + 40초 등), 받은 값과 429의 저장, 장부 잠금(PRD 04)

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/toss.py` | 429 뒤 다시 보내는 횟수 상한과 429 알림. 둘 다 `ContextVar`이고, 걸지 않으면 지금과 같다 |
| `src/rt_trading/v6_market.py` | 토스 요청 함수, 장 운영 정보, 세션, 국내 거래일, 판단 차례, 끝 시각, 종목 정보, 후보 |
| `src/rt_trading/v6_acceptance.py` | PRD별 사례 등록·실행, 결과 출력 |
| `src/rt_trading/cli.py` | `v6-acceptance [--prd NN]` |
| `docs/specs/v6/fixtures/prd01-market.json` | 가짜 장 운영 정보·랭킹·종목 정보 응답과 손으로 정한 기대 |

- `v6-acceptance`는 v5와 같은 JSON으로 사례별 결과와 요약을 출력한다. 실행한 사례가 모두 통과하면 exit 0, 아니면 exit 1이다. `--prd`가 없으면 구현된 모든 v6 PRD를 실행한다.

## 토스 요청

- `toss.py`에 `RATE_LIMIT_RETRIES`(429 뒤 다시 보내는 횟수 상한. 없으면 지금처럼 `retry`일 때 끝없이 다시 보낸다)와 `ON_RATE_LIMIT`(429를 받을 때마다 경로를 넘겨 부르는 함수. 없으면 부르지 않는다)를 `ContextVar`로 더한다. `_send`는 429를 받으면 `ON_RATE_LIMIT`을 부르고, 이 요청에서 429 뒤 다시 보낸 횟수가 상한에 이르렀으면 다시 보내지 않고 `TossError`를 낸다. 토큰 발급 요청도 같은 `_send`를 거치므로 같은 규칙을 따른다. v0~v5는 두 값을 걸지 않으므로 동작이 그대로다.
- `request(http, token, path, params, deadline, clock, events)`는 GET 요청 하나를 보내 `(result, observed_at)`을 돌려준다. `token`은 `SharedToken`, `result`는 응답 본문의 `result`, `observed_at`은 응답을 받은 시각이다. 저장소에는 아무것도 쓰지 않는다.
- `toss.INTERRUPT`에 "지금이 `deadline`과 같거나 늦다", `RATE_LIMIT_RETRIES`에 1, `ON_RATE_LIMIT`에 `events`에 `{"at": 받은 시각, "path": 경로}`를 더하는 함수를 걸고 `toss._send(http, "GET", path, token, params=params)`를 부른다. 그래서 다시 보내기는 아래와 같다.

| 받은 것 | 할 일 |
| --- | --- |
| 네트워크 오류, 시간 초과, 5xx | 1, 2, 4초 뒤 세 번까지 다시 보낸다 |
| 토큰 오류 | 함께 쓰는 토큰 절차로 토큰을 바꾸고 한 번 다시 보낸다. 토큰 발급 요청에도 이 표가 걸리고, 그 429는 경로 `/oauth2/token`으로 알린다 |
| 429 | 알리고, 처음이면 `Retry-After`초(없으면 1초) 뒤 한 번 다시 보내며, 두 번째면 그만둔다 |
| 그 밖의 오류 | 그만둔다 |

- 마감이 지나면 보내기 직전, 다시 보내기를 기다리는 동안, 토큰 잠금을 기다리는 동안 그만둔다(`toss.Interrupted`). 응답을 받은 시각이 `deadline`보다 늦으면 그 응답은 버린다. `deadline`과 같은 시각에 받은 응답은 쓴다.
- 그만두거나 버리면 `V6_TOSS_FAILED`를 낸다. 설명에는 마지막 오류 코드와 HTTP 상태를 적고, 인증 정보와 토큰은 적지 않는다. `result`가 없거나 형식이 틀리면 `V6_TOSS_RESPONSE_INVALID`다.
- v6가 쓰는 `httpx.Client`의 시간 제한은 10초다.

## 장 운영 정보

- `fetch_calendar(market, day)`는 국내면 `date=day`, 미국이면 `date=day − 1일`로 불러 세 날의 세션을 돌려준다. 한국 날짜 day의 미국 세션은 미국 날짜 day − 1과 day에 걸치기 때문이다.
- `merge_calendar(calendar, market, fetched)`는 세 날을 넣고 세 날 사이에 빠진 날을 휴장일로 넣는다. 이미 있는 날은 새 값으로 바꾼다. `data/v6/calendar.json`에 쓰는 것은 PRD 04가 잠금 안에서 한다(임시 파일에 쓴 뒤 바꿔 끼운다).
- `calendar.json`의 꼴은 `{"KR": {날짜: 세션}, "US": {미국 날짜: 세션}}`이다. 국내 세션은 `pre`·`regular`·`after`, 미국 세션은 `day`·`pre`·`regular`·`after`를 키로 `[시작, 끝]`(`+09:00`이 붙은 ISO 시각)을 가진다. 없는 세션은 키가 없고, 휴장일은 빈 객체다.

### 세션과 국내 거래일

- `sessions(calendar, t)`는 한국 시각 t에 열린 세션 `{"KR": 이름 또는 None, "US": 이름 또는 None}`을 돌려준다. 시작 ≤ t < 끝이면 열린 것이다.
- t의 한국 날짜를 d라 하면 국내는 `KR[d]`, 미국은 `US[d − 1]`과 `US[d]`를 본다. 셋 중 하나라도 없으면 `None`(모름)을 돌려준다.
- 국내 거래일은 `KR`에 `regular`가 있는 날이다.

### 판단 차례와 끝 시각

- `slots(calendar, d)`는 한국 날짜 d의 00:40부터 23:40까지 매시 T마다 E = T + 5분의 `sessions`를 본다. 모름이면 그 T는 `missing` 차례(판단하지 않고 `missing`으로 둔다), 한 시장이라도 열렸으면 판단 차례, 아니면 차례가 아니다.
- `end_time(calendar, start, fill)`은 start부터 날짜순으로 국내 거래일을 세어 10번째 날의 다음 날 00:00을 돌려준다. 세는 동안 `KR`에 없는 날이 나오면 `fill(그날)`(PRD 04가 `fetch_calendar("KR", 그날)`로 받아 잠금 안에서 합치고 쓴다)을 부른다.

## 종목 정보와 후보

- `fetch_instruments(symbols)`는 `/api/v1/stocks`를 한 번 불러 종목마다 아래 열의 값과 `observed_at`을 돌려준다. `data/v6/paper.sqlite3`의 `instruments` 표(`symbol` 기본 키)에 쓰는 것은 PRD 04다.
- `instruments`의 열은 `symbol`, `name`, `market`, `security_type`, `status`, `currency`, `leverage`(문자열, 없으면 빈 문자열), `nxt_supported`, `nxt_suspended`, `krx_suspended`, `liquidation`(0·1, 미국 종목은 0), `observed_at`이다. `market`은 토스 `market`이 `KOSPI`·`KOSDAQ`·`KR_ETC`이면 `KR`, `NYSE`·`NASDAQ`·`AMEX`·`US_ETC`이면 `US`이고, 그 밖이면 그 종목은 돌려주지 않는다.
- `eligible(info)`: `security_type`이 `STOCK` 또는 `ETF`이고 `status`가 `ACTIVE`다. ETF는 `leverage`를 수로 읽어 1 또는 −1이다(비어 있으면 거짓). 국내 종목은 `krx_suspended`와 `liquidation`이 거짓이다.
- `tradable(info, open_at_E)`: 그 시장 세션이 E에 열려 있다. 국내 `regular`면 `krx_suspended`가 거짓이고, 국내 `pre`·`after`면 `nxt_supported`가 참이고 `nxt_suspended`가 거짓이다(`null`은 거짓으로 본다).
- `fetch_rankings`의 항목은 `rank`, `symbol`, `change_rate`(`changeRate`), `last_price`(`lastPrice`)다. 두 값은 수로 읽히는 문자열이거나 없으면 `null`이고, 그 밖이면 `V6_TOSS_RESPONSE_INVALID`다.
- `candidates(rankings, infos, holdings, open_at_E, skip)`는 아래 순서로 후보 목록을 만든다. `rankings`는 받은 시장의 랭킹, `infos`는 종목마다 쓸 종목 정보, `skip`은 랭킹에서 고르지 않을 종목(기본은 없음)이다(어느 값을 쓸지는 PRD 02 `candidates_at`이 정한다).
  1. 시장 KR, US 순으로, 받은 랭킹을 순위 순으로 보며 `skip`에 없고 `infos`에 있고 `eligible`이고 `tradable`인 종목을 10개까지 고른다.
  2. 보유 종목(수량 > 0) 가운데 1에 없는 종목을 종목 코드 순으로 더한다.
  3. 항목은 `symbol`, `name`, `market`, `type`(`security_type`), `leverage`(수, 비어 있으면 `null`), `tradable_now`, `exit_only`(보유 종목이고 `eligible`이 아니면 참), `change_pct`(랭킹 `changeRate` × 100을 소수 둘째 자리 `ROUND_HALF_UP`, 랭킹에 없거나 `null`이면 `null`), `rank`(랭킹에 없으면 `null`), `held`의 순서다.
- 랭킹은 E에 세션이 열린 시장만 받는다.

## 가상 fixture

- 가짜 토스는 `httpx.MockTransport`로, 경로·쿼리별로 미리 적은 응답(상태, 본문, 헤더, 걸린 시간)을 차례로 돌려주고 요청을 기록한다. 시계는 가짜 시계이고 걸린 시간과 기다린 시간만큼 움직인다. 토큰은 임시 폴더의 유효한 토큰 파일로 만든 `SharedToken`이다.
- 장 운영 정보는 2026-11-09 기준 시각(국내 08:00~09:00·09:00~15:30·15:30~20:00, 미국 10:00~18:00·18:00~23:30·23:30~06:00·06:00~09:50)으로 정상 평일, 국내만 쉬는 날, 미국만 쉬는 날, 주말을 적고, T와 E 사이에 끝나는 세션과 시작하는 세션을 둔 날을 더한다.
- 종목 정보는 국내 주식(NXT 지원·미지원), 국내 ETF 배수 1·−1·2·−2, ETN, `SCHEDULED`·`DELISTED`, KRX 거래정지, NXT 거래정지, 정리매매, 미국 주식, 미국 ETF를 넣는다.
- 기대 세션, 판단 차례, 후보 목록은 손으로 적어 그대로 비교한다.

## 필수 Acceptance

SPEC 9절의 "세션과 후보" 항목을 맡는다.

- 국내 프리마켓·애프터마켓에는 NXT 지원이고 NXT 거래정지가 아닌 종목만, 정규장에는 KRX 거래정지가 아닌 종목이, 미국은 네 세션 모두 거래할 수 있고, 닫힌 시각에는 거래할 수 없다. 판단 차례와 `tradable_now`는 T가 아니라 E로 정한다(T에 열리고 E에 닫힌 경계, T에 닫히고 E에 열린 경계). 장 운영 정보를 받지 못하면 저장한 그 날짜의 값을 쓰고, 없으면 그 차례는 `missing`이다.
- 레버리지 배수 2·−2인 ETF, ETN, `ACTIVE`가 아닌 종목은 후보에 없고 −1 인버스 ETF는 있다. 열린 시장마다 10개까지다. 조건을 어기게 된 보유 종목은 `exit_only`로 들어가고 목표로 고를 수 없다.

## 완료 조건

- `uv run rt-trading v6-acceptance --prd 01`이 exit 0이다.
- `toss.py`와 `cli.py`를 바꾸므로 `acceptance`, `v0-acceptance`, `v2-acceptance`, `v3-acceptance`, `v4-acceptance`, `web-acceptance`, `v5-acceptance`가 그대로 통과한다.
- 장 운영 정보(국내·미국), 랭킹(국내·미국), 종목 정보를 한 번씩 읽기만 하는 시험 호출로 응답 형식이 이 PRD와 같은지 확인한다. 시험 호출은 서버에서 돌고 있는 v5 컨테이너 안에서 `cli.shared_token`으로 하고, v5의 판단·주문 시간 밖에서 한다. `issue_token`을 직접 부르거나 v5 서비스를 다시 만들거나 다시 켜지 않는다. 확인용 코드는 남기지 않는다.

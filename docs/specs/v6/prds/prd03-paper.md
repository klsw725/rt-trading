# PRD: v6 PRD 03 모의 계좌
> **상태**: 승인(2026-11-09, 사용자가 정한 대로 오라클 검토 통과로 승인)
> 상위 SPEC: [v6 SPEC](../SPEC.md)

## 의존성

- PRD 01의 토스 요청 함수(`request`), 세션(`sessions`), 국내 거래일, 종목 정보(`eligible`, `tradable`)
- PRD 02의 평가 자산(`equity`), C 평가(`account`), 실행할 목표(`targets`)
- 토스 Open API 1.2.28(2026-11-09 확인)

| API | 쓰는 값 |
| --- | --- |
| `GET /api/v1/prices?symbols=` | 종목마다 `symbol`, `lastPrice`(문자열) |
| `GET /api/v1/exchange-rate?baseCurrency=USD&quoteCurrency=KRW` | `midRate`(문자열). `validFrom` 등 시각 값은 쓰지 않는다 |
| `GET /api/v1/orderbook?symbol=` | `timestamp`(데이터 시각, 없으면 `null`), `asks`·`bids`의 `price`·`volume`(문자열) |

- 토스증권 수수료 안내(2026-11-09 확인). 값은 SPEC 4.2·4.3 표와 같다.

## 문제

모의 계좌는 원화와 달러를 따로 들고, 체결 시각의 실제 호가로 판 뒤 사며, 국내·미국의 다른 비용과 환전을 정확히 계산해야 한다. 체결에 쓸 호가가 판단보다 앞서거나 낡으면 결과를 믿을 수 없다.
판단이 실패해도 10분마다 평가하고 손실 한도를 지켜야 하며, 한도가 발동하면 다 팔 때까지 강제 매도를 이어 가야 한다.

## 목표

1. 장부(원화·달러 현금, 보유, 체결, 평가, 기준, 한도, 사건, 마지막 값)를 둔다.
2. 현재가·환율·호가를 받고, 받은 시각과 요청한 시각을 돌려준다.
3. 판단 체결: 목표 수량, 판 뒤 사기, 호가 걷기, 비용, 환전, 5종목, 막힌 매수를 한다(SPEC 4.1~4.3).
4. 10분 평가와 낡은 값, `stale`을 기록한다(SPEC 4.4).
5. 하루·한 주 기준, 손실 한도, 매수 막기, 강제 매도를 한다(SPEC 4.3).

## 범위 밖

- 언제 받고 체결하고 평가하는지, 장부 잠금과 트랜잭션, 429 기록, 검증 구간, 판정과 보고서(PRD 04)

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/v6_paper.py` | 장부 표, 받기, 판단 체결, 비용과 환전, 10분 평가, 기준과 한도, 강제 매도 |
| `src/rt_trading/v6_acceptance.py` | PRD 03 사례 |
| `docs/specs/v6/fixtures/prd03-paper.json` | 장부 상태, 받은 값, 호가, 목표와 손으로 계산한 기대 체결·평가·한도 |

## 장부

`data/v6/paper.sqlite3`에 아래 표를 둔다. 원화는 정수, 달러·가격·환율·평균 단가는 `Decimal`을 글자로 저장한다. 시각은 초까지 적은 `+09:00` ISO 시각이다.

| 표 | 열 |
| --- | --- |
| `cash` | `currency`(`KRW`·`USD`, 기본 키), `amount` |
| `positions` | `symbol`(기본 키), `market`, `currency`, `quantity`(INTEGER), `avg_price` |
| `fills` | `id`(INTEGER 기본 키), `at`, `origin`(`decision`·`forced`), `turn`(판단 차례 또는 평가 차례 이름), `symbol`, `market`, `session`, `side`(`BUY`·`SELL`), `quantity`, `levels`(JSON `[[가격, 수량], …]`), `amount`, `currency`, `commission`, `tax`, `risk_cost`, `sec_fee`, `fx_usd`, `fx_krw`, `fx_rate`, `fx_cost_rate`, `realized`, `eligible`·`tradable`·`blocked`(그 체결 때의 값, 0·1), `krw_after`, `usd_after`, `holdings_after` |
| `valuations` | `slot`(기본 키), `kind`(`watch`·`c`), `at`, `equity`(INTEGER), `krw`, `usd`, `rate`, `used`(JSON: 종목과 `USDKRW`마다 `[값, observed_at, 이번에 받았는지]`), `stale`(0·1), `stale_detail` |
| `baselines` | `kind`(`daily`·`weekly`), `key`(날짜, 한 주는 그 주 월요일), `equity`, `source`(`start`·`valuation`·`inherited`). 기본 키는 (`kind`, `key`) |
| `limits` | `kind`(`daily`·`weekly`·`cumulative`), `key`(날짜, 월요일, `all`), `at`, `equity`. 기본 키는 (`kind`, `key`) |
| `events` | `at`, `kind`(`risk_data_missing`·`forced_start`·`forced_end`, PRD 04의 `toss_429`·`rebalanced`), `detail` |
| `last_values` | `key`(종목 코드 또는 `USDKRW`, 기본 키), `value`, `observed_at` |

- 아래 장부 함수는 모두 PRD 04가 잠금 안에서 연 SQLite 연결 `conn`(트랜잭션 하나)을 받아 그 안에서만 쓰고, 네트워크를 부르지 않는다.
- `start(conn, day)`: 표를 만들고, 비어 있으면 원화 500,000·달러 0, 보유 없음, `daily`(day)·`weekly`(day가 든 주의 월요일) 기준 500,000(`start`)을 넣는다.
- `remember(conn, received)`: 받은 현재가·환율을 `last_values`에 쓴다.
- 보유 종목 수는 수량이 0보다 큰 종목 수다. 수량이 0이 되면 그 행을 지운다.
- C 평가는 PRD 02 `account`의 A_C와 쓴 값을 `kind = c`, `slot` = C(`YYYY-MM-DD HH:MM`)로 `valuations`에 쓴 것이다.

## 받기

받기 함수는 PRD 01 `request`로 부르고, 받은 값과 시각만 돌려준다. 저장소에는 쓰지 않는다.

- `observe_prices(symbols, deadline)`: `/api/v1/prices`를 한 번 불러 `{종목: (Decimal, observed_at)}`을 돌려준다. 응답에 없는 종목은 받지 않은 것이다.
- `observe_rate(deadline)`: `/api/v1/exchange-rate`를 불러 `(midRate, observed_at)`을 돌려준다.
- `observe_books(symbols, after, deadline)`: 종목마다 `after` 뒤에 호가를 요청해 `requested_at`, `observed_at`, `timestamp`, `asks`(가격 오름차순), `bids`(가격 내림차순)를 돌려준다. 잔량은 정수로 내린다.
- 쓸 수 있는 호가: `requested_at` ≥ 체결 시각, `observed_at` ≤ 체결 시각 + 40초, `timestamp`가 있고 체결 시각 − 5분 이상. 아니면 그 종목은 이번에 사고팔지 않는다.

## 판단 체결

`rebalance(conn, E, open_at_E, targets, a_c, rate_c, books, blocked)`는 PRD 04가 `ok` 판단의 E 뒤에 부른다. `rate_c`는 그 차례에 C까지 받은 환율(그 차례에 받지 못했으면 `None`. 앞 차례의 마지막 값은 쓰지 않는다), `blocked`는 지금 매수가 막혔는지다.

1. 거래할 종목은 PRD 01 `tradable`이 참이고 쓸 수 있는 호가가 있는 종목이다.
2. 목표 수량: 목표의 종목은 floor(A_C × 비중 ÷ 원화 최우선 매도 호가)다. 국내는 매도 호가 그대로, 미국은 매도 호가 × `rate_c`다. 매도 호가가 없거나, 미국 종목인데 `rate_c`가 `None`이면 목표 수량이 없다(사고팔지 않는다). 목표에 없는 보유 종목의 목표 수량은 0이다(환율이 없어도 된다).
3. 팔기: 거래할 보유 종목 가운데 목표 수량이 있고 지금 수량보다 적은 종목을 종목 코드 순으로, 그 차이만큼 매수 호가를 높은 쪽부터 잔량까지 채워 판다. 미국 매도는 기존 달러 현금 + 체결 금액 − 수수료 − SEC Fee가 0 이상인 가장 큰 정수 수량까지만 판다. 그 수량이 0이면 이번에는 팔지 않는다.
4. 사기: `blocked`가 아니면 거래할 종목 가운데 목표 수량이 지금 수량보다 많은 종목을 비중이 큰 순(같으면 종목 코드 순)으로 산다.
   - 지금 들고 있지 않은 종목인데 보유 종목 수가 이미 5이면 사지 않는다.
   - 살 수량은 목표 수량 − 지금 수량과 매도 호가 잔량 합 가운데 작은 것 이하에서, 매도 호가를 낮은 쪽부터 채웠을 때 아래 비용을 낼 수 있는 가장 큰 정수다. 0이면 사지 않는다.
   - 국내: 체결 금액 + 수수료 ≤ 원화 현금.
   - 미국: 모자란 달러 = max(0, 체결 금액 + 수수료 − 달러 현금), 드는 원화 = ceil(모자란 달러 × `rate_c` × (1 + 환전 비용률)) ≤ 원화 현금.
5. 체결마다 `fills`에 한 행을 쓰고 현금·보유를 바꾼다. 한 차례의 종목·방향마다 한 행(모의 주문 하나)이다.
   - 국내 매수: 원화 −= 금액 + 수수료. 미국 매수: 달러 += 모자란 달러, 원화 −= 드는 원화, 달러 −= 금액 + 수수료.
   - 평균 단가 = (지금 수량 × 평균 단가 + 체결 금액) ÷ (지금 수량 + 산 수량)이고, `Decimal` 기본 정밀도(28자리)로 계산한다. 팔아도 평균 단가는 그대로다.
   - 국내 매도: 원화 += 금액 − 수수료 − 세금 − 위험 비용. 미국 매도: 달러 += 금액 − 수수료 − SEC Fee. 달러는 원화로 바꾸지 않는다.
   - `realized`는 매도면 금액 − 판 수량 × 평균 단가 − 그 체결의 비용(그 종목 통화), 매수면 0이다.
   - `eligible`은 그때의 PRD 01 `eligible`, `tradable`은 그때의 PRD 01 `tradable`, `blocked`는 매수면 `blocked`, 매도면 0이다.
   - 새로 산 종목에 `last_values`가 없으면 체결 평균 단가(금액 ÷ 수량)를 그 호가를 받은 시각의 값으로 넣는다.
6. 체결 뒤에 PRD 02 `equity`로 다시 평가해 `check`를 부른다. 가격은 C 평가에 쓴 값이고, C에 값이 없던 종목은 체결 평균 단가, 환율은 C 평가에 쓴 값이다.

## 비용과 환전

| 항목 | 계산 |
| --- | --- |
| 국내 수수료 | floor(금액 × 0.00015). 체결 시각의 국내 세션이 `pre`·`after`면 floor(금액 × 0.00014) |
| 국내 주식 매도 세금 | `STOCK`이면 floor(금액 × 0.002) |
| 국내 ETF 매도 위험 비용 | `ETF`면 floor(max(0, 금액 − 판 수량 × 평균 단가) × 0.154) |
| 미국 수수료 | 금액 × 0.001을 0.01달러 단위로 내림 |
| 미국 매도 SEC Fee | max(금액 × 0.0000206을 소수 셋째 자리에서 `ROUND_HALF_UP`, 0.01) |
| 환전 비용률 | E의 한국 날짜가 국내 거래일이고 09:00 ≤ E < 15:30이면 0.0005, 아니면 0.005. 그 날짜의 장 운영 정보가 없으면 0.005 |

- 금액은 채운 호가 단계마다 가격 × 수량을 더한 값이다. 수수료와 SEC Fee는 그 금액 전체에 한 번 계산한다.

## 10분 평가

`value_slot(conn, slot, at, received)`는 PRD 04의 `v6-watch`가 받은 값(`received`: 이번에 받은 보유 종목 현재가와 환율)으로 부른다.

- 받지 못한 값은 `last_values`를 쓰고 `used`에 이번에 받지 않았다고 적는다. PRD 02 `equity`로 A를 계산해 `valuations`에 `kind = watch`, `at`(계산한 시각)으로 쓴다.
- `stale`: `at`에 PRD 01 `tradable`이 참인 보유 종목의 현재가, 또는 달러 현금이 0보다 크거나 미국 보유가 있을 때의 환율의 `last_values` `observed_at`이 `at`보다 20분 넘게 이르면 1이고, 해당 종목·환율을 `stale_detail`에 적는다. 응답 안의 시각 값은 쓰지 않는다. `at`의 세션을 모르면 열린 것으로 본다.

## 기준, 손실 한도와 매수 막기

- `bases(conn, at)`: 한국 날짜 d(시작일 뒤)의 00:00 차례 평가가 있으면 `daily`(d)를 그 자산으로 넣고, d가 월요일이면 `weekly`(d)도 넣는다(`valuation`). at이 d의 00:10 이후인데 `daily`(d)가 없으면 전날 `daily` 값을 `daily`(d)로 넣고(`inherited`) `risk_data_missing`을 기록한다. d가 월요일이면 앞 주 `weekly` 값도 `weekly`(d)로 넣고 함께 기록한다. PRD 04가 평가와 판단 때마다 부른다.
- `check(conn, A, at)`: 하루는 A × 100 ≤ `daily`(at의 날짜) × 98, 한 주는 A × 100 ≤ `weekly`(at이 든 주) × 95, 누적은 A × 100 ≤ 500,000 × 80이면 발동한다. 그 기간에 처음 발동한 한도만 `limits`에 쓰고, 강제 매도 중이 아니고 보유가 있으면 `forced_start`를 기록한다. 10분 평가 뒤와 판단 체결 뒤에 부른다.
- `blocked_reasons(conn, at)`: `daily_limit`(at의 날짜에 하루 한도 발동), `weekly_limit`(at이 든 주에 한 주 한도 발동), `cumulative_limit`(누적 한도 발동), `risk_data_missing`(at의 날짜에 `inherited` 하루 기준이 있거나, at이 든 주의 월요일에 `inherited` 한 주 기준이 있다), `forced_sale`(마지막 `forced_start` 뒤에 `forced_end`가 없다). 하나라도 있으면 매수가 막힌다.

## 강제 매도

- `force(conn, at, open_at, books)`: 강제 매도 중이면 10분 평가 뒤에, 그 차례 시작 시각 at을 체결 시각으로 받은 쓸 수 있는 호가로 거래할 수 있는 보유를 모두 판다(`origin = forced`, 판 뒤의 계산과 미국 매도 수량 제한은 판단 체결과 같다). 잔량이 모자라면 판 만큼만 팔고 다음 10분 평가에서 다시 판다.
- 보유가 없어지면 `forced_end`를 기록한다. 날짜나 주가 바뀌어도 이어 간다.

## 가상 fixture

- 장부 함수 사례는 임시 `paper.sqlite3`에 장부 상태를 만들고, 받은 값과 호가(요청·받은 시각과 데이터 시각)를 직접 넘긴다. 받기 함수 사례는 PRD 01과 같은 가짜 토스와 가짜 시계를 쓴다.
- 체결 사례는 장부 상태, C 평가(A_C, 쓴 값), 목표, 호가를 적고, 기대 `fills` 행을 손으로 계산해 적는다.
  - 국내 정규장 주식 매수(매도 호가 세 단계), 애프터마켓(NXT) ETF 차익 매도, 국내 주식 매도
  - 달러 일부가 있을 때 미국 매수: 국내 거래일 10:45(0.05%), 22:45와 토요일(0.5%). 미국 수수료가 0.01달러 아래를 버리는 금액, SEC Fee가 0.01달러가 되는 매도와 셋째 자리에서 반올림되는 매도
  - 판 뒤 사기, 비중 순 매수
  - E 전에 요청한 호가, E + 40초 뒤에 받은 호가, 데이터 시각이 없는 호가와 E − 5분보다 이른 호가
  - C까지 환율이 없는 차례: 미국 종목의 새 매수·증액·일부 감액은 없고, 목표에서 빠진 미국 보유와 `exit_only` 보유는 판다. 환율 응답의 `validFrom`이 오래된 차례는 그대로 쓴다
  - 닫힌 시장 보유 1종목 + 새 목표 5종목
- 한도 사례는 평가 순서와 자산을 적는다. 하루·한 주·누적이 정확히 경계인 자산, 잔량이 모자라 일부만 팔린 강제 매도가 자정과 월요일 00:00을 넘는 사례, 월요일이 아닌 날과 월요일의 00:00 평가가 없는 사례를 둔다.

## 필수 Acceptance

SPEC 9절의 "모의 계좌" 항목을 맡는다.

- 판 뒤에 사고, 목표 수량은 C 평가 자산 기준의 내린 정수 주다. E 전에 요청한 호가, E + 40초 뒤에 받은 호가, 데이터 시각이 없거나 E보다 5분 넘게 이른 호가는 쓰지 않는다. 여러 호가 단계의 체결을 종목·방향마다 모의 주문 하나로 모아 수수료와 SEC Fee를 한 번만 계산하고, 일부 달러 현금이 있을 때의 환전 원화(국내 거래일 09:00~15:30 0.05%, 그 밖 0.5%), 미국 수수료의 버림, SEC Fee의 최저액과 반올림, 국내 수수료(정규장·NXT), 증권거래세, 국내 ETF 위험 비용이 손 계산과 같다. 판 미국 종목의 돈은 달러로 남는다. 그 차례 C까지 환율을 받지 못하면 미국 종목을 새로 사거나 늘리거나 일부만 줄이지 않지만, 목표에서 빠진 보유의 전량 매도, `exit_only` 매도와 강제 매도는 계속한다. C까지 환율을 받았다면 응답의 `validFrom`은 목표 수량과 환전에 쓸 수 있는지 판단하는 기준으로 쓰지 않는다.
- 닫힌 시장의 보유 1종목과 새 목표 5종목이면 사고 나서 5종목을 넘는 매수를 하지 않는다.
- 손실 한도가 경계(정확히 −2%, −5%, −20%)에서 발동한다. 강제 매도가 일부만 체결되면 날짜나 주가 바뀌어도 다 팔 때까지 10분마다 다시 팔고 그동안 사지 않는다. 월요일이 아닌 날의 00:00 평가가 없으면 앞의 하루 기준을 쓰고 그날 사지 않으며, 월요일 00:00 평가가 없으면 앞의 한 주 기준도 쓰고 그 주 내내 사지 않는다.

## 완료 조건

- `uv run rt-trading v6-acceptance --prd 03`과 앞 PRD가 exit 0이다.
- 국내 정규장·애프터마켓 종목과 미국 데이마켓 종목의 호가, 현재가, 환율을 한 번씩 읽기만 하는 시험 호출로 응답 형식이 이 PRD와 같은지 확인한다. 시험 호출은 서버에서 돌고 있는 v5 컨테이너 안에서 `cli.shared_token`으로 하고, v5의 판단·주문 시간 밖에서 한다. `issue_token`을 직접 부르거나 v5 서비스를 다시 만들거나 다시 켜지 않는다. 확인용 코드는 남기지 않는다.

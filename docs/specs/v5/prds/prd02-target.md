# PRD: v5 PRD 02 목표 보유
> **상태**: 승인(2026-11-04, 사용자가 정한 대로 오라클 검토 통과로 승인)
> 상위 SPEC: [v5 SPEC](../SPEC.md)

## 의존성

- 엔진(`engine.run`과 v3·v4 선택 인자), v4 계좌 실행(`v4_paper.simulate`, `effective`, `closing`, `SUBORDINATES`), v4 계획 부하(`v4_head.plan`, `slots`)
- v3 기간·달력·시세 사본(`v3_daily.Period`, `is_trading_day`, `calendar`, `settled_days`, `settled_data`), 시세 검증과 기준가(`market_data.load`, `Bar`, `MarketData`), 장 일정(`sessions`)
- 엔진의 호가 단위(`engine.TICK_CHANGE`, 2,000원 경계)

## 문제

v4 모의 계좌는 다음 날 아침 정산 때만 계산한다. 실행기는 장중 차례마다 헤드 모의 계좌가 그 봉을 처리한 뒤의 상태를 알아야 하고, 기간이 끝나지 않아야 한다.
그런데 `engine.run`은 `Records`만 돌려줘 대기 주문을 알 수 없고, `market_data.load`는 끝나지 않은 날을 받지 않으며, v3 기간은 5거래일로 고정이고, 엔진은 데이터의 마지막 날을 기간 마지막 거래일로 보아 청산할 수 있다.

## 목표

1. 끝 없는 v5 기간과 달력을 만든다.
2. 정산한 시세에 오늘 그 봉까지의 1분봉을 더해 헤드 모의 계좌를 다시 돌리고, 엔진이 그 봉 뒤의 상태를 알린다.
3. SPEC 3.2 표와 종가 차례 규칙으로 목표 보유를 정하고, 목표를 정하지 않을 때의 이유를 돌려준다.
4. 상·하한가로 기준가가 전 거래일 종가와 다른 날을 알아본다.

## 범위 밖

- 봉과 상·하한가를 언제 받고 얼마나 기다리는지, 목표로 주문하는 것(PRD 03)
- 아침 실행·판단 명령과 보고서(PRD 04)

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/engine.py` | 선택 인자 `state`(마지막 봉 뒤의 상태 알리기), 봉 없는 첫날 |
| `src/rt_trading/v4_paper.py` | `simulate`가 `state`를 엔진에 넘기는 선택 인자 |
| `src/rt_trading/v5_target.py` | v5 기간·달력, 오늘을 더한 시세, 다시 돌리기, 목표, 상·하한가 계산 |
| `src/rt_trading/v5_period.json` | 시작일 `2026-11-09` |
| `src/rt_trading/v5_acceptance.py` | PRD 02 사례 |
| `docs/specs/v5/fixtures/prd02-target.json` | 가상 시세 spec, 판단 기록, 손으로 계산한 상태·목표·상하한가 |

## v5 기간과 달력

- `v5_target.load_period(period_path, holidays_path, today)`는 `v3_daily.Period(start, days, holidays)`를 돌려준다. `days`는 시작일부터 `today`의 다음 거래일까지의 거래일이다. 그래서 `final_day`(기간 마지막 날)는 늘 오늘 뒤라 어떤 데이터에도 없고, 엔진과 `v4_paper.simulate`의 마지막 거래일 처리(청산, 새로 사지 않음)가 일어나지 않는다. 이 날은 운용 끝이 아니며 어디에도 보이지 않는다.
- 시작일이 주말·휴장일이거나 읽을 수 없으면 `V5_PERIOD_INVALID`다. `today`가 시작일 전이면 `days`는 시작일 하나와 그 다음 거래일이다.
- 달력은 `v3_daily.calendar(data_days, period)`를 그대로 쓴다(기간 마지막 날이 든 달의 다음 달 말까지).

## 오늘을 더한 시세

`v5_target.with_today(base_data, today, bars, defects)`는 정산한 시세에 오늘을 더한 `MarketData`를 돌려준다.

- `base_data`는 정산한 마지막 날까지의 시세(`v3_daily.settled_data`)다. v5 시작일에는 정산한 날이 없으므로 원본 시세(`market_data.load(paths.source)`)의 오늘 전 날까지를 쓴다.
- 오늘의 1분봉은 받은 봉 중 `bar.time <= 차례 봉 시각`이고 `bar.time in 장 일정의 봉 시각 or bar.volume > 0`인 봉이다(`market_data.load`와 같은 규칙). 개장 전 차례에는 봉이 없다.
- 오늘의 기준가는 두 종목 모두 전 거래일 원가격 종가다. 오늘의 일봉 값은 (전 거래일 종가, 전 거래일 수정 종가)로 둔다(오늘 안에서는 엔진이 쓰지 않는다).
- 오늘이 결함일 표(`data_defects.csv`)에 있으면 결함일이다.

## 엔진에 더하는 것

`engine.run`에 선택 인자 `state`(dict)를 더한다. 주지 않으면 지금과 같다.

- 마지막 날의 마지막 봉을 처리한 뒤(봉이 없으면 개장 전 처리 뒤) `state`에 다음을 넣는다. `holding`: (종목, 수량) 또는 `None`. `buy`: (사려는 종목, 매도가 체결된 뒤에 사는지 참·거짓) 또는 `None`. `sell_reason`: 대기 매도 사유 문자열 또는 `None`. `limits`: `{"daily": 참·거짓, "weekly": 참·거짓, "cumulative": 참·거짓}`(발동해 있는지).
- `state`를 주면 마지막 날(오늘)의 마감 자산을 `Records.closes`에 넣지 않는다. 오늘은 마감한 날이 아니다.
- 봉이 없는 날이 시뮬레이션 첫날이어도 실패하지 않는다. 이전 버전의 결과는 바뀌지 않는다.
- `v4_paper.simulate`에 선택 인자 `state`를 더해 그대로 엔진에 넘긴다.

## 목표

`v5_target.target(...)`은 정산한 시세, 오늘 받은 봉, 판단 기록, 차례 종류(`pre_open`, `intraday`, `closing`)와 차례 봉 시각, 두 종목의 오늘 상·하한가를 받아 `Target(symbol, reason, state)`을 돌려준다. `symbol`은 목표 종목(현금이면 `None`)이고, 목표를 정하지 않으면 `reason`에 이유가 있다.

- 목표를 정하지 않는 이유는 이 순서로 본다.

| 이유 | 언제 |
| --- | --- |
| `settlement_behind` | 정산한 마지막 날이 오늘의 전 거래일이 아니다(v5 시작일은 아니다) |
| `limits_missing` | 두 종목 중 하나라도 오늘 상·하한가가 없다 |
| `base_changed` | 한 종목이라도 상·하한가가 전 거래일 종가로 계산한 값과 다르다 |
| `bar_missing` | 장중·종가 차례인데 두 종목 중 하나라도 차례 봉 시각의 봉이 없다 |

- 다시 돌리기는 헤드 계좌의 schedule(`v4_head.plan`을 기간의 오늘까지 날에 적용)로 `v4_paper.simulate`를 v5 시작일부터 돌린다. 오늘의 봉은 차례 봉 시각까지만 쓴다.
- 목표는 SPEC 3.2 표대로다. 대기 매수가 있으면 그 종목, 대기 매도만 있으면 현금, 대기 주문이 없으면 보유 종목(없으면 현금)이다.
- 종가 차례(차례 봉 시각이 마감 10분 전 봉)에는 그날 마감 때의 부하(`v4_paper.closing`)가 며칠 보유 방식이 아니면 표보다 앞서 현금이다.

## 상·하한가 계산

`v5_target.limits(base, day)`는 KRX 가격 제한으로 (상한가, 하한가)를 계산한다.

- 호가 단위는 엔진과 같다. 2023-12-11부터 2,000원 미만은 1원, 2,000원 이상은 5원이고, 그 전에는 5원이다.
- 폭 = 기준가 × 3 ÷ 10을 정수로 버린 값을 기준가의 호가 단위로 버린 값이다. 상한가 = 기준가 + 폭을 그 가격의 호가 단위 아래로, 하한가 = 기준가 − 폭을 그 가격의 호가 단위 위로 맞춘다.
- 받은 값과 계산한 값이 두 종목 모두 같으면 기준가가 전 거래일 종가와 같은 날이다.

## 가상 fixture

- 시세는 v1 `fixtures.build` spec으로 만든다. 날짜는 2025-11(수능일 2025-11-13 포함)과 월말이 들어가는 범위로 두고, 15거래일 준비일 뒤 v5 시작일을 둔다.
- 시험용 부하는 v4 PRD 03처럼 fixture에 적은 시각에 적은 주문을 내는 부하이고, acceptance만 `subordinates`에 더해 쓴다. 손실 한도 사례는 하루 −2%에 닿는 봉을 만든다.
- 월말 `turn_of_month` 사례는 그 달 마지막 거래일과 다음 달 거래일이 달력에 있어야 사는 날을 고른다.
- 상·하한가 사례는 기준가 89,280원, 2,000원, 1,999원, 1,995원과 2023-12-11 전 날을 두고 손으로 계산한 상·하한가를 적는다. 한 호가씩 다른 값도 적는다.
- 헤드 입력 사례는 v5 기간의 여섯 번째 거래일 개장 전 판단 입력을 만든다. 첫 다섯 날은 `hold_kodex200`을 고른 판단 기록이다.

## 필수 Acceptance

SPEC 9절의 "목표 보유" 항목을 맡는다.

- 장중 교체, 손실 한도 매도와 그 체결 뒤(`limits`가 알려지고 다시 사지 않음), 대기 주문 없이 보유 중인 때와 현금인 때, 마감 10분 전 `overnight` 매수, 종가 차례의 마감 청산(마감 10분 전 봉까지 돌린 상태에서), 개장 전 교체와 보유 부하의 매수(오늘 봉 없이), 교체의 매도 뒤 매수, 다음 달 거래일이 있어야 사는 월말 `turn_of_month`에서 목표와 `state`가 손 계산과 같다. 차례 봉 시각 뒤의 봉을 바꿔도 같다.
- 여섯 번째 거래일 개장 전 판단 입력에서 `head_account`의 부하·보유, `decision`의 `current`·`switches_left`, `recent_decisions` 수, `settled_through`가 손 계산과 같고, 지시문·부하 목록·모델이 v4 파일·상수와 같다. 다섯 번째 거래일과 데이터의 마지막 날에 `close` 매도가 없다(청산하지 않는다).
- v5 시작일에는 정산한 날 없이 원본 시세로 목표를 정한다(`settlement_behind`가 아니다).
- 상·하한가 계산이 손으로 계산한 값과 같고, 한 호가라도 다르면 `base_changed`다(SPEC 9절 "기준가 판별").
- `state`를 준 다시 돌리기의 `Records.closes`에 오늘이 없다. 엔진의 `state`를 주지 않은 v1~v4 결과가 그대로다(v1~v4 acceptance).
- 목표를 정하지 않는 네 이유에서 주문이 없다는 SPEC 9절 항목은 PRD 03이 실행기 전체로 확인한다.

## 완료 조건

- `uv run rt-trading v5-acceptance --prd 02`와 `--prd 01`이 exit 0이다.
- 엔진과 `v4_paper`를 바꾸므로 `acceptance`, `v2-acceptance`, `v3-acceptance`, `v4-acceptance`, `web-acceptance`가 그대로 통과한다.

# PRD: v3 PRD 02 부하와 하루 엔진
> **상태**: 초안, 구현 전
> 상위 SPEC: [v3 SPEC](../SPEC.md)

## 의존성

- v1·v2 엔진(`engine.run`), v1 노이즈 영역 돌파(`strategies.NoiseBreakout`), v2 후보 전략(`v2_strategies.CANDIDATES`·`SETTINGS`), v1 데이터 검증(`market_data.load`), 가상 시세 fixture 생성기(`fixtures.build`)
- PRD 01의 부하 id 목록(`v3_subordinates.json`)과 판단 기록(`decisions` 표)

## 문제

지금 엔진은 전략 하나로 기간 전체를 돌린다. v3는 계좌마다 날마다 다른 부하를 쓰고, 부하가 바뀌는 날은 개장 때 보유를 정리해야 한다.
또 모의 운용 기간의 마지막 날과 앞으로의 거래일 달력을 엔진에 알려야 한다. 데이터는 정산한 날까지만 있어서, 지금처럼 데이터의 마지막 날을 마지막 거래일로 보면 매일 다른 결과가 나온다.

## 목표

1. 엔진에 v3용 선택 인자를 더한다. 주지 않으면 v1·v2와 똑같이 움직인다.
2. 부하 id를 엔진 전략과 엔진 방식에 잇는다(SPEC 4.1).
3. 날마다 정한 부하로 계좌 하나를 돌리는 계좌 실행을 만든다. 부하 교체, 결함일, 마지막 거래일 규칙(SPEC 4.2)을 지킨다.
4. 헤드 계좌, 규칙 헤드 계좌, 부하 계좌가 날마다 쓸 부하를 정한다(SPEC 4.3).

## 범위 밖

- 시세 사본, 휴장일 표와 달력 만들기, 정산 순서, `v3-daily`(PRD 03)
- 보고서와 판정(PRD 04)

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/engine.py` | 선택 인자 `calendar`, `final_day`, `overnight_on`, `pre_open`과 개장 전 처리 |
| `src/rt_trading/v3_paper.py` | 부하 등록, 계좌 실행, 세 종류 계좌의 날마다 부하 |
| `src/rt_trading/v3_acceptance.py` | PRD 02 사례 |
| `docs/specs/v3/fixtures/prd02-paper.json` | 가상 시세 spec, 계좌별 날마다 부하, 시험용 부하의 주문, 손으로 계산한 체결과 마감 자산 |

## 엔진에 더하는 것

`engine.run`에 아래 선택 인자를 더한다. 하나도 주지 않으면 지금과 같다.

| 인자 | 뜻 |
| --- | --- |
| `calendar` | 전략에 넘기는 거래일 달력(`View.calendar`). 주지 않으면 지금처럼 데이터의 거래일이다 |
| `final_day` | 마지막 거래일로 볼 날. 주지 않으면 지금처럼 데이터의 마지막 거래일이다. 데이터에 이 날이 아직 없으면 어느 날도 마지막 거래일이 아니다 |
| `overnight_on` | 날짜를 받아 그날 며칠 보유 방식인지 돌려주는 함수. 주지 않으면 지금처럼 `overnight` 값 하나를 모든 날에 쓴다 |
| `pre_open` | 시뮬레이션하는 날마다 첫 봉 전에 한 번 부르는 함수. 아래 개장 전 처리를 돌려준다 |

개장 전 처리는 다음과 같다.

- `pre_open`은 그날 분배금을 받은 뒤, 첫 봉을 처리하기 전에 부른다. 그날 봉이 없고 `time`이 개장 시각인 `View`를 받는다.
- 돌려주는 값은 `PreOpen(cancel, sell, buy)`이다. 없으면 아무것도 하지 않는다.
  - `cancel`: 대기 매수를 취소한다. 버린 주문의 사유는 `switch_head`다.
  - `sell`: 보유가 있으면 판다. 체결 사유는 `switch_head`이고, 이미 대기 중인 매도가 있으면 그 사유를 `switch_head`로 바꾼다.
  - `buy`: 이 종목을 산다. 체결 사유는 `signal`이다. 보유가 없거나 같은 처리에서 보유를 팔 때만 받고, 팔 때는 매도가 체결된 뒤에 산다. 마지막 거래일이면 `last_day`, 한 주·누적 한도가 진입을 막고 있으면 `entry_blocked`, 대기 매수가 남아 있으면 `pending`을 사유로 버린다.
- 개장 전 주문은 엔진의 기존 규칙대로 그 종목의 시가 봉에서 시가 동시호가 가격에 체결된다. 매도가 먼저, 매수가 나중이다.
- 버린 주문의 시각은 그날 개장 시각이다.

## 부하 등록

`v3_paper.SUBORDINATES`는 부하 id마다 (전략을 만드는 함수, 며칠 보유 방식인지, 개장 전에 살 종목)을 담는다. id와 순서는 `v3_subordinates.json`과 같다.

| id | 전략 | 며칠 보유 | 개장 전에 살 종목 |
| --- | --- | --- | --- |
| `cash` | 주문하지 않는다 | 예 | 없음 |
| `hold_kodex200` | 주문하지 않는다 | 예 | KODEX 200 |
| `hold_inverse` | 주문하지 않는다 | 예 | KODEX 인버스 |
| `noise_breakout` | `strategies.NoiseBreakout` | 아니오 | 없음 |
| `turn_of_month`, `trend_200`, `last_half_hour`, `overnight_trend` | `v2_strategies.CANDIDATES[id]` | `v2_strategies.SETTINGS[id]`의 값 | 없음 |

- 계좌마다, 부하마다 전략 객체를 하나씩 만들어 그 계좌의 실행 내내 쓴다.
- 부하의 숫자와 규칙은 원래 스펙 그대로다. 결함일에 주문하지 않는 것도 각 부하가 지킨다.

## 계좌 실행

`v3_paper.simulate(data, calendar, start, final_day, plan, subordinates=SUBORDINATES)`는 계좌 하나의 `engine.Records`를 돌려준다.

- `data`는 v1 검증을 통과한 시세, `calendar`는 PRD 03이 만든 거래일 달력, `start`와 `final_day`는 기간의 첫날과 마지막 날, `plan`은 날짜마다 정한 부하 id다.
- 그날 실제로 쓰는 부하 e(d)는 d가 결함일이면 전 거래일의 e이고, 아니면 plan(d)다. 기간 첫날은 plan(첫날)이다.
- `engine.run(data, 전략, start=data.days.index(start), calendar=calendar, final_day=final_day, overnight_on=e(d)의 방식, pre_open=개장 전 처리)`로 돌린다.
- 전략은 분마다 e(d) 부하의 전략 객체에 `View`를 넘기고, 받은 주문을 그대로 돌려준다.
- 개장 전 처리는 다음과 같다.
  - 결함일에는 아무것도 하지 않는다. 그날의 plan은 쓰지 않는다.
  - 기간 첫날이거나 e(d)가 전 거래일의 e와 다르면 `cancel`을 하고, 보유가 있으면 `sell`을, e(d)에 개장 전에 살 종목이 있으면 `buy`를 한다.
  - e(d)가 전 거래일과 같으면, e(d)에 개장 전에 살 종목이 있고 보유와 대기 주문이 모두 없을 때만 `buy`를 한다.
- 손실 한도는 계좌마다 엔진이 따로 센다.

## 세 종류 계좌

| 계좌 | plan(d) |
| --- | --- |
| `head` | d의 판단 상태가 `ok`이면 그 부하, 아니면 `cash`. 판단 기록이 없는 날도 `cash` |
| `rule_head` | d의 전 거래일까지의 KODEX 200 수정 종가로 v2 SPEC 5.2의 추세를 본다. `200 × 마지막 값 > 마지막 200개 합`이면 `hold_kodex200`, 아니면 `cash` |
| 부하 id | 기간 내내 그 부하 |

- 계좌 순서와 이름은 `head`, `rule_head`, 그 뒤 SPEC 4.1 순서의 부하 id다. PRD 03·04는 이 순서를 쓴다.

## 가상 fixture

- 시세는 v1 `fixtures.build` spec으로 만들고 `market_data.load`로 읽는다.
- 시험용 부하는 fixture에 적은 날짜·시각에 적은 주문을 내는 부하다. acceptance만 `subordinates`에 더해 쓴다. 결함일에도 적은 대로 주문한다.
- 대기 매수가 남는 사례는 실제 결함일 2023-11-16(수능일인데 15:31~16:30 봉이 없다)을 쓴다. 시험용 부하가 그날 마지막 봉(15:30)이 끝날 때 낸 매수가 체결되지 않고 남은 상태에서, 다음 거래일에 부하를 바꾼다.
- 규칙 헤드의 선택은 수정 종가 목록을 직접 넣어 확인한다.
- 사례마다 계좌별 체결(시각, 종목, 매수·매도, 수량, 가격, 수수료, 세금, 사유), 버린 주문(시각, 종류, 사유), 날마다 마감 자산을 손으로 계산해 적고 그대로 비교한다.

## 필수 Acceptance

SPEC 9절의 "부하와 계좌" 항목을 맡는다.

- 같은 부하가 이어지면 보유와, 다음 거래일로 넘어가는 주문(종가 동시호가 분에 낸 매도)이 이어진다.
- 부하가 바뀌는 날 대기 매수가 `switch_head`로 취소되고, 보유는 시가 동시호가에 `switch_head`로 팔리고, 새 부하는 현금에서 시작한다. 보유 부하의 첫 매수는 그 매도 뒤 같은 시가 동시호가 가격에 체결된다.
- 결함일에 plan이 바뀌어도 교체와 새 주문이 없고, 부하와 상태가 다음 거래일로 넘어간다. 다음 거래일에 plan대로 교체한다.
- 같은 시세의 두 계좌 중 한 계좌에서만 손실 한도가 발동한다. 한 주 한도가 진입을 막는 동안 보유 부하의 개장 전 매수는 `entry_blocked`로 버려진다.
- 규칙 헤드가 전 거래일 추세로 고른다. 평균과 같으면 아래로 보고 `cash`다.
- 기간 마지막 거래일에 개장 전 매수와 새 매수를 하지 않고, 남은 보유를 종가 동시호가에 판다.
- 사례 안의 동시호가 체결가, 연속 매매의 한 단계 불리한 호가, 수수료, 인버스 세금, 정수 주가 손 계산과 같다.

## 완료 조건

- `uv run rt-trading v3-acceptance --prd 02`와 `--prd 01`이 exit 0이다.
- 엔진을 바꾸므로 `uv run rt-trading acceptance`와 `uv run rt-trading v2-acceptance`로 v1·v2 결과가 그대로인지 확인한다.

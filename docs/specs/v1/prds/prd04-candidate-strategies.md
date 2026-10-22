# PRD: v1 PRD 04 후보 전략
> **상태**: 초안, 구현 전
> 상위 SPEC: [v1 SPEC](../SPEC.md)

## 의존성

- PRD 03의 엔진과 주문 경계

## 문제

세 후보를 SPEC 5절의 숫자와 규칙 그대로 구현해야 백테스트 결과를 믿을 수 있다.

## 목표

1. `gap_reversal`, `overnight`, `noise_breakout`을 PRD 03의 주문만 써서 구현한다.
2. 전략 숫자를 코드 상수로 고정하고, 보고서가 그대로 남길 수 있게 내보낸다.

## 범위 밖

- 숫자 찾기, 새 후보
- 엔진 규칙 변경

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/strategies.py` | 세 전략과 고정 숫자 |
| `docs/specs/v1/fixtures/prd04-strategies.json` | 전략 사례 |

## 구현 메모

### `gap_reversal` (SPEC 5.1)

- 하룻밤 보유하지 않는다.
- 개장 첫 봉이 끝나면 갭과 기준 크기를 계산하고, 조건을 충족하면 `buy(종목, next_bar)`를 낸다.
- 기준 크기는 직전 20거래일 갭 절댓값의 평균이다. 시뮬레이션 첫날에는 2~21번째 거래일의 갭을 쓴다.

### `overnight` (SPEC 5.2)

- 하룻밤 보유한다.
- 거래일마다 개장 첫 봉이 끝나면 `buy(KODEX 200, close_auction)`를 낸다. 마지막 거래일에는 내지 않는다.
- 종가 동시호가 매수가 체결된 봉이 끝나면 `sell()`을 낸다. 다음 거래일 시가 동시호가에 팔린다.

### `noise_breakout` (SPEC 5.3)

- 하룻밤 보유하지 않는다.
- 판단 시각은 그날 장 일정에서 개장 30분 뒤부터 30분마다 마감 30분 전까지다. 그 시각에 끝난 봉에서만 판단한다.
- σ(m), 위·아래 경계, VWAP은 SPEC 5.3대로 계산하고, 계산할 수 없으면 그 시각을 건너뛴다.
- 판단 시각마다 한 가지만 한다. 보유 중이면 방향 전환(`switch`), 손절(`sell`, 사유 "손절"), 유지 순서로 보고, 보유가 없으면 진입(`buy(종목, next_bar)`)을 본다.
- 손절한 날에도 다음 판단 시각부터 다시 진입할 수 있다.

## 보고서에 남길 숫자

보고서의 `params`에 그대로 쓴다.

| 전략 | `params` |
| --- | --- |
| `gap_reversal` | `{"gap_lookback_days": 20}` |
| `overnight` | `{}` |
| `noise_breakout` | `{"sigma_lookback_days": 14, "first_decision_after_open_minutes": 30, "decision_interval_minutes": 30, "last_decision_before_close_minutes": 30}` |

## 필수 Acceptance

SPEC 9절의 "전략" 항목을 맡는다.

- 갭 되돌림: 갭이 기준 이상·이하·사이인 날, 분배락일(기준가 사용), 시뮬레이션 시작일의 동작이 기대와 같다.
- 종가 베팅: 종가 매수, 다음 날 시가 매도, 분배락일 분배금, 한도로 진입이 막힌 날, 마지막 날 신규 보유 없음이 기대와 같다.
- 노이즈 영역 돌파: 직전 14거래일만 쓴 경계, 갭 보정, 봉이 없는 m과 VWAP 거래량 0에서의 판단 생략, 장 유형별 판단 횟수, 방향 전환 우선, 손절 뒤 재진입, 마감 청산이 기대와 같다.
- 세 전략 모두 미래 봉을 바꿔도 그 전에 낸 주문이 같다.

## 완료 조건

- `uv run rt-trading acceptance --prd 04`가 exit 0이고 PRD 01~03도 계속 통과한다.

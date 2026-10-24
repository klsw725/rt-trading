# PRD: v3 PRD 04 보고서와 판정
> **상태**: 초안, 구현 전
> 상위 SPEC: [v3 SPEC](../SPEC.md)

## 의존성

- PRD 01의 `decisions` 표와 모델 이름
- PRD 02의 계좌 실행과 계좌 순서
- PRD 03의 달력과 기간, 시세 사본, `settled_days` 표
- v1 지표·체결 CSV(`report.metrics`, `report.write_trades`)

## 문제

1주 모의 운용의 진행과 결과를 보고서로 보고, 기간이 끝나면 SPEC 6절로 판정해야 한다.

## 목표

1. `v3-report`로 보고서와 판정을 만든다(SPEC 6·7절).
2. `v3-acceptance`가 PRD 01~04를 모두 실행하게 완성한다.

## 범위 밖

- 판단, 계좌 실행, 정산 자체(PRD 01~03)
- 자동 실행, 현황 웹페이지, 실데이터 첫날(PRD 05)
- 실전 매매

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/v3_report.py` | 계좌 지표, 판정, 보고서 파일, 터미널 요약 |
| `src/rt_trading/cli.py` | `v3-report` |
| `src/rt_trading/v3_acceptance.py` | PRD 04 사례와 전체 실행 |
| `docs/specs/v3/fixtures/prd04-report.json` | 정산한 상태를 만드는 실행 순서와 손으로 계산한 보고서 |

## 보고서

- 사본과 판단 기록으로 열 계좌를 정산한 마지막 날까지 돌려 만든다(PRD 03과 같은 방법). 정산한 날이 없으면 `V3_NOTHING_SETTLED`를 출력하고 보고서 없이 exit 1이다.
- `data/reports/v3-{실행 시각 YYYYMMDD-HHMMSS}/`에 아래 파일을 저장한다. JSON은 들여쓰기 2칸, 한글 그대로, 끝에 줄바꿈이다. 실행 시각은 디렉터리 이름에만 쓴다.
- 만들면 판정 여부와 관계없이 exit 0이다.

### `report.json`

| 키 | 값 |
| --- | --- |
| `run` | `start`, `end`(기간 마지막 날), `trading_days`(5), `settled_days`(정산한 날 수), `settled_through`, `model`(`gpt-6-sol`), `initial_capital`(`500000`), `commission_rate`(`0.000188`), `loss_limits`(v1과 같다), `defect_days`(기간 안의 결함일) |
| `decisions` | 정산한 날의 판단 상태별 수 `{"ok", "late", "invalid", "failed", "missing"}` |
| `accounts` | PRD 02 순서의 계좌마다 `id`와 지표 |
| `verdict` | 기간의 5거래일을 모두 정산하기 전에는 `null`. 정산했으면 `{"criteria": {"no_decision_failures", "drawdown_below_limit"}, "passed"}` |

- 계좌 지표는 `report.metrics`가 계산한 값 중 `final_equity`, `total_return`, `max_drawdown`, `trade_count`, `win_rate`, `avg_trade_pnl`, `commission_total`, `tax_total`, `limit_triggers`만 쓰고, v2처럼 `distribution_total`, `distribution_tax_total`을 더한다. 기간이 1주라 연환산 수익, 앞뒤 기간 수익, 연도별 수익, v1 기준 판정은 넣지 않는다.

### `decisions.csv`

열은 `date,subordinate,reason,status,model,reported_model,head_return`이다. 정산한 날과 오늘 판단한 날을 날짜순으로 쓴다. `head_return`은 그날 헤드 계좌의 `C_t ÷ C_(t−1) − 1`(자르지 않는 소수)이고, 정산 전이면 빈칸이다.

### `trades-{계좌}.csv`

계좌마다 `report.write_trades`로 v1과 같은 열을 쓴다. `reason`에 `switch_head`가 더해진다.

## 판정

기간의 5거래일을 모두 정산했을 때만 판정한다.

- `no_decision_failures`: 5거래일의 판단 상태가 모두 `ok`다.
- `drawdown_below_limit`: 헤드 계좌의 `C_0`과 1분봉 자산열의 모든 시점에서 `자산 × 5 > 그때까지의 최고 자산 × 4`다. `report.metrics`와 같은 정수 판정이라, 정확히 20%이면 거짓이다.
- `passed`는 둘 다 참일 때만 참이다.

## 터미널 요약

- 계좌마다 최종 자산, 총수익, 최대 낙폭, 체결 수를 한 줄씩 표로 보여 준다.
- 판단 상태별 수와, 정산 진행(`정산 n/5`) 또는 판정(`통과`·`불통과`)을 보여 준다.
- 끝에 보고서 디렉터리를 출력한다.

## 가상 fixture

- PRD 03의 fixture 방법으로 `v3-daily`를 여러 번 실행해 정산한 상태를 만든 뒤, `v3-report` 흐름(파일 위치, 보고서 디렉터리, 실행 시각을 주는 함수)으로 보고서를 만든다.
- 보고서 사례 하나에 `ok`와 실패 판단, 부하 교체, 기간 안의 결함일(실제 결함일 표의 날)이 모두 있게 한다.
- 기간 중간까지만 정산한 사례로 `verdict`가 `null`인 것을 확인한다.
- 최대 낙폭이 정확히 20%인 판정은 v2처럼 자산열을 판정 함수에 직접 넣어 확인한다.
- 기대값은 손으로 계산해 함께 적는다. 소수는 `1e-12` 안의 차이를 허용하고, 나머지는 그대로 비교한다.

## 필수 Acceptance

SPEC 9절의 "판정과 보고서" 항목을 맡는다.

- SPEC 6절의 경계: 판단 실패가 한 번이라도 있으면 불통과이고, 최대 낙폭이 정확히 20%면 불통과다. 5거래일을 모두 정산하기 전에는 판정하지 않는다(`verdict`가 `null`).
- `report.json`, `decisions.csv`, `trades-{계좌}.csv`의 형식과 값이 SPEC 7절과 이 PRD와 같다.
- 같은 저장 상태로 두 번 만들면 실행 시각을 뺀 보고서 내용이 같다.

## 완료 조건

- `uv run rt-trading v3-acceptance --prd 04`가 exit 0이다.
- `cli.py`를 바꾸므로 `uv run rt-trading acceptance`, `uv run rt-trading v0-acceptance`, `uv run rt-trading v2-acceptance`로 기존 명령이 그대로인지 확인한다.

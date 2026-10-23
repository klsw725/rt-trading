# PRD: v2 PRD 04 판정, 보고서, 실행
> **상태**: 구현 완료
> 상위 SPEC: [v2 SPEC](../SPEC.md)

## 의존성

- PRD 01의 1단계 일봉 읽기·확인과 `stage1.json` 내용(`v2_stage1.load`, `v2_stage1.build`)
- PRD 02의 엔진(시작 위치, 며칠 보유 방식, 분배금 기록)
- PRD 03의 후보 전략과 후보별 엔진 설정(`v2_strategies.CANDIDATES`, `v2_strategies.SETTINGS`)
- v1의 데이터 검증(`market_data.load`), 지표·판정과 체결 CSV(`report.py`), 가상 시세 fixture 생성기(`fixtures.build`)

## 문제

두 단계의 결과를 이어 최종 판정을 내리고 보고서로 남겨야 v2가 끝난다.

## 목표

1. `v2-backtest`를 구현한다: 두 DB 확인, 1단계, 단계 잇기, 2단계, 최종 판정, 보고서 저장, 요약 출력.
2. `v2-acceptance`가 PRD 01~04를 모두 실행한다.
3. 실데이터로 `v2-backtest`를 한 번 실행한다.

## 범위 밖

- 1단계 계산(PRD 01), 엔진(PRD 02), 전략(PRD 03)
- 시세 수집(`v0-check`, `fetch`)
- 결과를 보고 숫자를 바꾸는 일

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/v2_backtest.py` | 두 DB 확인, 단계 잇기, 2단계 보고서, 최종 판정, 보고서 파일, 터미널 요약 |
| `src/rt_trading/report.py` | 후보 하나의 지표·판정 계산과 체결 CSV 쓰기를 따로 부를 수 있게 나눈다. v1 보고서는 그대로다 |
| `src/rt_trading/cli.py` | `v2-backtest` |
| `src/rt_trading/v2_acceptance.py` | PRD 04 사례 |
| `docs/specs/v2/fixtures/prd04-backtest.json` | 두 DB의 가상 시세 spec과 손으로 계산한 보고서 |

## 데이터 확인

- `data/v0.sqlite3`는 PRD 01의 읽기·확인으로, `data/market.sqlite3`는 v1 PRD 02의 검증으로 확인한다. 한쪽이 실패해도 다른 쪽까지 확인한다.
- 시세 DB가 v1 검증을 통과하면 2단계 확인을 더 한다. 어기면 `V2_STAGE2_DATA_INVALID`다.
  - 거래일이 201일 이상이다.
  - 마지막 거래일에 두 종목 모두 그날 장 일정의 종가 동시호가 시각 봉이 있고 거래량이 0보다 크다. v1 검증은 결함일의 봉 구성을 확인하지 않으므로, 마지막 거래일이 결함일일 때 이 확인이 필요하다.
- 하나라도 어기면 어긴 것을 한 줄씩 오류 코드로 시작해 1단계, 시세 DB 순서로 출력하고, 보고서 디렉터리를 만들지 않고 exit 1이다.
  - 1단계: PRD 01의 코드(`V2_DATA_MISSING`, `V2_STAGE1_DATA_INVALID`)와 내용
  - 시세 DB: v1 PRD 02의 검증 출력(파일이 없으면 `V1_DATA_MISSING`)
  - 2단계 확인: `V2_STAGE2_DATA_INVALID`와 내용

## 단계 잇기와 최종 판정

- 1단계는 ①②④를 PRD 01대로 계산한다.
- 2단계는 1단계를 통과한 ①②④와 ③을 SPEC 5절 순서(①②③④)로 PRD 03의 엔진 설정에 맞춰 `engine.run`으로 돌린다.
- 후보마다 단계별 결과는 `passed`(통과), `failed`(불통과), `not_tested`(시험하지 않음)다. ③의 1단계는 늘 `not_tested`이고, 1단계를 통과하지 못한 ①②④의 2단계는 `not_tested`다.
- 최종 `passed`는 시험한 단계를 모두 통과했을 때만 참이다.

## 보고서

`data/reports/v2-{실행 시각 YYYYMMDD-HHMMSS}/`에 SPEC 7절의 파일을 저장한다. JSON은 v1처럼 들여쓰기 2칸으로 한글을 그대로 쓰고 끝에 줄바꿈을 둔다. 실행 시각은 디렉터리 이름에만 쓴다.

- `stage1.json`: PRD 01의 `stage1.json` 내용 그대로다.
- `report.json`
  - `run`: `fetched_at`(시세 DB `fetch_runs`의 가장 늦은 값), `data_start`·`data_end`(2단계 데이터의 처음과 마지막 거래일), `initial_capital`(`500000`), `commission_rate`(`0.000188`), `loss_limits`(v1과 같다), `defect_days`(2단계 데이터 기간의 결함일 전체)
  - `candidates`: 2단계를 시험한 후보를 SPEC 5절 순서로 담는다. 후보마다 `id`, `params`, `simulation_start`·`simulation_end`(그 후보 시험 기간의 처음과 마지막 거래일), `trading_days`(`N`), v1 PRD 05의 후보 지표(`final_equity`부터 `passed`까지, 계산도 v1과 같다), `distribution_total`(분배금 기록의 분배금 합계), `distribution_tax_total`(분배금 기록의 세금 합계)
  - `params`는 SPEC 5절의 보고서 숫자다.
  - `tax_total`은 v1처럼 체결의 세금(인버스 매매차익 세금)만 더한다. 거래 순손익(`avg_trade_pnl`)에는 엔진 기록대로 받은 분배금 − 세금이 들어 있다.
- `trades-{id}.csv`: 2단계를 시험한 후보마다 v1과 같은 열과 형식으로 쓴다.
- `verdict.json`: SPEC 7절의 형식으로 ①②③④ 순서다.

## 터미널 요약

후보마다 시험한 단계별로 최종 자산(1단계는 소수, 2단계는 원), 총수익, 최대 낙폭, 앞뒤 기간 수익, 판정을 한 줄씩 보여 주고, 최종 판정을 한 줄 더한 표를 출력한다. 끝에 보고서 디렉터리를 출력한다.

## 명령

```bash
uv run rt-trading v2-backtest
uv run rt-trading v2-acceptance
```

- `v2-backtest`는 보고서를 만들면 후보 통과 여부와 관계없이 exit 0이다. 확인에 실패하면 위대로 exit 1이다.
- `v2-acceptance`는 PRD 01~04 사례를 모두 실행하고, 모두 통과하면 exit 0이다.

## 가상 시세 fixture

- 사례는 임시 디렉터리에 두 DB를 만들어 `v2-backtest`와 같은 흐름(두 DB 경로, 보고서 디렉터리, 실행 시각을 주는 함수)으로 돌린다.
- 1단계 DB는 PRD 01의 가상 일봉 fixture 형식, 2단계 DB는 v1 PRD 02의 가상 시세 fixture 형식으로 적는다.
- 보고서 사례 하나에 1단계를 통과하지 못한 후보, 1단계를 통과하고 2단계를 통과하지 못한 후보, 2단계를 통과한 ③, 분배금을 받은 2단계 거래, 어느 후보의 시험 기간에도 들지 않는 결함일이 모두 있게 한다.
- 거래일 수 확인은 정확히 201일(보고서를 만든다)과 200일(실패)로 본다.
- 확인 실패 사례는 실패 이유가 하나만 있게 만든다. 마지막 거래일 종가 동시호가 봉 사례는 마지막 거래일을 `data_defects.csv`의 결함일로 하고 한 종목의 종가 동시호가 봉을 뺀다.
- 기대값은 손으로 계산해 함께 적는다. 소수는 `1e-12` 안의 차이를 허용하고, 나머지는 그대로 비교한다.

## 필수 Acceptance

SPEC 9절의 "데이터, 판정과 보고서" 항목을 맡는다.

- 두 DB 중 하나라도 없거나, 1단계 일봉 확인, v1 검증, 2단계 거래일 수(201일 이상), 2단계 마지막 거래일의 종가 동시호가 봉 확인 중 하나라도 실패하면 보고서 없이 exit 1이다. 2단계 거래일이 정확히 201일이면 보고서를 만든다.
- 1단계를 통과한 후보만 2단계를 시험하고, ③은 2단계만 시험하며, 시험한 단계를 모두 통과해야 통과다.
- `stage1.json`, `report.json`, `trades-{id}.csv`, `verdict.json`의 형식과 값이 7절과 같다.
- 같은 입력으로 두 번 실행하면 실행 시각을 뺀 보고서 내용이 같다.

## 완료 조건 (v2 완료)

- `uv run rt-trading v2-acceptance`가 exit 0이다.
- `report.py`와 `cli.py`를 바꾸므로 `uv run rt-trading acceptance`와 `uv run rt-trading v0-acceptance`로 기존 명령이 그대로인지 확인한다.
- SPEC 10절을 확인한 뒤 실데이터로 `v2-backtest`를 한 번 실행해 보고서를 만들었다. 후보가 통과하는지는 완료 조건이 아니다.
- SPEC과 모든 PRD의 상태를 "구현 완료"로 바꾼다.

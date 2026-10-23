# PRD: v1 PRD 05 판정, 보고서, 백테스트 실행
> **상태**: 구현 완료
> 상위 SPEC: [v1 SPEC](../SPEC.md)

## 의존성

- PRD 02의 검증, PRD 03의 엔진 기록, PRD 04의 전략

## 문제

엔진 기록을 SPEC 6절의 통과 기준과 7절의 보고서로 바꾸고, 실데이터로 한 번 돌려야 v1이 끝난다.

## 목표

1. 엔진 기록에서 지표와 통과 판정을 계산한다(SPEC 6~7절).
2. `backtest`를 구현한다: 데이터 검증, 노이즈 영역 돌파 시뮬레이션, 보고서 저장, 요약 출력.
3. `acceptance`가 PRD 01~05를 모두 실행한다.
4. 실데이터로 `fetch`와 `backtest`를 한 번 실행한다.

## 범위 밖

- 통과한 전략의 실전 운용(실전 매매 스펙)
- 결과를 보고 숫자를 바꾸는 일

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/report.py` | 지표, 통과 판정, 보고서 파일, 터미널 요약 |
| `src/rt_trading/cli.py` | `backtest` |
| `docs/specs/v1/fixtures/prd05-report.json` | 판정·보고서 사례 |

## 보고서 계약

`data/reports/{실행 시각 YYYYMMDD-HHMMSS}/`에 저장한다.

- `report.json`
  - `run`: `fetched_at`, `data_start`, `data_end`, `simulation_start`, `simulation_end`, `trading_days`(N), `initial_capital`, `commission_rate`, `loss_limits`(`daily`, `weekly`, `cumulative`), `defect_days`
  - `candidates`: 후보마다 `id`, `params`, `final_equity`, `total_return`, `annualized_return`, `max_drawdown`, `first_half_return`, `second_half_return`, `yearly_returns`, `trade_count`, `win_rate`, `avg_trade_pnl`, `commission_total`, `tax_total`, `limit_triggers`(`daily`, `weekly`, `cumulative`), `criteria`(`net_positive`, `drawdown_below_limit`, `both_halves_positive`), `passed`
  - `run.fetched_at`은 `fetch_runs`의 가장 늦은 `fetched_at`이다. `run.data_start`와 `run.data_end`는 검증된 거래일 목록의 처음과 끝 날짜다.
  - `run.commission_rate`는 `0.000188`, `run.loss_limits`는 `{"daily": -0.02, "weekly": -0.05, "cumulative": -0.20}`이다.
  - `run.defect_days`는 시뮬레이션 기간에 든 결함일(PRD 02)의 날짜 목록이다.
  - `params`는 PRD 04의 표를 그대로 쓴다. `yearly_returns`는 연도 문자열을 키로 쓴다(예: `{"2023": 0.0123}`).
  - `avg_trade_pnl`은 거래 순손익 합계 ÷ 거래 수를 JSON 숫자로 쓰며 원 단위 정수로 자르지 않는다. 거래가 없으면 `null`이다.
- `trades-{id}.csv`: PRD 03의 체결 내역. 열은 `time,symbol,side,quantity,price,commission,tax,reason`이다.
  `side`는 `buy`·`sell`, `reason`은 `signal`(신호)·`stop`(손절)·`switch`(방향 전환)·`limit`(한도)·`close`(마감)이다.
- 수익률·낙폭·비율은 소수(예: `0.0123`)로 쓰고, 돈은 `avg_trade_pnl`을 빼고 원 단위 정수로 쓴다.
- 시각은 초까지 적은 RFC 3339 한국 시각(예: `2026-10-22T15:30:00+09:00`), 날짜는 `YYYY-MM-DD`로 쓴다.
- 터미널에는 최종 자산, 총수익, 연환산 수익, 최대 낙폭, 앞뒤 기간 수익, 판정을 표로 보여 준다.

## 명령

```bash
uv run rt-trading backtest
uv run rt-trading acceptance
```

- `backtest`는 보고서를 만들면 후보 통과 여부와 관계없이 exit 0이다. 데이터가 없거나 검증에 실패하면 PRD 02의 코드를 출력하고 보고서 없이 exit 1이다.
- `acceptance`는 PRD 01~05 사례를 모두 실행하고, 모두 통과하면 exit 0이다.

## 필수 Acceptance

SPEC 9절의 "판정" 항목을 맡는다. 사례는 임시 SQLite 파일에 가상 시세를 넣어 `backtest`와 같은 흐름으로 돌린다. 최대 낙폭이 정확히 20%인 판정만 자산열을 직접 넣어 확인한다.

- 홀수 거래일의 앞뒤 나누기, `E_0`를 포함한 최대 낙폭, 연환산 공식, 연도별 수익, 거래 수·승률·거래당 평균 손익과 통과 판정이 손 계산과 같다. 거래가 없으면 거래 수는 `0`이고 승률과 거래당 평균 손익은 `null`이다. 최대 낙폭이 정확히 20%이면 통과하지 못한다.
- `report.json`과 `trades-{id}.csv`의 형식과 값이 기대와 같다. 결함일, 인버스 세금, 한도 발동, 방향 전환·한도 체결이 있는 보고서로도 확인한다.
- 수집 기록이 여러 개면 가장 늦은 `fetched_at`이 `report.json`의 `run.fetched_at`에 들어간다.
- 검증에 실패하는 데이터에서는 보고서를 만들지 않고 exit 1이다.
- 같은 입력으로 두 번 돌리면 실행 시각을 뺀 보고서 내용이 같다.

## 완료 조건 (v1 완료)

- `uv run rt-trading acceptance`가 exit 0이다.
- SPEC 10절을 확인한 뒤 실데이터로 `fetch`와 `backtest`를 한 번 실행해 보고서를 만들었다. 후보가 통과하는지는 완료 조건이 아니다.
- SPEC과 모든 PRD의 상태를 "구현 완료"로 바꾼다.

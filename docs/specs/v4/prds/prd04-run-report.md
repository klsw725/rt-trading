# PRD: v4 PRD 04 실행과 보고
> **상태**: 승인(2026-11-01), 구현 완료(2026-11-03)
> 상위 SPEC: [v4 SPEC](../SPEC.md)

## 의존성

- PRD 01의 뉴스 받기(`v4_news`), PRD 02의 판단 차례·계획 부하·입력·판단(`v4_head`), PRD 03의 계좌 실행과 계좌 순서(`v4_paper`)
- v3 매일 실행의 달력·기간·시세 사본·정산 방법(`v3_daily`), 미국 시세 받기(`rt-trading v3-us-fetch`), v1 시세 받기(`rt-trading fetch`), 데이터 검증(`market_data.load`), 가상 시세 fixture 생성기(`fixtures.build`)
- v1 토스 클라이언트(`toss.read_credentials`, `toss.issue_token`, `toss.candles`, `toss.minute_rows`)
- v1 지표·체결 CSV(`report.metrics`, `report.write_trades`), v3 보고서 형식(v3 PRD 04)

## 문제

아침에는 v3처럼 시세를 받아 밀린 날을 정산하고 개장 전 판단을 한다. 장중·마감 전 판단 차례마다 당일 1분봉과 뉴스를 받아 판단한다.
판단 차례가 하루 여러 번이라, 정산은 판단하지 못한 차례를 모두 `missing`으로 남겨야 하고, 보고서와 판정은 차례 단위로 세야 한다.

## 목표

1. v4 기간 설정과 파일 위치를 둔다.
2. 정산이 판단하지 못한 차례마다 `missing`을 넣게 한다.
3. 헤드 입력에 넣을 계좌 요약을 만든다(SPEC 3.2).
4. `v4-daily`와 `v4-decide`를 만든다(SPEC 5.1).
5. `v4-report`로 보고서와 판정을 만든다(SPEC 6·7절).
6. `v4-acceptance`가 PRD 01~04를 모두 실행하게 완성한다.

## 범위 밖

- 자동 실행, 현황 웹페이지, 서버(PRD 05)

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/v4_period.json` | 기간 시작일 `{"start": "YYYY-MM-DD"}` |
| `src/rt_trading/v4_daily.py` | 파일 위치, 기간, 정산, 계좌 요약, 당일 1분봉 받기, `v4-daily`, `v4-decide` |
| `src/rt_trading/v4_report.py` | 보고서, 판정, 터미널 요약 |
| `src/rt_trading/cli.py` | `v4-daily`, `v4-decide --at HH:MM`, `v4-report` |
| `src/rt_trading/v4_acceptance.py` | PRD 04 사례와 전체 실행 |
| `docs/specs/v4/fixtures/prd04-run.json` | 실행마다의 시각·명령·원본 시세 spec·당일 1분봉·뉴스·Codex 응답과 손으로 정한 기대, 손으로 계산한 보고서 |

## 파일 위치와 기간

| 파일 | 위치 |
| --- | --- |
| 원본 시세 | `data/market.sqlite3`(v1 `fetch`) |
| 시세 사본 | `data/v4/market.sqlite3` |
| 판단과 정산 | `data/v4/paper.sqlite3`의 `decisions`(PRD 02), `settled_days`(v3와 같은 열) |
| 헤드 기록 | `data/v4/head/` |
| 뉴스 기록 | `data/v4/news/` |
| 미국 시세 | `data/us.sqlite3`(v3와 함께 쓴다) |

- 기간 설정은 `v4_period.json`이고, 휴장일 표(`krx_holidays.csv`를 v3와 함께 쓴다), 거래일 달력, 5거래일 기간은 v3 PRD 03과 같은 방법으로 정한다. 기간 설정이 틀렸으면 `V4_PERIOD_INVALID`다.

## 정산

v3 PRD 03의 정산과 같은 순서로 하고, 3단계만 바꾼다. v3 코드를 함께 쓰려고 나누면 v3 동작은 그대로여야 한다. 함께 쓰는 단계의 오류 코드(`V3_CALENDAR_MISMATCH`, `V3_DATA_NOT_READY` 등)는 v3와 같다.

3. 그날의 판단 차례(PRD 02) 중 판단 행이 없는 차례마다 `missing` 행을 넣는다. `date`, `slot`, `kind`, `status` 밖의 문자열 열은 빈 문자열이고, `attempts`는 0이다.

## 계좌 요약

v3 PRD 03의 계좌 요약과 같고 다음을 바꾼다.

- 계좌는 PRD 03의 네 종류 계좌이고, 사본과 판단 기록으로 정산한 마지막 날까지 돌린다. 헤드 계좌의 schedule은 판단 기록으로 계산한 PRD 02의 계획 부하다.
- 부하 계좌 요약은 `v4_subordinates.json`의 부하마다 만든다.
- `head_account.subordinate`는 마지막 정산일 마감 때 헤드 계좌가 실제로 쓴 부하다(PRD 03). 정산한 날이 없으면 `null`이다.
- `morning_account`는 아침 판단 계좌의 `{"return_since_start_pct", "max_drawdown_pct"}`이고, 계산은 `rule_head`와 같다.
- 정산한 날이 없으면 모든 계좌가 시작 상태다.

## 당일 1분봉 받기

`v4-decide`가 오늘 시세 요약(PRD 02)에 쓸 두 종목의 오늘 1분봉을 받는다.

- 실제 받기 함수는 받을 수 있는 초를 받아, 그 안에서 `.env`의 토스 인증 정보로 토큰을 받고, 종목마다 `toss.candles(interval="1m", adjusted=False, since=오늘)`로 받는다. `toss.minute_rows(since=오늘, today=내일)`로 오늘 봉만 남겨 `market_data.Bar` 목록으로 돌려준다.
- 받은 초 안에 끝나지 않으면 멈추고 `V4_BARS_TIMEOUT`을, 토스 오류면 v1 오류 코드를 출력한 뒤 받지 못한 것(`None`)으로 돌려준다.
- 원본 시세 파일에는 저장하지 않는다. 받은 봉은 그 차례의 헤드 기록 폴더에 `bars.json`(종목마다 `[시각, 시가, 고가, 저가, 종가, 거래량]` 목록)으로 남긴다.
- 받지 못하면 오늘 시세 요약이 `null`이고 판단은 계속한다.

## `v4-daily`

v3 PRD 03의 `v3-daily`와 같은 순서로 하고 다음을 바꾼다.

- 3단계의 정산은 위 정산이다.
- 4단계의 판단은 PRD 02의 `pre_open` 판단이다. 판단 시각 T는 입력을 만드는 시각이고, 뉴스는 PRD 01로 T에 받는다. 뉴스 받기 마감은 T + 2분과 판단 마감 중 이른 시각이다.
- 4단계에서 오늘 `pre_open` 판단 행이 이미 있으면 다시 부르지 않는다.
- 6단계의 출력에 오늘 `pre_open` 판단의 상태와 부하, 뉴스 받기 오류 코드를 적는다.
- 4단계의 실패는 `pre_open` 판단 상태가 `ok`가 아니거나 마감이 지나 판단하지 않은 것이다.

## `v4-decide --at HH:MM`

`HH:MM`을 차례 s라 한다.

1. 기간 설정과 휴장일 표를 읽는다. 오늘이 주말이거나 휴장일이면 `휴장일`, 기간 시작일 전이면 `기간 전`, 기간 마지막 날 뒤면 `기간 끝`을 출력하고 exit 0이다. 아무것도 받거나 부르지 않고, 파일을 만들거나 바꾸지 않는다.
2. s가 오늘의 `intraday`·`pre_close` 차례가 아니면 `V4_SLOT_INVALID`를 출력하고 exit 1이다.
3. 오늘 s의 판단 행이 이미 있으면 다시 부르지 않고 그 행의 상태와 부하를 출력한다. 상태가 `ok`나 `capped`면 exit 0, 아니면 exit 1이다.
4. 지금이 s의 마감(s + 5분)과 같거나 늦으면 `V4_DEADLINE_PASSED`를 출력하고 exit 1이다. 받기와 호출을 하지 않고, 그 차례는 다음 정산 때 `missing`이 된다.
5. 당일 1분봉을 받고, 이어서 뉴스를 받는다. 둘 다 s + 2분까지만 받는다. 당일 1분봉 받기 함수에는 s + 2분까지 남은 초를 주고, 뉴스 받기 마감은 s + 2분이다.
6. 계좌 요약(정산을 마친 날까지)과 PRD 02의 입력을 만들어 판단한다. 판단 시각 T는 s다.
7. 판단 상태와 부하, 지금 부하와 남은 교체 횟수(판단을 반영한 뒤), 받기 실패 코드를 출력한다. 상태가 `ok`나 `capped`면 exit 0, 아니면 exit 1이다.

- `v4-decide`는 시세 사본과 정산을 바꾸지 않는다.
- 당일 1분봉 받기 함수(남은 초를 받는다), 뉴스 받기 함수, Codex 클라이언트, 시계, 기다리기, 파일 위치, 기간 설정과 휴장일 표는 인자로 받는다. acceptance가 바꿔 끼운다.

## 보고서

v3 PRD 04의 보고서와 같고 다음을 바꾼다.

- 디렉터리는 `data/reports/v4-{실행 시각 YYYYMMDD-HHMMSS}/`다. 정산한 날이 없으면 `V4_NOTHING_SETTLED`를 출력하고 보고서 없이 exit 1이다.
- `report.json`
  - `run`에 기간의 판단 차례 수 `decision_slots`를 더한다.
  - `decisions`는 정산한 날의 판단 행 상태별 수 `{"ok", "capped", "late", "invalid", "failed", "missing"}`다.
  - `accounts`는 PRD 03 순서의 계좌다.
  - `verdict`는 기간의 5거래일을 모두 정산하기 전에는 `null`이고, 정산했으면 `{"criteria": {"no_decision_failures", "drawdown_below_limit"}, "passed"}`다.
- `decisions.csv`의 열은 `date,slot,kind,subordinate,reason,status,model,reported_model,head_return`이다. 기간 안의 판단 행을 (날짜, 차례) 순으로 쓴다. `head_return`은 그날 헤드 계좌 수익률(자르지 않는 소수, 같은 날 행은 같은 값)이고, 정산 전이면 빈칸이다.
- `trades-{계좌}.csv`는 v3와 같다.

## 판정

기간의 5거래일을 모두 정산했을 때만 판정한다.

- `no_decision_failures`: 기간 5거래일의 모든 판단 차례의 상태가 `ok`나 `capped`다.
- `drawdown_below_limit`: v3 PRD 04와 같다. 정확히 20%이면 거짓이다.
- `passed`는 둘 다 참일 때만 참이다.

## 터미널 요약

- v3 PRD 04와 같다. 판단 상태별 수에 `capped`가 더해진다.

## 가상 fixture

- v3 PRD 03의 fixture 방법을 쓴다. 사례마다 실행 순서(날짜, 시각, 명령), 그때의 원본 spec, 당일 1분봉, 뉴스 받기 결과, Codex 응답, 기대(정산한 날, `decisions` 행, 출력에 나올 코드, exit, Codex 호출 수, 받기 함수에 준 초, 파일 변경 여부)를 적는다.
- 당일 1분봉 받기와 뉴스 받기는 가짜 함수다. 받은 초나 마감을 기록하고, 시간 초과 사례에서는 가짜 시계를 받기 마감까지 움직이고 받지 못한 것으로 돌려준다.
- 미국 시세 파일은 두지 않는다. 헤드 입력의 `us_market`은 `null`이다.
- 보고서 사례 하나에 `ok`, `capped`, 실패, `missing`, 장중 교체, 기간 안의 결함일(실제 결함일 표의 날)이 모두 있게 한다. 기간 중간까지만 정산한 사례로 `verdict`가 `null`인 것을 확인한다.
- 최대 낙폭이 정확히 20%인 판정은 v3처럼 자산열을 판정 함수에 직접 넣어 확인한다.
- 기대값은 손으로 계산해 함께 적는다. 소수는 `1e-12` 안의 차이를 허용하고, 나머지는 그대로 비교한다.

## 필수 Acceptance

SPEC 9절의 "실행"과 "판정과 보고서" 항목을 맡는다.

- `v4-decide`: 휴장일과 기간 밖에는 받기와 Codex 호출이 없고 파일이 바뀌지 않으며 exit 0이다. 차례가 아니면 `V4_SLOT_INVALID`다. 마감과 같거나 늦게 시작하면 받기와 호출, 판단 행이 없고, 다음 정산에서 그 차례가 `missing`이 된다. 같은 차례를 두 번 실행하면 두 번째에 받기와 Codex 호출이 없다. 당일 1분봉 받기에는 s + 2분까지 남은 초(s에 시작하면 120초)를 주고, 그때까지 받지 못하면 `today_market`과 `news`가 `null`인 채로 마감 전에 판단한다. 시세 사본과 `settled_days`가 바뀌지 않는다.
- 정산은 판단 행이 없는 차례마다 `missing` 행을 넣는다(개장 전·장중·마감 전 모두). 오래된 날부터 정산하고 검증에 실패한 날에서 멈춘다. 같은 날 `v4-daily`를 두 번 실행해도 판단과 정산이 다시 생기지 않는다. 휴장일에는 아무것도 하지 않는다(v3에서 옮긴 사례).
- SPEC 6절의 경계: 판단 실패가 한 번이라도 있으면 불통과다. `capped`만 있으면 통과할 수 있다. 최대 낙폭이 정확히 20%면 불통과다. 5거래일을 모두 정산하기 전에는 판정하지 않는다(`verdict`가 `null`).
- `report.json`, `decisions.csv`, `trades-{계좌}.csv`의 형식과 값이 SPEC 7절과 이 PRD와 같다. 같은 저장 상태로 두 번 만들면 실행 시각을 뺀 보고서 내용이 같다.

## 완료 조건

- `uv run rt-trading v4-acceptance`(PRD 01~04)가 exit 0이다.
- `cli.py`와 v3 매일 실행 코드를 바꾸므로 `uv run rt-trading acceptance`, `v0-acceptance`, `v2-acceptance`, `v3-acceptance`로 기존 명령이 그대로인지 확인한다.

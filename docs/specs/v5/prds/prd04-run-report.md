# PRD: v5 PRD 04 운영과 보고
> **상태**: 승인(2026-11-04, 사용자가 정한 대로 오라클 검토 통과로 승인)
> 상위 SPEC: [v5 SPEC](../SPEC.md)

## 의존성

- v4 실행(`v4_daily.run`, `decide`, `summary`, `mark_missing`, `Paths`), v4 헤드·뉴스·계좌 실행, v4 보고서(`v4_report.build`), v3 정산(`v3_daily.settle`, `settled_days`, `settled_data`, `calendar`)
- PRD 01의 함께 쓰는 토큰(시세 받기 명령과 당일 1분봉), PRD 02의 v5 기간(`load_period`), PRD 03의 저장소(`v5_book`: 장부, 주문, 정지)

## 문제

v4 아침 실행과 판단 명령은 5거래일 기간을 스스로 읽는다. v5는 같은 판단·정산을 기간 끝 없이 이어 가야 하고, 실계좌 봇 장부와 헤드 모의 계좌를 함께 보는 보고서가 필요하다.

## 목표

1. `v5-daily`, `v5-decide --at`을 v4와 같은 순서·입력으로, v5 파일 위치와 끝 없는 기간으로 돌린다.
2. `v5-report`로 실장부, 주문, 날마다의 모의 계좌·봇 자산 수익률을 보고서로 만든다.

## 범위 밖

- 실행기와 장부 반영(PRD 03), 일정 실행과 현황 페이지(PRD 05)
- 분배금(SPEC: 봇 장부에 넣지 않는다), 판정(v5는 통과 판정을 하지 않는다)

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/v4_daily.py` | `run`·`decide`에 선택 인자 `period`(기간을 돌려주는 함수). 주지 않으면 지금처럼 v4 기간을 읽는다 |
| `src/rt_trading/v5_report.py` | 보고서 내용과 파일 |
| `src/rt_trading/cli.py` | `v5-daily`, `v5-decide --at HH:MM`, `v5-report`, `V5_PATHS` |
| `src/rt_trading/v5_acceptance.py` | PRD 04 사례 |
| `docs/specs/v5/fixtures/prd04-run.json` | 가상 시세·판단·장부·주문, 손으로 계산한 보고서 값 |

## 아침 실행과 판단

- 파일 위치는 `data/v5/`의 `market.sqlite3`(정산한 날까지의 시세 사본), `paper.sqlite3`(판단과 정산한 날), `head/`, `news/`이다. 원본 시세 `data/market.sqlite3`와 미국 시세 `data/us.sqlite3`는 v4와 같이 쓴다.
- `v5-daily`는 `v4_daily.run`을, `v5-decide --at`은 `v4_daily.decide`를 v5 파일 위치와 `period=lambda: v5_target.load_period(..., today)`로 부른다. 시세 받기·뉴스·Codex·정산·판단·출력은 v4와 같다. 기간 마지막 날이 늘 오늘 뒤라 `기간 끝`·`기간 종료`가 나오지 않는다.
- `v4_daily.run`·`decide`의 `period`를 주지 않으면 지금처럼 `v3_daily.load_period`로 v4 기간을 읽는다.

## 보고서

`v5-report`는 `data/reports/v5-{YYYYMMDD-HHMMSS}/`에 다음을 쓴다. 정산한 날이 없으면 `V5_NOTHING_SETTLED`를 출력하고 1이다.

| 파일 | 내용 |
| --- | --- |
| `report.json` | `run`(시작일, 정산한 마지막 날, 모델, 시작 자금, 수수료율, 손실 한도)과 `v4_report.build`(v5 기간으로 부른다)의 `decisions`·`accounts`, `verdict`(늘 `null`), `live`다. 엔진에 주는 기간 마지막 날(PRD 02)은 어디에도 쓰지 않는다(`v4_report.build`의 `run`을 그대로 쓰지 않는 까닭이다). `live`는 가장 최근 실장부의 시작 현금, 현금, 보유 종목과 수량, 정산한 마지막 날 마감 봇 자산, 총수익, 수수료 합, 세금 합, 실주문 수, 안전 정지 수다. 실장부가 없으면 `null`이다 |
| `trades-{계좌}.csv` | v4와 같다. 현황 페이지가 `trades-head.csv`로 헤드 계좌 보유를 정한다(web 스펙) |
| `orders.csv` | 실주문과 점검 주문(연습 주문은 넣지 않는다): 주문 번호, 구분, 날짜, 차례, 종목, 방향, 방식, 수량, 토스 상태, 체결 수량, 체결 금액, 평균 체결가, 수수료, 세금, 오류 코드 |
| `decisions.csv` | v4와 같은 열 |
| `daily.csv` | 실장부를 만든 날부터 정산한 날마다 날짜, 헤드 모의 계좌 수익률, 마감 봇 자산, 봇 자산 수익률 |

- 마감 봇 자산과 봇 자산 수익률은 PRD 03의 마감 봇 자산 정의를 쓴다. 첫날 수익률은 시작 현금 대비다.
- 계좌번호와 토큰은 쓰지 않는다. `account_seq`도 쓰지 않는다.

## 가상 fixture

- 시세와 판단 기록은 v5 기간의 여섯 거래일 이상을 덮는다. 가짜 시세 받기·뉴스·Codex는 v4 PRD 04 acceptance와 같은 방식이다.
- 장부와 주문은 `v5_book`에 직접 쓴다. 매수 체결(수수료 포함) 뒤 하루 들고 이틀째 파는 실주문, 거부된 주문 하나, 안전 정지 하나를 둔다.
- 보고서 값(마감 봇 자산, 수익률, 수수료·세금 합, 주문 수)을 손으로 계산해 적는다.

## 필수 Acceptance

SPEC 9절의 "운영과 보고" 항목을 맡는다.

- `v5-daily`가 여섯 번째·일곱 번째 거래일에도 밀린 날을 정산하고 개장 전 판단을 하며, `v5-decide`가 같은 날 판단한다. `기간 끝`·`기간 종료`가 나오지 않는다. `period`를 주지 않은 v4 명령은 그대로다(v4 acceptance).
- 보고서의 `live`(현금, 보유, 마감 봇 자산, 총수익, 수수료·세금 합, 실주문 수, 안전 정지 수)와 `daily.csv`의 날마다 모의 계좌·봇 자산 수익률, `orders.csv`의 행(연습 주문 없음)이 손 계산과 같다. 실장부가 없으면 `live`가 `null`이다. `accounts`와 `trades-head.csv`가 있고 `verdict`가 `null`이며, 보고서 어디에도 엔진에 주는 기간 마지막 날이 없다.

## 완료 조건

- `uv run rt-trading v5-acceptance --prd 04`와 `--prd 01`~`--prd 03`이 exit 0이다.
- `v4_daily.py`와 `cli.py`를 바꾸므로 v0~v4와 web acceptance가 그대로 통과한다.

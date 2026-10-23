# PRD: v1 PRD 01 토스 시세 수집
> **상태**: 구현 완료
> 상위 SPEC: [v1 SPEC](../SPEC.md)

## 문제

백테스트에 쓸 1분봉과 일봉을 토스 Open API에서 받아 로컬에 저장해야 한다.
토큰 충돌, 허용 IP, 호출 한도 같은 토스 쪽 제약은 숨기지 않고 실패로 드러내야 한다.

## 목표

1. CLI 골격(`fetch`, `backtest`, `acceptance`)과 PRD별 acceptance 실행기를 만든다.
2. 토스 토큰 발급과 캔들 조회 클라이언트를 만든다(SPEC 3.1).
3. 두 종목의 1분봉 원가격, 일봉 원가격·수정 가격을 받아 SQLite 파일 `data/market.sqlite3`에 저장한다(SPEC 3.2).
4. `data/`를 `.gitignore`에 넣는다.

## 범위 밖

- 장 일정, 데이터 검증, 기준가(PRD 02)
- 계좌·주문 API
- 인증 정보와 토큰의 저장·출력

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/__init__.py` | `main`이 `cli.main`을 부른다 |
| `src/rt_trading/cli.py` | `fetch`, `backtest`, `acceptance [--prd NN]` |
| `src/rt_trading/toss.py` | 토큰 발급, 캔들 조회, 페이지네이션, 호출 속도, 오류 분류 |
| `src/rt_trading/store.py` | `data/market.sqlite3`의 표 만들기, 한 번에 바꿔 넣기, 읽기 |
| `src/rt_trading/acceptance.py` | PRD별 사례 등록·실행, 결과 출력 |
| `docs/specs/v1/fixtures/prd01-toss.json` | 가상 토스 응답 |
| `pyproject.toml` | `uv add httpx` |
| `.gitignore` | `data/` |

- `backtest`는 PRD 05 전까지 "아직 구현하지 않음"을 출력하고 exit 1이다.

## 수집 계약

- 인증 정보는 환경 변수 `TOSS_INVEST_CLIENT_ID`, `TOSS_INVEST_CLIENT_SECRET`에서 읽고, 없으면 저장소 루트의 `.env`에서 읽는다.
- `POST /oauth2/token`으로 토큰을 한 번 발급해 실행 내내 쓴다.
- `GET /api/v1/candles`를 `symbol`, `interval`, `count=200`, `adjusted`, `before`로 부르고, 응답의 `result.candles`를 읽어 `result.nextBefore`로 과거로 넘긴다.
  가장 오래된 봉이 2022-11-23보다 앞서거나, 받은 봉이 없거나, `result.nextBefore`가 없으면 멈춘다.
- 종목마다 1분봉 원가격(`adjusted=false`), 일봉 원가격(`adjusted=false`), 일봉 수정 가격(`adjusted=true`)을 받는다.
- 원가격 일봉과 수정 가격 일봉은 `date`로 합친다. 날짜가 중복되거나 두 응답의 날짜가 다르면 `V1_TOSS_RESPONSE_INVALID`로 실패하고 저장하지 않는다.
- 2022-11-23 전 봉과 실행일 당일 봉은 버린다.
- 가격과 거래량은 정수로 바꿔 저장한다. 바꿀 수 없으면 `V1_TOSS_RESPONSE_INVALID`로 실패한다.
- 호출은 초당 15회 이하로 보낸다. 한도 20회보다 여유를 둔다.

## 오류 처리

| 상황 | 동작 | 코드 |
| --- | --- | --- |
| `401 token-revoked` | 재발급하지 않는다. 같은 키를 쓰는 다른 프로그램과 충돌했을 수 있다고 출력하고 실패한다 | `V1_TOKEN_REVOKED` |
| 그 밖의 `401` | 실패한다 | `V1_TOKEN_INVALID` |
| `403` | 토스 WTS의 허용 IP 등록을 안내하고 실패한다 | `V1_IP_NOT_ALLOWED` |
| `429` | `Retry-After`초 기다린 뒤 같은 요청을 다시 보낸다 | 없음 |
| `5xx`, 네트워크 오류 | 1·2·4초 뒤 세 번까지 다시 보내고, 그래도 실패하면 실패한다 | `V1_TOSS_UNAVAILABLE` |
| 응답 형식 오류 | 실패한다 | `V1_TOSS_RESPONSE_INVALID` |

토큰 발급 오류는 OAuth2 형식(`error`, `error_description`)으로 온다. 오류 출력에는 응답에 `requestId`가 있을 때만 넣고, 인증 정보와 토큰은 넣지 않는다.

## 저장 형식

시세는 SQLite 파일 `data/market.sqlite3` 하나에 저장한다. 파이썬 기본 `sqlite3` 모듈을 쓴다.

| 표 | 열 | 기본 키 |
| --- | --- | --- |
| `candles_1m` | `symbol`, `time`, `open`, `high`, `low`, `close`, `volume`. `time`은 봉이 끝나는 한국 시각 `YYYY-MM-DD HH:MM` | `symbol`, `time` |
| `candles_1d` | `symbol`, `date`, `open`, `high`, `low`, `close`, `volume`, `adj_close`. `close`는 원가격 종가, `adj_close`는 수정 종가 | `symbol`, `date` |
| `fetch_runs` | `fetched_at`(수집을 마친 시각, 초까지 적은 RFC 3339 한국 시각), `symbol`, `interval`, `data_start`, `data_end`, `bar_count` | `fetched_at`, `symbol`, `interval` |

- 모든 열은 `NOT NULL`이다. `symbol`, `time`, `date`, `fetched_at`, `interval`, `data_start`, `data_end`는 `TEXT`, 가격·거래량·`bar_count`는 `INTEGER`로 선언한다.
- `time`은 `YYYY-MM-DD HH:MM`, `date`·`data_start`·`data_end`는 `YYYY-MM-DD`로 저장한다.
- 스키마 버전은 `PRAGMA user_version`으로 표시하며 v1은 1이다.
- `fetch`는 모든 응답을 다 받아 검사한 뒤, 한 트랜잭션 안에서 두 종목의 기존 봉을 지우고 새 봉과 `fetch_runs` 기록을 넣는다. 중간에 실패하면 되돌려서 기존 데이터를 그대로 둔다.
- 성공한 수집마다 같은 `fetched_at`으로 종목별 `1m`·`1d` 한 행씩, 모두 네 행을 `fetch_runs`에 넣는다. 수정 가격과 합친 일봉도 `1d` 한 행으로 기록한다.
- `fetch_runs`는 지우지 않고 쌓는다. 가장 늦은 `fetched_at`이 지금 저장된 시세의 수집 기록이다.
- `candles_1m`은 `symbol, time`, `candles_1d`는 `symbol, date` 오름차순으로 읽는다.

## 명령

```bash
uv run rt-trading fetch
uv run rt-trading acceptance --prd 01
```

- `fetch`는 성공하면 exit 0, 실패하면 오류 코드를 출력하고 exit 1이다.
- `acceptance`는 사례별 결과와 요약을 JSON으로 출력한다. 실행한 사례가 모두 통과하면 exit 0, 아니면 exit 1이다.
  `--prd`가 없으면 구현된 모든 PRD를 실행한다.

## 필수 Acceptance

SPEC 9절의 "수집" 항목을 맡는다. 네트워크 없이 `httpx.MockTransport`에 fixture 응답을 넣어 확인한다.

- 페이지네이션: 세 페이지를 이어 붙이고, 2022-11-23 전 봉과 당일 봉을 버리고, 시각 오름차순으로 저장한다.
- 봉 시각: 응답 시각을 봉이 끝나는 시각으로 저장한다.
- 요청: 1분봉은 원가격, 일봉은 원가격과 수정 가격을 각각 요청한다.
- 일봉 원가격과 수정 가격의 날짜가 다르거나 중복되면 저장하지 않고 `V1_TOSS_RESPONSE_INVALID`로 실패한다.
- `401 token-revoked`: 토큰 발급이 한 번뿐이고 `V1_TOKEN_REVOKED`로 실패한다.
- 토큰 발급의 OAuth2 형식 `403`(`requestId` 없음)은 `V1_IP_NOT_ALLOWED`로 실패한다.
- 캔들 조회의 `403`, `429`, `5xx`, 형식 오류가 위 표대로 동작한다. `429`는 기다린 뒤 성공한다.
- 저장: 임시 SQLite 파일의 봉 네 계열과 `fetch_runs` 네 행이 기대와 같고, 세 표의 열 이름·선언 형식·`NOT NULL`·기본 키와 `PRAGMA user_version = 1`이 위 저장 형식과 같다.
- 두 번째 종목 수집 실패와, 기존 봉을 지운 뒤 새 봉을 넣다가 일부러 일으킨 실패 모두에서 `candles_1m`, `candles_1d`, `fetch_runs`의 기존 내용이 그대로 남는다.
- 출력과 DB 어디에도 인증 정보와 토큰이 없다.
- `git check-ignore`로 `data/` 아래 파일이 git에서 제외되는지 확인한다.

## 완료 조건

- `uv run rt-trading acceptance --prd 01`이 exit 0이다.
- 실제 토스 호출은 PRD 05 완료 조건에서 한 번 한다.

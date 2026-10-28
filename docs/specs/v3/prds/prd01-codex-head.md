# PRD: v3 PRD 01 Codex 헤드
> **상태**: 초안, 구현 전
> 상위 SPEC: [v3 SPEC](../SPEC.md)

## 의존성

- v1 시세 DB `data/market.sqlite3`의 `candles_1d`와 그 저장 형식(v1 PRD 01), v1 장 일정 표(`sessions`). v1 코드는 바꾸지 않는다.
- PRD 03이 받아 두는 미국 네 종목 일봉 `data/us.sqlite3`의 `us_candles_1d`와 종목 목록(`v3_us.SYMBOLS`)
- trading-oracle이 쓰는 Codex OAuth 토큰 파일 `~/.trading-oracle/auth/codex.json`. trading-oracle 코드는 가져오지 않고, 같은 라이브러리(`oauth-cli-kit`)와 같은 요청 형식(trading-oracle `src/agent/codex.py`)을 쓴다.

## 문제

헤드가 매 거래일 개장 전에 부하를 고르려면 Codex를 부르고, 개장 전에 알 수 있는 것만 담은 입력을 만들고, 응답을 검증해 기록해야 한다.
Codex가 늦거나 실패하거나 엉뚱한 답을 줘도 그날을 `cash`로 정하고 실패로 남겨야 한다. 그래야 모의 운용이 멈추지 않고, 실패가 판정에 드러난다.

## 목표

1. 공유 토큰으로 Codex를 부르는 클라이언트를 만든다(SPEC 3.1).
2. SPEC 3.2의 헤드 입력을 만든다. 계좌 요약과 그달 거래일 목록은 PRD 03이 넘겨준다.
3. 응답을 검증하고(SPEC 3.3), 다시 부르기와 판단 마감을 지켜(SPEC 3.1·3.4) 판단을 기록한다.
4. 헤드 지시문과 부하 설명 표를 저장소에 둔다. 각 부하 id를 엔진 전략에 잇는 것은 PRD 02다.
5. `v3-acceptance [--prd NN]` 실행기를 만든다. PRD 04에서 모든 PRD를 실행하게 완성한다.

## 범위 밖

- 부하를 엔진 전략에 잇기, 하루 실행, 계좌와 손실 한도(PRD 02)
- 휴장일 표, 시세 수집·검증, 정산, `missing` 기록, `v3-daily`(PRD 03)
- 보고서와 판정(PRD 04)
- Codex 로그인(trading-oracle의 `uv run main.py codex-login`)

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/codex.py` | 공유 토큰 읽기, Codex 호출, 응답 스트림 읽기, 오류 분류 |
| `src/rt_trading/v3_head.py` | 헤드 입력 만들기, 응답 검증, 다시 부르기와 마감, 판단 기록 |
| `src/rt_trading/v3_head_prompt.txt` | 헤드 지시문. SPEC 부록 B 그대로다 |
| `src/rt_trading/v3_subordinates.json` | 부하 id(SPEC 4.1 순서), 설명, 원래 백테스트 결과 |
| `src/rt_trading/v3_acceptance.py` | PRD별 사례 등록·실행, 결과 출력 |
| `src/rt_trading/cli.py` | `v3-acceptance [--prd NN]` |
| `docs/specs/v3/fixtures/prd01-head.json` | 가상 일봉 spec, 가짜 Codex 응답, 손으로 계산한 기대 입력과 기록 |
| `pyproject.toml` | `uv add "oauth-cli-kit>=0.1.3,<1.0.0"` |

- `v3-acceptance`는 v2와 같은 JSON으로 사례별 결과와 요약을 출력한다. 실행한 사례가 모두 통과하면 exit 0, 아니면 exit 1이다. `--prd`가 없으면 구현된 모든 v3 PRD를 실행한다.

## 부하 설명 표

`v3_subordinates.json`은 아래 행을 이 순서로 담은 배열이다. 숫자는 각 SPEC 상태 줄의 값이다.

| `id` | `description` | `backtest` |
| --- | --- | --- |
| `cash` | 아무것도 사지 않고 현금으로 쉰다 | 없음 |
| `hold_kodex200` | 계좌가 비어 있으면 개장 때 KODEX 200을 사서 계속 든다 | 없음 |
| `hold_inverse` | 계좌가 비어 있으면 개장 때 KODEX 인버스를 사서 계속 든다 | 없음 |
| `noise_breakout` | 개장 30분 뒤부터 30분마다, KODEX 200이 최근 14거래일의 평소 움직임보다 크게 오르면 KODEX 200을, 크게 내리면 인버스를 사고 손절·방향 전환을 한다. 그날 종가에 모두 판다 | v1 1분봉(2022-11~2026-10) 총수익 −20.08%, 최대 낙폭 20.47%, 2023-09-12 누적 손실 한도 발동 |
| `turn_of_month` | 달 마지막 거래일의 전 거래일 종가에 KODEX 200을 사서 다음 달 셋째 거래일 종가에 판다 | v2 일봉(2002~2026) 총수익 +173.0%, 최대 낙폭 41.45% |
| `trend_200` | 전 거래일 KODEX 200이 200거래일 평균보다 높으면 KODEX 200, 아니면 인버스를 들고 간다. 바꿀 때는 종가에 갈아탄다 | v2 일봉(2009~2026) 총수익 +65.7%, 최대 낙폭 67.17%, 앞 기간 −29.96% |
| `last_half_hour` | 개장 30분 뒤 KODEX 200이 기준가보다 높으면 마감 30분 전에 사서 그날 종가에 판다 | v2 1분봉(2022~2026) 총수익 −20.08%, 최대 낙폭 20.23% |
| `overnight_trend` | 전 거래일 추세가 위면 KODEX 200, 아래면 인버스를 종가에 사서 다음 거래일 시가에 판다 | v2 일봉(2009~2026) 총수익 −20.41%, 최대 낙폭 71.06% |

## Codex 호출

- 토큰은 `oauth_cli_kit`의 `FileTokenStorage(token_filename="codex.json", data_dir=Path.home() / ".trading-oracle", import_codex_cli=False)`와 `get_token`으로 읽는다. 갱신이 필요하면 라이브러리가 파일 잠금 아래에서 갱신해 같은 파일에 쓴다. trading-oracle도 같은 방식으로 읽어서 함께 쓸 수 있다.
- 요청은 trading-oracle `src/agent/codex.py`의 `_generate`와 같은 주소·헤더·본문으로 보낸다. 바꾸는 것은 다음뿐이다.
  - `originator`와 `User-Agent`는 `rt-trading`이다.
  - `model`은 `gpt-6-sol`이다. 이 값은 `codex.py`의 상수 하나로 둔다.
  - `instructions`는 `v3_head_prompt.txt`이고, 사용자 입력 텍스트는 헤드 입력을 키 순서 그대로, 한글을 그대로 쓴 JSON 문자열이다.
- 한 번 부를 때 시간 제한은 120초다.
- 응답 스트림에서 `response.output_text.delta` 이벤트의 `delta`를 이어 붙여 응답 텍스트로 쓴다. 이벤트에 `response.model`이 있으면 마지막 값을 실제 모델 이름으로 기록한다.
- 오류는 아래처럼 나눈다. 오류 메시지에는 토큰을 넣지 않고, HTTP 오류 본문은 앞 300자만 넣는다.

| 상황 | 코드 | 다시 부르기 |
| --- | --- | --- |
| 토큰 파일이 없거나 토큰에 `account_id`가 없다 | `V3_CODEX_TOKEN_MISSING` | 안 한다 |
| 토큰 갱신 실패 | `V3_CODEX_TOKEN_INVALID` | 한다 |
| HTTP 429 | `V3_CODEX_RATE_LIMITED` | 한다 |
| 그 밖의 HTTP 오류 | `V3_CODEX_HTTP_ERROR` | 한다 |
| 시간 초과, 네트워크 오류 | `V3_CODEX_UNAVAILABLE` | 한다 |
| 스트림의 `error`·`response.failed` 이벤트 | `V3_CODEX_FAILED` | 한다 |

## 헤드 입력

판단일 d의 입력은 아래 키를 이 순서로 가진 JSON 객체다.
수익률과 비율은 `%` 단위 수이고, `decimal`로 계산한 뒤 소수 둘째 자리에서 반올림(`ROUND_HALF_UP`)한다. 가격 자체는 넣지 않는다.

| 키 | 값 |
| --- | --- |
| `decision_date` | d (`YYYY-MM-DD`) |
| `session` | `{"open", "close", "note"}`. 개장·마감은 v1 장 일정 표의 값이고, `note`는 그 표의 `reason`(정상일은 빈 문자열)이다 |
| `month_position` | `{"from_start", "from_end"}`. 호출하는 쪽이 넘긴 그달 거래일 목록에서 d가 앞에서·끝에서 몇째인지다(첫째가 1) |
| `data_through` | 시세 요약에 쓴 마지막 날. `candles_1d`에서 d보다 앞선 마지막 날이다 |
| `market` | 아래 시세 요약 |
| `us_market` | 아래 미국 시세 요약. 미국 시세 파일이 없으면 `null`이다 |
| `subordinates` | 부하마다 `{"id", "description", "backtest", "recent_returns_pct", "holding"}`. 앞의 셋은 `v3_subordinates.json`에서, 뒤의 둘은 호출하는 쪽이 넘긴 부하 계좌 요약에서 온다 |
| `head_account` | 호출하는 쪽이 넘긴 헤드 계좌 요약 `{"subordinate", "holding", "return_since_start_pct", "max_drawdown_pct", "limits"}` |
| `recent_decisions` | d 전의 판단 기록 최근 20개, 오래된 것부터. `{"date", "subordinate", "reason", "status", "head_return_pct"}`이고, `head_return_pct`는 호출하는 쪽이 넘긴 그날 헤드 계좌 수익률이며 정산 전이면 `null`이다 |
| `rule_head` | 호출하는 쪽이 넘긴 `{"return_since_start_pct", "max_drawdown_pct"}` |
| `settled_through` | 호출하는 쪽이 넘긴, 정산을 마친 마지막 거래일. 아직 없으면 `null`이다 |
| `notes` | d 전의 판단 중 상태가 `ok`인 가장 최근 판단의 `notes`. 없으면 빈 문자열이다 |

시세 요약 `market`은 `candles_1d`의 `data_through`까지 수정 종가 `A`로 계산한다. `A_0`이 `data_through`의 값이고, `A_k`는 그보다 k거래일 앞의 값이다.

| 키 | 계산 |
| --- | --- |
| `kodex200.daily_returns_pct` | 최근 20거래일의 `A_i ÷ A_(i+1) − 1`(i = 19 … 0), 오래된 것부터 |
| `kodex200.returns_pct` | k = 1, 5, 20, 60마다 `A_0 ÷ A_k − 1`. 키는 `"1d"`, `"5d"`, `"20d"`, `"60d"` |
| `kodex200.vs_average_pct` | k = 5, 20, 60, 200마다 `A_0 ÷ (A_0 … A_(k−1)의 평균) − 1`. 키는 `"5d"`, `"20d"`, `"60d"`, `"200d"` |
| `kodex200.volatility_20d_pct` | 최근 20거래일 일별 수익률(반올림 전)의 표본 표준편차(분모 19) × √252 |
| `kodex200.drawdown_60d_pct` | `A_0 ÷ max(A_0 … A_59) − 1` |
| `inverse.daily_returns_pct` | KODEX 인버스의 최근 20거래일 일별 수익률, 오래된 것부터 |

미국 시세 요약 `us_market`은 `us_candles_1d`에서 날짜(미국 날짜)가 d보다 앞선 행으로 계산한다. 종목마다 `B_0`이 그 종목의 마지막 행의 수정 종가이고, `B_k`는 그 종목의 행으로 k개 앞의 값이다. 미국 거래일로 세므로 한국 휴장일과 상관없다.

| 키 | 계산 |
| --- | --- |
| `session_date` | 그 행들 중 가장 늦은 날짜 |
| `returns_pct` | `v3_us.SYMBOLS` 순서(SOXX, QQQ, SPY, MU)의 종목마다, k = 1, 5, 20의 `B_0 ÷ B_k − 1`. 키는 `"1d"`, `"5d"`, `"20d"` |

- `candles_1d`와 `us_candles_1d`만 읽고, d와 그 뒤의 행(미국 일봉은 미국 날짜)은 읽지 않는다.
- 계좌 요약은 PRD 03이 정산 결과로 만들어 넘긴다. 이 PRD는 넘겨받은 값을 그대로 넣는다.
- 입력에는 계좌 번호와 토스·Codex 인증 정보가 들어갈 자리가 없다.

## 응답 검증

- 응답 텍스트의 앞뒤 공백을 빼고, 전체를 감싼 코드 블록 표시 하나(```` ```json ```` 또는 ```` ``` ````로 시작하고 ```` ``` ````로 끝남)는 벗긴 뒤 JSON 객체 하나로 읽는다.
- 키는 정확히 `subordinate`, `reason`, `notes`이고 값은 모두 문자열이다.
- `subordinate`는 `v3_subordinates.json`의 id 중 하나이고, `reason`은 500자 이하, `notes`는 2,000자 이하다(파이썬 `len`).
- 하나라도 어기면 형식 오류 `V3_HEAD_RESPONSE_INVALID`이고, 다시 부르기 대상이다.

## 다시 부르기와 마감

- 판단 마감은 d의 개장 시각(v1 장 일정 표)보다 10분 이른 한국 시각이다.
- 첫 호출이 실패하면 1분, 2분, 4분을 기다린 뒤 다시 부르고, 그 뒤로는 4분마다 다시 부른다. 기다리는 시간은 앞 호출이 끝난 때부터 센다.
- 다음 호출을 시작할 시각이 마감과 같거나 늦으면 부르지 않는다. `V3_CODEX_TOKEN_MISSING`이면 다시 부르지 않는다.
- 첫 호출을 시작할 시각이 이미 마감과 같거나 늦으면 부르지도 기록하지도 않는다. 그날은 PRD 03이 정산할 때 `missing`으로 남긴다.
- 상태는 다음과 같다. `ok`가 아니면 부하는 `cash`이고 `reason`과 `notes`는 빈 문자열이다.

| 상태 | 뜻 |
| --- | --- |
| `ok` | 마감보다 이른 시각에 응답을 받아 검증까지 마쳤다 |
| `late` | 검증은 통과했지만 마친 시각이 마감과 같거나 늦다 |
| `invalid` | 마지막 시도가 형식 오류로 끝났다 |
| `failed` | 마지막 시도가 호출 오류로 끝났다 |

- `missing`(판단을 아예 하지 않은 거래일)은 PRD 03이 정산할 때 기록한다.
- 시계와 기다리기는 함수로 받는다. acceptance는 가짜 시계와 기다린 시간을 기록하는 함수로 바꿔 끼운다.

## 판단 기록

`data/v3/paper.sqlite3`의 `decisions` 표에 판단일마다 한 행을 넣는다. 파이썬 기본 `sqlite3`를 쓰고, 표가 없으면 만든다.

| 열 | 내용 |
| --- | --- |
| `date` | 판단일. 기본 키 |
| `status` | 위 상태 |
| `subordinate`, `reason`, `notes` | 기록한 판단 |
| `model` | 요청한 모델(`gpt-6-sol`) |
| `reported_model` | 응답에 온 실제 모델 이름. 없으면 빈 문자열 |
| `attempts` | 호출 횟수 |
| `error` | 마지막 오류 코드. 없으면 빈 문자열 |
| `started_at`, `finished_at` | 첫 호출을 시작한 시각과 판단을 마친 시각(초까지 적은 RFC 3339 한국 시각) |

- 모든 열은 `NOT NULL`이고, `attempts`만 `INTEGER`, 나머지는 `TEXT`로 선언한다.
- `data/v3/head/{d}/`에 호출 전에 `input.json`을 쓰고, 끝나면 마지막 응답 원문 `response.txt`(응답이 없었으면 만들지 않는다)와, 기록한 행과 같은 내용의 `decision.json`을 쓴다. JSON은 들여쓰기 2칸, 한글 그대로, 끝에 줄바꿈이다.
- d의 행이 이미 있으면 Codex를 부르지 않고 그 행을 돌려준다. 기록한 판단은 바꾸지 않는다.

## 가상 fixture

- 일봉 spec에는 시작일, 마지막 날, 휴장일, 종목별 기본 수정 종가, 바꿀 날의 수정 종가, 판단일과 그 뒤에 더할 미래 일봉만 적는다. acceptance가 나머지를 채워, v1과 같은 `candles_1d` 표만 있는 임시 SQLite 파일을 만든다. 원가격은 수정 종가와 같게 둔다.
- 손 계산을 쉽게 하려고 대부분의 날은 기본 가격 그대로 두고, 판단일 앞 마지막 61거래일 안에서만 가격을 바꾼다. 200거래일 평균을 계산하도록 거래일은 200일보다 많이 둔다.
- 미국 일봉 spec도 같은 방법으로 적고, acceptance가 `us_candles_1d` 표만 있는 임시 SQLite 파일을 만든다. 종가는 소수가 있는 문자열이다.
  - 2025-12-31은 한국만 쉬고 미국은 연다. 미국 1거래일 앞이 그날이라, 한국 거래일로 세면 틀리게 한다.
  - 1·5·20개 앞의 값과 그 바로 옆의 값을 다르게 둔다. 하나 어긋나게 세면 틀리게 한다.
  - `+0.125%`와 `−0.125%`를 넣어 `ROUND_HALF_UP` 경계를 확인한다.
- 가짜 Codex는 `httpx.MockTransport`로 만든다. 사례마다 시도별 응답(SSE 본문, HTTP 상태, 네트워크 오류, 걸린 시간)을 적고, 요청 본문과 헤더를 기록해 확인한다.
- 토큰은 임시 디렉터리에 만든 가짜 토큰 파일(만료가 먼 `access`, `refresh`, `account_id`)로 읽는다.
- 장 일정은 실제 표를 쓴다. 정상일 경계는 2026-01-05(마감 08:50), 10시 개장일 경계는 2026-01-02(연초 첫 거래일, 마감 09:50)로 본다.
- 계좌 요약과 그달 거래일 목록은 fixture에 직접 적는다.
- 사례마다 기대값을 손으로 계산해 함께 적고 그대로 비교한다. 입력의 수는 반올림한 값이라 그대로 비교한다.

## 필수 Acceptance

SPEC 9절의 "헤드" 항목을 맡는다.

- 판단일과 그 뒤의 일봉(미국 일봉 포함)을 바꿔도 헤드 입력이 손으로 계산한 기대 입력과 같다. 미국 시세 수익률은 미국 거래일로 세고, 반올림은 `ROUND_HALF_UP`이다. Codex에 보낸 본문은 지시문과 헤드 입력뿐이다. 가짜 토큰과 계좌 id 문자열이 입력, 요청 본문, 기록, 출력 어디에도 없고, 입력에 가격 자체가 없다.
- 올바른 응답(코드 블록으로 감싼 응답 포함)은 그 부하와 `ok`로 기록된다. 목록 밖 id, JSON이 아닌 응답, 다른 키, 501자 `reason`, 2,001자 `notes`는 형식 오류다. 500자 `reason`과 2,000자 `notes`는 통과한다.
- 다시 부르는 간격이 1·2·4·4분이다. 마감보다 일찍 검증까지 마치면 `ok`, 마감과 같거나 늦게 마치면 `late`, 마감까지 형식 오류나 호출 오류로 끝나면 `invalid`나 `failed`이고, 부하는 `cash`다. 2026-01-05 08:50과 2026-01-02 09:50을 경계로 확인한다. 토큰 파일이 없으면 다시 부르지 않고 `failed`다.
- `ok` 판단의 `notes`가 다음 판단 입력에 들어간다. 그 사이에 실패한 날이 있어도 마지막 `ok` 판단의 노트가 들어간다.

## 완료 조건

- `uv run rt-trading v3-acceptance --prd 01`이 exit 0이다.
- `cli.py`를 바꾸므로 `uv run rt-trading acceptance`, `uv run rt-trading v0-acceptance`, `uv run rt-trading v2-acceptance`로 기존 명령이 그대로인지 확인한다.
- 실제 Codex에 `gpt-6-sol`로 시험 호출을 한 번 해서 응답이 오는지 확인한다(SPEC 10절). 확인용 코드는 남기지 않는다.

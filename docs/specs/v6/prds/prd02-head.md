# PRD: v6 PRD 02 목표 보유 헤드
> **상태**: 승인(2026-11-09, 사용자가 정한 대로 오라클 검토 통과로 승인)
> 상위 SPEC: [v6 SPEC](../SPEC.md)

## 의존성

- Codex 클라이언트(`codex.ask`, `codex.credentials`). `ask`에 키워드 전용 선택 인자 `model`(기본 `codex.MODEL`)을 더해 v6가 자기 모델을 넘긴다. v3~v5는 넘기지 않으므로 그대로다.
- v3 헤드의 응답 감싸기 처리(`v3_head.FENCE`)와 `%` 반올림(`v3_head.pct`: 비율 × 100을 소수 둘째 자리 `ROUND_HALF_UP`)
- v4 뉴스(`v4_news.fetch`, `v4_news.read_credentials`). 검색어는 `v4_news.QUERIES`(`코스피`, `뉴욕증시`, `환율`, `반도체`)로 SPEC 3.2와 같다.
- PRD 01의 세션(`sessions`), 종목 정보, 후보 목록(`candidates`)

## 문제

헤드는 매시 C까지 받은 값으로만 입력을 만들고, E 전에 응답을 받아 검증해야 한다. 늦은 값이 입력에 들어가면 미래를 보고 판단하게 되고, 검증이 빠지면 한도를 어기는 목표가 실행된다.
판단에 실패한 차례에는 사고팔 목표가 없어야 한다.

## 목표

1. 판단 차례의 T·C·E를 정한다(SPEC 3.1).
2. C까지 받은 값으로 후보, 평가 자산과 계좌 요약, 헤드 입력을 만든다(SPEC 3.2).
3. Codex를 두 번까지 부르고 E까지만 기다리며, 응답을 검증해 상태를 정한다(SPEC 3.3·3.4).
4. 판단 기록의 꼴과 실행할 목표를 정한다.
5. v6 지시문을 둔다.

## 범위 밖

- 랭킹·종목 정보 받기(PRD 01), 현재가·환율 받기, 10분 평가, 체결(PRD 03)
- 언제 판단하는지, 받기와 마감, 저장소에 쓰기와 잠금, `missing` 기록(PRD 04)

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/v6_head.py` | 시각, C까지의 후보, 평가 자산과 계좌 요약, 입력 만들기, 호출과 검증, 판단 기록, 실행할 목표 |
| `src/rt_trading/v6_head_prompt.txt` | 헤드 지시문. SPEC 부록 B 코드 블록 안 글 그대로다 |
| `src/rt_trading/codex.py` | `ask`의 키워드 전용 선택 인자 `model` |
| `src/rt_trading/v6_acceptance.py` | PRD 02 사례 |
| `docs/specs/v6/fixtures/prd02-head.json` | 입력 재료(받은 시각이 붙은 랭킹·종목 정보·현재가·환율, 기사, 계좌 상태, 판단 기록), 가짜 Codex 응답, 손으로 적은 기대 입력과 기록 |

## 시각과 C까지 받은 값

- 판단 차례 T는 한국 날짜 d의 `HH:40`이고, C = T + 2분, E = T + 5분이다. 차례 이름은 `YYYY-MM-DD HH:MM`이다.
- `MODEL = "gpt-6-sol"`이다. v6 코드에 두고 `codex.MODEL`을 따라가지 않는다.
- 입력은 C까지 받은 값으로만 만든다. 받은 값(랭킹, 종목 정보, 현재가, 환율)은 `(값, observed_at)`이고, `observed_at`이 C보다 늦으면 받지 않은 것으로 본다. 아래 함수가 모두 이 규칙으로 거른다.

## 후보

- `candidates_at(cutoff, rankings, infos, saved_infos, holdings, open_at_E, total, used)`는 C까지 받은 이번 랭킹과 종목 정보로 PRD 01 `candidates`를 부른다. `total`과 `used`는 아래 `valuation`의 A_C와 쓴 값이다.
  - 이번 종목 정보를 C까지 받았으면 랭킹 종목은 이번 값을 쓰고, 이번 값에 없는 보유 종목은 `observed_at`이 C 이하인 마지막 저장값(`saved_infos`)을 쓴다.
  - 이번 종목 정보를 C까지 받지 못했으면 랭킹은 쓰지 않고, 보유 종목만 마지막 저장값으로 넣는다(SPEC 2.2).
  - 한 시장의 랭킹을 C까지 받지 못했으면 그 시장의 새 후보는 없다.
  - 1주 값은 보유 종목이면 `used`의 값, 아니면 랭킹 `last_price`이고, 미국은 `used`의 `USDKRW`를 곱한다(없으면 모름). 1주 값이 `total` × 0.4보다 크거나 모르는 랭킹 종목은 `skip`으로 넘겨 고르지 않는다. 보유 종목은 PRD 01대로 뒤에 들어간다.
  - 후보마다 끝에 `one_share_pct`(1주 값 ÷ `total`을 `v3_head.pct`로, 모르면 `null`)를 붙인다.
- `account`와 `build_input`은 이 같은 후보 목록을 받고, 후보를 다시 만들지 않는다.

## 평가 자산과 계좌 요약

- `equity(krw, usd, positions, prices, rate)`는 원화 평가 자산 A(정수 원)다. A = 원화 현금 + Σ 국내 수량 × 가격 + (달러 현금 + Σ 미국 수량 × 가격) × `rate`를 원 미만 `ROUND_HALF_UP`. 원화는 정수, 달러와 환율은 `Decimal`이다. PRD 03의 평가도 이 함수를 쓴다.
- 현재가와 환율은 C까지 받은 이번 값, 없으면 마지막으로 받은 값(계좌 상태의 마지막 값)이다.
- `valuation(state, observed, cutoff)`는 (A_C, 쓴 값)을 돌려준다. 받은 적이 없는 값이 있으면 `V6_VALUE_MISSING`이다.
- `account(state, observed, cutoff, bases, history, blocked, candidates)`는 `valuation`과 같은 (A_C, 쓴 값)과 계좌 요약을 돌려준다. 요약은 아래 키를 이 순서로 가진다. 비율은 `v3_head.pct`로 쓴다.

| 키 | 값 |
| --- | --- |
| `krw_cash_pct`, `usd_cash_pct` | 원화 현금 ÷ A, 달러 현금 × 환율 ÷ A |
| `holdings` | 종목 코드 순으로 `{"symbol", "name", "market", "weight_pct", "return_pct", "tradable_now", "exit_only"}`. `weight_pct`는 그 종목 원화 평가 ÷ A, `return_pct`는 가격 ÷ 평균 단가 − 1(그 종목 통화). `tradable_now`·`exit_only`는 후보 목록의 값이다 |
| `return_since_start_pct` | A ÷ 500,000 − 1 |
| `today_return_pct`, `week_return_pct` | A ÷ 하루 기준 − 1, A ÷ 한 주 기준 − 1(기준은 PRD 03) |
| `max_drawdown_pct` | 500,000을 앞에 붙인 T까지의 10분 평가 자산 목록 끝에 A를 붙여 계산한 `max(1 − A_t ÷ max(A_0, …, A_t))` |
| `buy_blocked` | 지금 매수가 막힌 이유 목록(PRD 03): `daily_limit`, `weekly_limit`, `cumulative_limit`, `risk_data_missing`, `forced_sale` 순서 |

## 헤드 입력

`build_input(slot, open_at_E, ends_at_E, account_summary, candidates, news, decisions)`는 아래 키를 이 순서로 가진 JSON 객체다.

| 키 | 값 |
| --- | --- |
| `decision` | `{"time": T, "input_cutoff": C, "fill_at": E, "sessions": {"KR": …, "US": …}}`. 시각은 `YYYY-MM-DDTHH:MM+09:00`이고, 시장마다 E에 열린 세션 `{"name", "ends_at"}` 또는 `null`이다 |
| `account` | 위 계좌 요약 |
| `candidates` | `candidates_at`의 후보 목록 |
| `news` | `v4_news.fetch`에 cutoff C, 받기 마감 C로 받은 값 |
| `recent_decisions` | T보다 앞선 판단 기록 최근 10개, 오래된 것부터 `{"time", "status", "positions", "strategy", "reason"}` |
| `notes` | T보다 앞선 `ok` 판단 가운데 가장 최근 판단의 `notes`. 없으면 빈 문자열이다 |
| `limits` | `{"max_positions": 5, "max_weight": 0.4, "max_total_weight": 1, "loss_limits_pct": {"daily": -2, "weekly": -5, "cumulative": -20}}` |

- 입력에는 가격 자체, 계좌 번호, 토스·네이버·Codex 인증 정보가 들어갈 자리가 없다.
- `observations`는 입력에 쓴 값의 받은 시각을 남긴다: `{"rankings": {시장: [observed_at, 썼는지]}, "instruments": {종목: [observed_at, "current" 또는 "saved", 썼는지]}, "prices": {종목: [observed_at, 이번에 받았는지]}, "rate": [observed_at, 이번에 받았는지], "news": [뉴스 전체가 준비된 observed_at, 썼는지, 뉴스 기록 파일 경로]}`. 받지 못한 값은 `null`이고, 가격 자체와 인증 정보는 넣지 않는다.

## Codex 호출

- `decide(head_dir, slot, content, observations, candidates, http, credentials, clock, sleep)`는 호출 전에 `head_dir/{d}/{HHMM}/input.json`에 `{"input": content, "observed_at": observations}`를 쓰고 Codex를 부른다. Codex에는 `content`만 보낸다. 사용자 입력 텍스트는 `content`를 키 순서 그대로, 한글 그대로 쓴 JSON 문자열이고, 두 번 부르면 두 번 모두 같은 텍스트다. 지시문은 `v6_head_prompt.txt`, 모델은 `MODEL`이다.
- 첫 호출은 입력을 다 만든 때(C 전) 시작한다. 첫 호출이 호출 오류나 형식 오류로 끝나면 15초 뒤 한 번 더 부른다. 15초 뒤가 E와 같거나 늦거나, 토큰이 없으면(`V3_CODEX_TOKEN_MISSING`) 다시 부르지 않는다.
- 호출은 따로 돌리고 응답은 E까지만 기다린다. E가 되면 더 기다리지 않고, 그 뒤에 온 응답은 쓰지 않는다.
- 시작할 때 이미 E와 같거나 늦으면 부르지 않고 `None`을 돌려준다. 그 밖에는 (기록할 행, 마지막 응답 원문 또는 `None`)을 돌려준다. 저장소에는 쓰지 않는다(PRD 04가 잠금 안에서 `record`로 쓴다).

## 응답 검증

`parse(text, candidates)`는 아래를 하나라도 어기면 `V6_HEAD_RESPONSE_INVALID`다.

- 앞뒤 공백을 뺀 응답 전체가 코드 블록 하나로 감싸여 있으면 그 안을 쓴다(v3와 같다). JSON 객체 하나이고 키가 `positions`, `strategy`, `reason`, `notes` 넷이다. 수는 `Decimal`로 읽는다.
- `positions`는 목록이고, 항목은 키가 `symbol`, `weight` 둘인 객체다. `symbol`은 후보에 있고 `exit_only`가 아닌 종목 코드이며 겹치지 않고, 항목은 5개 이하다. `weight`는 참·거짓이 아닌 수이고 0 < `weight` ≤ 0.4이며, 합이 1 이하다(`Decimal`로 정확히 더한다).
- `strategy`, `reason`, `notes`는 글자이고 길이가 40, 500, 1,000 이하다.

## 상태와 기록

| 상태 | 뜻 |
| --- | --- |
| `ok` | E 전에 응답을 받아 검증까지 마쳤다 |
| `invalid` | 마지막 호출이 형식 오류로 끝났다 |
| `failed` | 마지막 호출이 호출 오류로 끝났다 |
| `late` | E가 될 때까지 호출이 끝나지 않았다(E 뒤 응답) |
| `missing` | 그 차례에 판단하지 않았다. PRD 04가 넣는다 |

- 판단 행은 `data/v6/paper.sqlite3`의 `decisions` 표에 차례마다 하나다. 열은 `time`(차례 이름, 기본 키), `status`, `positions`(JSON 목록 글자), `strategy`, `reason`, `notes`, `model`, `reported_model`, `attempts`, `error`, `started_at`, `finished_at`이고 모두 `NOT NULL`이다. `attempts`만 `INTEGER`다. `ok`가 아니면 `positions`는 `[]`, 세 글자 열은 빈 문자열이다.
- `record(conn, head_dir, row, raw)`는 PRD 04가 연 트랜잭션 안에서 행을 넣고, `head_dir/{d}/{HHMM}/`에 `response.txt`(`raw`가 있을 때)와 행과 같은 `decision.json`을 쓴다. JSON은 들여쓰기 2칸, 한글 그대로, 끝에 줄바꿈이다. 기록한 판단은 바꾸지 않는다.
- `targets(row)`는 `ok`면 `{종목: Decimal 비중}`, 아니면 `None`이다. 체결은 이 값으로만 한다(PRD 04).

## 가상 fixture

- 가짜 Codex와 가짜 토큰은 v4와 같은 방법(`httpx.MockTransport`, 임시 토큰 파일)이고, 응답마다 걸린 시간을 적는다. 받은 요청 본문을 기록한다. 시계는 가짜 시계이고 걸린 시간과 기다린 시간만큼 움직인다.
- 입력 재료는 받은 시각을 붙여 적는다. C 전에 받은 값과 같은 종목·같은 키로 C 뒤에 받은 다른 값, C 뒤에 나온 기사를 함께 넣는다. 계좌 상태에는 원화·달러 현금과 국내·미국 보유, 마지막 값, 기준, 평가 목록, 막힌 이유를 적는다. 가짜 계좌번호·인증 정보 문자열을 계좌 상태와 환경 변수에 둔다.
- 기대 입력과 기대 상태는 손으로 적어 그대로 비교한다.

## 필수 Acceptance

SPEC 9절의 "헤드" 항목을 맡는다.

- C 뒤에 받은 랭킹·종목 정보·현재가·환율과 C 뒤에 나온 기사를 바꿔도 입력이 같다. 입력에 가격 자체, 계좌 번호, 인증 정보가 없다. Codex를 두 번 부르면 두 호출의 입력이 같다. 1주 값이 C 평가 자산의 40%보다 큰 랭킹 종목은 후보에 없고, 그런 보유 종목은 후보에 남는다.
- 목록 밖 종목, `exit_only` 종목, 6종목, 비중 0.41, 비중 합 1.01, 겹친 종목은 형식 오류다. E 뒤 응답은 `late`다. 첫 호출이 실패하면 한 번만 더 부른다. 실패한 차례에는 사고팔지 않는다(`targets`가 `None`).

## 완료 조건

- `uv run rt-trading v6-acceptance --prd 02`와 `--prd 01`이 exit 0이다.
- `codex.py`를 바꾸므로 `v3-acceptance`, `v4-acceptance`, `v5-acceptance`가 그대로 통과한다.
- v6 지시문과 가상 입력으로 Codex를 한 번 시험 호출해 형식에 맞는 응답이 오는지 확인한다. 시험 호출은 서버에서 돌고 있는 v5 컨테이너 안에서 기존 Codex 토큰 파일(잠금 아래 갱신)로 하고, v5의 판단 시간 밖에서 한다. 맥북에서는 부르지 않고, v5 서비스를 다시 만들거나 다시 켜지 않는다. 확인용 코드는 남기지 않는다.

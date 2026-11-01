# PRD: v4 PRD 01 뉴스 검색
> **상태**: 초안, 구현 전
> 상위 SPEC: [v4 SPEC](../SPEC.md)

## 의존성

- NAVER API HUB 뉴스 검색 API(API 가이드 2026-09-17판). `GET https://naverapihub.apigw.ntruss.com/search/v1/news`, 인증 헤더 `X-NCP-APIGW-API-KEY-ID`(Client ID)와 `X-NCP-APIGW-API-KEY`(Client Secret)
- 응답 본문은 `{"lastBuildDate", "total", "start", "display", "items"}`이고, `items`의 기사는 `title`, `originallink`, `link`, `description`, `pubDate`를 가진다. `title`과 `description`은 검색어와 맞는 부분을 `<b>` 태그로 감싸고, `pubDate`는 `Sun, 01 Nov 2026 14:58:00 +0900` 꼴이다(2026-11-01 시험 호출로 확인)
- `.env`의 `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`(SPEC 10절)

## 문제

헤드가 판단할 때 판단 시각 전에 나온 뉴스를 함께 보려면, 정해 둔 검색어로 최신 기사를 받고, 판단 시각 뒤의 기사를 빼고, 헤드 입력에 넣을 꼴로 다듬어야 한다.
뉴스를 받지 못해도 판단은 해야 하므로, 실패는 판단을 막지 않고 기록만 남긴다.

## 목표

1. 정해 둔 검색어로 NAVER API HUB 뉴스 검색을 부른다.
2. 판단 시각 전 기사만 골라 중복을 빼고 태그를 지워, 헤드 입력의 `news`를 만든다(SPEC 3.2).
3. 받은 응답과 입력에 넣은 기사를 저장한다(SPEC 7절).
4. `v4-acceptance [--prd NN]` 실행기를 만든다. PRD 04에서 모든 PRD를 실행하게 완성한다.

## 범위 밖

- 헤드 입력의 다른 항목과 Codex 호출(PRD 02)
- 판단 시각과 받기 마감(T + 2분)을 정하는 것, `v4-daily`·`v4-decide`(PRD 04)
- 기사 본문, 웹 검색

## 구현 표면

| 경로 | 책임 |
| --- | --- |
| `src/rt_trading/v4_news.py` | 인증 정보 읽기, 검색어마다 호출, 기사 고르기와 다듬기, 저장 |
| `src/rt_trading/v4_acceptance.py` | PRD별 사례 등록·실행, 결과 출력 |
| `src/rt_trading/cli.py` | `v4-acceptance [--prd NN]` |
| `docs/specs/v4/fixtures/prd01-news.json` | 검색어마다 가짜 응답, 판단 시각, 손으로 정한 기대 기사 목록 |

- `v4-acceptance`는 v3와 같은 JSON으로 사례별 결과와 요약을 출력한다. 실행한 사례가 모두 통과하면 exit 0, 아니면 exit 1이다. `--prd`가 없으면 구현된 모든 v4 PRD를 실행한다.

## 검색어와 호출

- 검색어는 `v4_news.QUERIES`의 `코스피`, `뉴욕증시`, `환율`, `반도체`이고 이 순서로 부른다. 검색어를 바꾸면 판단 방식이 바뀌므로 새 기간으로 센다(SPEC 6절).
- 검색어마다 `query`, `display=10`, `start=1`, `sort=date`, `format=json`으로 한 번 부른다. 실패해도 다시 부르지 않는다.
- 인증 정보는 환경 변수에서 읽고, 없으면 저장소 루트의 `.env`에서 읽는다(토스 인증 정보와 같은 방법).
- 호출하는 쪽은 판단 시각 T, 받기 마감, 시계를 준다. 검색어를 부르기 전에 받기 마감이 지났으면 그 검색어는 부르지 않는다. 한 번 부를 때 시간 제한은 10초와 받기 마감까지 남은 시간 중 짧은 쪽이다.
- 검색어마다 결과를 아래처럼 나눈다. 오류 메시지에는 인증 정보를 넣지 않고, HTTP 오류 본문은 앞 300자만 넣는다.

| 상황 | 코드 |
| --- | --- |
| 인증 정보가 없다. 어느 검색어도 부르지 않는다 | `V4_NEWS_CREDENTIALS_MISSING` |
| HTTP 상태가 200이 아니다 | `V4_NEWS_HTTP_ERROR` |
| 시간 초과, 네트워크 오류 | `V4_NEWS_UNAVAILABLE` |
| 받기 마감이 지나 부르지 않았다 | `V4_NEWS_SKIPPED` |
| 본문이 JSON 객체가 아니거나, `items`가 목록이 아니거나, 기사에 다섯 키 중 하나가 없거나 문자열이 아니거나, `pubDate`를 읽을 수 없다. 그 검색어의 기사를 모두 버린다 | `V4_NEWS_RESPONSE_INVALID` |

## 기사 고르기

판단 시각 T에 대해 다음 순서로 고른다. 개장 전 판단의 T는 입력을 만드는 시각이고, 장중·마감 전 판단의 T는 판단 차례다(PRD 02).

1. 성공한 검색어의 기사를 검색어 순서, 응답 순서대로 모은다.
2. `pubDate`를 RFC 2822 날짜로 읽어 한국 시각으로 바꾸고, T보다 이른 기사만 남긴다. T와 같은 시각의 기사는 뺀다.
3. 같은 기사는 먼저 모은 것 하나만 남긴다. 기사 열쇠는 `originallink`이고, 빈 문자열이면 `link`다.
4. 나온 시각이 늦은 것부터 정렬하고(같으면 모은 순서) 앞의 30개만 남긴다.
5. 기사마다 아래 키를 이 순서로 가진 객체로 바꾼다.

| 키 | 값 |
| --- | --- |
| `published_at` | 나온 한국 시각 `YYYY-MM-DDTHH:MM:SS+09:00` |
| `source` | 기사 열쇠 URL의 호스트(`www.news1.kr`) |
| `title` | `title`에서 태그를 지우고 HTML 문자 참조를 푼 뒤 앞뒤 공백을 뺀 것 |
| `summary` | `description`을 같은 방법으로 다듬은 것 |

- 태그는 `<`로 시작해 `>`로 끝나는 부분이다. 태그를 먼저 지우고 문자 참조를 푼다. 그래서 `&lt;b&gt;`는 글자 `<b>`로 남는다.
- 모든 검색어가 실패하면(`V4_NEWS_SKIPPED` 포함) `news`는 `null`이다. 하나라도 성공했으면 고른 기사가 없어도 빈 목록이다.

## 저장

- 호출하는 쪽이 정한 파일(`data/v4/news/{날짜}/{차례 HHMM}.json`)에 다음 객체를 쓴다. JSON은 들여쓰기 2칸, 한글 그대로, 끝에 줄바꿈이다.

| 키 | 값 |
| --- | --- |
| `cutoff` | T(초까지 적은 RFC 3339 한국 시각) |
| `queries` | 검색어 순서로 `{"query", "error", "response"}`. `error`는 오류 코드와 설명이고 성공하면 빈 문자열이다. `response`는 받은 본문을 JSON으로 읽은 값이고, JSON이 아니면 본문 문자열, 받지 못했으면 `null`이다 |
| `news` | 입력에 넣은 값 |

- 인증 정보는 파일과 출력 어디에도 쓰지 않는다.

## 가상 fixture

- 가짜 NAVER API HUB는 `httpx.MockTransport`로 만든다. 검색어마다 응답(본문, HTTP 상태, 네트워크 오류, 걸린 시간)을 적고, 받은 요청의 주소·쿼리·헤더를 기록해 확인한다. 시계는 가짜 시계이고, 걸린 시간만큼 움직인다.
- 인증 정보는 가짜 문자열을 환경 변수로 준다. 인증 정보가 없는 사례는 환경 변수와 `.env`가 모두 없는 임시 루트를 쓴다.
- 기사에는 T보다 1분 늦은 기사, T와 같은 시각의 기사, 두 검색어에 같은 `originallink`로 나온 기사, `originallink`가 빈 기사, `<b>`·`&quot;`·`&amp;`·`&lt;b&gt;`가 든 제목, `+0000`으로 적힌 `pubDate`를 넣는다.
- 고른 기사가 30개를 넘는 사례 하나를 둔다. 31번째와 30번째 기사의 시각을 다르게 둔다.
- 기대 기사 목록은 손으로 적어 그대로 비교한다.

## 필수 Acceptance

SPEC 9절의 "뉴스" 항목을 맡는다.

- T 뒤와 T와 같은 시각의 기사는 없고, 같은 원문 링크의 기사는 한 번만 있으며, 제목·요약의 태그가 지워지고 문자 참조가 풀린다. `+0000`으로 적힌 기사도 한국 시각으로 바뀌어 고른다. 최신 30개만 늦은 것부터 들어간다.
- 요청은 정해 둔 주소와 쿼리, 두 인증 헤더로 간다. 인증 정보 문자열이 저장한 파일과 출력에 없다.
- 한 검색어가 HTTP 오류나 형식 오류로 실패해도 나머지 검색어로 만든다. 모든 검색어가 실패하면 `null`이다. 인증 정보가 없으면 아무것도 부르지 않고 `null`이다. 받기 마감이 지난 뒤의 검색어는 부르지 않는다.

## 완료 조건

- `uv run rt-trading v4-acceptance --prd 01`이 exit 0이다.
- `cli.py`를 바꾸므로 `uv run rt-trading acceptance`, `v0-acceptance`, `v2-acceptance`, `v3-acceptance`로 기존 명령이 그대로인지 확인한다.
- 실제 NAVER API HUB에 검색어 하나로 시험 호출을 해 응답 형식이 이 PRD와 같은지 확인한다. 확인용 코드는 남기지 않는다.

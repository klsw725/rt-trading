"""v4 뉴스 검색(v4 SPEC 3.2, PRD 01). NAVER API HUB 뉴스 검색으로 판단 시각 전 기사를 골라 헤드 입력의 news를 만든다."""

import html
import json
import os
import re
from collections.abc import Callable
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from rt_trading.toss import KST

URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
QUERIES = ("코스피", "뉴욕증시", "환율", "반도체")  # 바꾸면 판단 방식이 바뀌므로 새 기간이다(SPEC 6절)
DISPLAY = 10  # 검색어마다 받는 기사 수
LIMIT = 30  # 헤드 입력에 넣는 기사 수
TIMEOUT = 10  # 한 번 부를 때 시간 제한(초)
KEYS = ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")
FIELDS = ("title", "originallink", "link", "description", "pubDate")
TAG = re.compile(r"<[^>]*>")


def read_credentials(root: Path) -> tuple[str, str] | None:
    """환경 변수에서 NAVER API HUB 인증 정보를 읽고, 없으면 저장소 루트의 .env에서 읽는다. 하나라도 없으면 None이다."""
    values = {}
    if (root / ".env").exists():
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    client_id, secret = (os.environ.get(key) or values.get(key) for key in KEYS)
    return (client_id, secret) if client_id and secret else None


def fetch(
    http: httpx.Client,
    credentials: tuple[str, str] | None,
    cutoff: datetime,
    deadline: datetime,
    clock: Callable[[], datetime],
    path: Path,
) -> tuple[list[dict] | None, list[str]]:
    """검색어마다 부르고 cutoff 전 기사를 골라 path에 기록한다. (헤드 입력의 news, 실패한 검색어마다 '검색어: 오류').

    모든 검색어가 실패하면 news는 None이다. deadline이 지난 뒤의 검색어는 부르지 않는다.
    """
    records, articles = [], []
    for query in QUERIES:
        found, record = _query(http, credentials, query, deadline, clock)
        records.append(record)
        articles += found or []
    news = select(articles, cutoff) if any(not record["error"] for record in records) else None
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {"cutoff": cutoff.isoformat(timespec="seconds"), "queries": records, "news": news}
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return news, [f"{record['query']}: {record['error']}" for record in records if record["error"]]


def select(articles: list[tuple[datetime, dict]], cutoff: datetime) -> list[dict]:
    """cutoff보다 이른 기사를 원문 링크로 한 번씩만 남기고, 늦은 것부터 LIMIT개를 헤드 입력 꼴로 바꾼다."""
    kept, seen = [], set()
    for published, item in articles:
        key = item["originallink"] or item["link"]
        if published < cutoff and key not in seen:
            seen.add(key)
            kept.append((published, key, item))
    kept.sort(key=lambda entry: entry[0], reverse=True)  # 같은 시각은 모은 순서를 지킨다
    return [
        {
            "published_at": published.isoformat(timespec="seconds"),
            "source": _host(key),
            "title": _clean(item["title"]),
            "summary": _clean(item["description"]),
        }
        for published, key, item in kept[:LIMIT]
    ]


def _host(url: str) -> str:
    """주소의 호스트. 읽을 수 없는 주소(짝이 맞지 않는 대괄호 등)는 빈 문자열이라 뉴스 때문에 판단이 멈추지 않는다."""
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def _query(
    http: httpx.Client, credentials: tuple[str, str] | None, query: str, deadline: datetime, clock: Callable[[], datetime]
) -> tuple[list[tuple[datetime, dict]] | None, dict]:
    """검색어 하나를 부른다. (기사 목록 또는 실패면 None, 저장할 기록). 기록과 오류에는 인증 정보를 넣지 않는다."""
    record = {"query": query, "error": "", "response": None}
    if credentials is None:
        record["error"] = "V4_NEWS_CREDENTIALS_MISSING: 환경 변수나 .env에 NAVER_CLIENT_ID·NAVER_CLIENT_SECRET이 없다"
        return None, record
    left = (deadline - clock()).total_seconds()
    if left <= 0:
        record["error"] = "V4_NEWS_SKIPPED: 받기 마감이 지나 부르지 않았다"
        return None, record
    params = {"query": query, "display": DISPLAY, "start": 1, "sort": "date", "format": "json"}
    headers = {"X-NCP-APIGW-API-KEY-ID": credentials[0], "X-NCP-APIGW-API-KEY": credentials[1]}
    try:
        response = http.get(URL, params=params, headers=headers, timeout=min(TIMEOUT, left))
    except httpx.TransportError as error:  # 시간 초과도 여기에 든다
        record["error"] = f"V4_NEWS_UNAVAILABLE: {type(error).__name__}"
        return None, record
    try:
        record["response"] = response.json()
    except ValueError:
        record["response"] = response.text
    if response.status_code != 200:
        record["error"] = f"V4_NEWS_HTTP_ERROR: HTTP {response.status_code}: {response.text[:300]}"
        return None, record
    try:
        return _articles(record["response"]), record
    except ValueError as error:
        record["error"] = f"V4_NEWS_RESPONSE_INVALID: {error}"
        return None, record


def _articles(body: object) -> list[tuple[datetime, dict]]:
    """응답 본문의 기사마다 (나온 한국 시각, 기사). 형식이 틀리면 ValueError다."""
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise ValueError("본문이 items 목록을 가진 JSON 객체가 아니다")
    articles = []
    for item in body["items"]:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in FIELDS):
            raise ValueError(f"기사에 {', '.join(FIELDS)} 문자열이 다 있지 않다")
        try:
            published = parsedate_to_datetime(item["pubDate"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"pubDate {item['pubDate']!r}를 읽을 수 없다") from error
        if published.tzinfo is None:
            raise ValueError(f"pubDate {item['pubDate']!r}에 시간대가 없다")
        articles.append((published.astimezone(KST), item))
    return articles


def _clean(text: str) -> str:
    """태그를 지우고 HTML 문자 참조를 푼다. 태그를 먼저 지우므로 &lt;b&gt;는 글자 <b>로 남는다."""
    return html.unescape(TAG.sub("", text)).strip()

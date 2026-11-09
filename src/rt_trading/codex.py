"""Codex Responses API 호출(v3 PRD 01). trading-oracle과 같은 OAuth 토큰 파일을 함께 쓴다."""

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

import httpx
from oauth_cli_kit import get_token
from oauth_cli_kit.storage import FileTokenStorage

URL = "https://chatgpt.com/backend-api/codex/responses"
MODEL = "gpt-6-sol"
TOKEN_DIR = Path.home() / ".trading-oracle"  # 토큰 파일은 ~/.trading-oracle/auth/codex.json
TIMEOUT = 120  # 초


class CodexError(Exception):
    """오류 코드와 설명. 토큰은 담지 않는다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class Reply(NamedTuple):
    text: str
    model: str  # 응답 이벤트에 온 실제 모델 이름. 없으면 빈 문자열


def credentials(data_dir: Path) -> tuple[str, str]:
    """(access 토큰, account_id). 만료가 가까우면 라이브러리가 파일 잠금 아래에서 갱신해 같은 파일에 쓴다."""
    storage = FileTokenStorage(token_filename="codex.json", data_dir=data_dir, import_codex_cli=False)
    if storage.load() is None:
        raise CodexError(
            "V3_CODEX_TOKEN_MISSING",
            f"{storage.get_token_path()}에 토큰이 없다. trading-oracle에서 uv run main.py codex-login으로 로그인한다",
        )
    try:
        token = get_token(storage=storage)
    except Exception as error:
        raise CodexError("V3_CODEX_TOKEN_INVALID", f"토큰을 갱신하지 못했다({type(error).__name__})") from error
    if not token.account_id:
        raise CodexError("V3_CODEX_TOKEN_MISSING", "토큰에 account_id가 없다. 다시 로그인한다")
    return token.access, token.account_id


def ask(http: httpx.Client, access: str, account_id: str, instructions: str, text: str, *, model: str | None = None) -> Reply:
    """trading-oracle src/agent/codex.py와 같은 주소·헤더·본문으로 한 번 부른다. model이 없으면 MODEL이다(v6는 자기 모델을 넘긴다)."""
    headers = {
        "Authorization": f"Bearer {access}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "rt-trading",
        "User-Agent": "rt-trading",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    body = {
        "model": model or MODEL,
        "store": False,
        "stream": True,
        "instructions": instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
        "text": {"verbosity": "medium"},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": hashlib.sha256(
            json.dumps({"s": instructions, "u": text}, ensure_ascii=True, sort_keys=True).encode()
        ).hexdigest(),
    }
    try:
        with http.stream("POST", URL, headers=headers, json=body, timeout=TIMEOUT) as response:
            if response.status_code == 429:
                raise CodexError("V3_CODEX_RATE_LIMITED", "ChatGPT 계정 사용량 한도를 넘었다")
            if response.status_code != 200:
                raw = response.read().decode("utf-8", "ignore")
                raise CodexError("V3_CODEX_HTTP_ERROR", f"HTTP {response.status_code}: {raw[:300]}")
            return _read(response.iter_lines())
    except httpx.TransportError as error:  # 시간 초과도 여기에 든다
        raise CodexError("V3_CODEX_UNAVAILABLE", type(error).__name__) from error


def _read(lines: Iterable[str]) -> Reply:
    """SSE 이벤트에서 출력 텍스트를 이어 붙이고, response.model의 마지막 값을 기억한다."""
    text, model, buffer = "", "", []
    for line in [*lines, ""]:  # 끝에 빈 줄이 없어도 마지막 이벤트를 읽는다
        if line:
            buffer.append(line)
            continue
        data = "\n".join(part[5:].strip() for part in buffer if part.startswith("data:")).strip()
        buffer = []
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        if isinstance(response.get("model"), str):
            model = response["model"]
        if event.get("type") == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text += event["delta"]
        elif event.get("type") in ("error", "response.failed"):
            error = event.get("error") if isinstance(event.get("error"), dict) else response.get("error")
            error = error if isinstance(error, dict) else {}
            detail = ": ".join(str(value) for value in (error.get("code") or error.get("type"), error.get("message")) if value)
            raise CodexError("V3_CODEX_FAILED", detail or "응답 실패 이벤트")
    return Reply(text, model)

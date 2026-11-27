"""OpenAI-compatible chat client for the free local router.

The only endpoint is LLM_BASE_URL (default http://127.0.0.1:3010/v1).
A 429 or 5xx is retried with backoff. Any other failure is final: this
client never calls a second host and never falls back to a paid API.
"""

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger("sellbird.llm")

DEFAULT_BASE_URL = "http://127.0.0.1:3010/v1"
DEFAULT_AGENT = "Sellbird"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 1.0


class LLMError(Exception):
    """The router did not answer. ``retryable`` is only for the client's own loop."""

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class LLMResult:
    text: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class _NoLiveQuota:
    """No subscription gauge. Budget falls back to this process's call count."""

    async def get_utilization(self):
        return None


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).strip()


class OpenAICompatClient:
    def __init__(self, model: str = "", base_url: str = "", api_key: str = "",
                 agent: str = "", transport=None, sleep=None):
        self.base_url = (base_url or _env("LLM_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key if api_key is not None and api_key != "" else _env("LLM_API_KEY")
        # An explicit empty model means "read LLM_MODEL", not "call with no model".
        chosen = model if model else _env("LLM_MODEL")
        self.model = (chosen or "").strip()
        self.agent = (agent or _env("LLM_X_AGENT", DEFAULT_AGENT) or DEFAULT_AGENT).strip() or DEFAULT_AGENT
        self.transport = transport
        self.sleep = sleep or asyncio.sleep

    def configured_reason(self) -> str:
        if not self.model:
            return "LLM_MODEL is empty"
        if not self.base_url.startswith(("http://", "https://")):
            return "LLM_BASE_URL is not an http(s) address"
        return ""

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Agent": self.agent,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    async def complete(self, prompt: str, *, timeout: float = 120.0) -> LLMResult:
        reason = self.configured_reason()
        if reason:
            logger.error("LLM call not sent: %s. No fallback.", reason)
            raise LLMError(reason, retryable=False)
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        last = "the router did not answer"
        status = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                delay = BACKOFF_SECONDS * (2 ** (attempt - 2))
                logger.info(
                    "Retrying the free model router in %.0fs (attempt %s/%s) after HTTP %s.",
                    delay, attempt, MAX_ATTEMPTS, status or "error",
                )
                await self.sleep(delay)
            try:
                response = await self._post(payload, timeout)
            except httpx.HTTPError as e:
                last = f"router request failed ({type(e).__name__})"
                status = 0
                logger.warning("%s. No fallback.", last)
                continue
            status = response.status_code
            if status in RETRY_STATUSES:
                last = f"router returned HTTP {status}"
                logger.warning("%s. No fallback.", last)
                continue
            if status >= 400:
                detail = _error_detail(response)
                message = f"router returned HTTP {status}" + (f": {detail}" if detail else "")
                logger.error("%s. No fallback.", message)
                raise LLMError(message, retryable=False, status=status)
            try:
                return _parse_success(response, self.model)
            except LLMError as e:
                logger.error("%s. No fallback.", e)
                raise
        message = f"{last} after {MAX_ATTEMPTS} attempts"
        logger.error("%s. No fallback.", message)
        raise LLMError(message, retryable=False, status=status)

    async def _post(self, payload: dict, timeout: float):
        headers = self._headers()
        url = self._url()
        if self.transport is not None:
            return await self.transport(url, json=payload, headers=headers, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, json=payload, headers=headers)


def _error_detail(response) -> str:
    try:
        data = response.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            text = str(err.get("message") or "")
        else:
            text = str(err or data.get("message") or "")
    else:
        text = ""
    text = " ".join(text.split())
    return text[:180]


def _parse_success(response, fallback_model: str) -> LLMResult:
    try:
        data = response.json()
    except Exception as e:
        raise LLMError(f"router returned a body that is not JSON ({type(e).__name__})") from e
    if not isinstance(data, dict):
        raise LLMError("router returned a JSON value that is not an object")
    choices = data.get("choices") or []
    if not choices or not isinstance(choices, list) or not isinstance(choices[0], dict):
        raise LLMError("router returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        content = "".join(parts)
    text = str(content or "").strip()
    if not text:
        raise LLMError("router returned an empty completion")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return LLMResult(
        text=text,
        model=str(data.get("model") or fallback_model),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )

"""OpenAI Chat Completions provider over plain HTTPX.

Plain HTTPX keeps the dependency list short, makes the transport easy to mock in tests,
and works with any OpenAI-compatible endpoint through ``RELAY_OPENAI_BASE_URL``.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.core.exceptions import LLMProviderError
from app.core.logging import get_logger, redact
from app.llm.base import LLMProvider, LLMRequest, LLMResponse, LLMUsage
from app.llm.prompting import build_messages

logger = get_logger("llm.openai")

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIProvider requires an API key")
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": build_messages(request),
            "response_format": {"type": "json_object"},
        }
        attempt = 0
        while True:
            attempt += 1
            started = time.perf_counter()
            try:
                return await self._send(body, started)
            except LLMProviderError as exc:
                if not exc.retryable or attempt > self.max_retries:
                    logger.warning(
                        "llm.call_failed",
                        extra={
                            "fields": {
                                "agent": request.agent,
                                "operation": request.operation,
                                "attempt": attempt,
                                "error": exc.message,
                            }
                        },
                    )
                    raise
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                logger.info(
                    "llm.retry",
                    extra={"fields": {"attempt": attempt, "delay_s": delay, "error": exc.message}},
                )
                await asyncio.sleep(delay)

    async def _send(self, body: dict[str, object], started: float) -> LLMResponse:
        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise LLMProviderError("LLM request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                f"LLM transport error: {type(exc).__name__}", retryable=True
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            raise LLMProviderError(
                f"LLM provider returned HTTP {response.status_code}{_error_type(response)}",
                retryable=response.status_code in _RETRYABLE_STATUS,
            )
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("LLM provider returned an unexpected response shape") from exc

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(data.get("model") or self.model),
            latency_ms=latency_ms,
            usage=LLMUsage(
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _error_type(response: httpx.Response) -> str:
    """Return a short, redacted error type. Never echo the full provider body."""
    try:
        error = response.json().get("error", {})
        kind = error.get("type") or error.get("code")
    except (ValueError, AttributeError):
        return ""
    return f" ({redact(str(kind))[:64]})" if kind else ""

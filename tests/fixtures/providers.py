"""Test doubles for LLM providers."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.core.exceptions import LLMProviderError
from app.llm.base import LLMProvider, LLMRequest, LLMResponse, LLMUsage
from app.llm.mock_provider import MockProvider

Answer = dict[str, Any] | str | LLMProviderError | Callable[[dict[str, Any]], Any]


class ScriptedProvider(LLMProvider):
    """Returns queued answers per operation. Falls back to the default mock responders.

    Use it to simulate a misbehaving model: invalid JSON, invented tools, prompt-injected
    plans, or provider outages.
    """

    name = "scripted"
    model = "scripted-1"

    def __init__(self, script: dict[str, list[Answer]] | None = None) -> None:
        self.script = {op: list(answers) for op, answers in (script or {}).items()}
        self.fallback = MockProvider()
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        queue = self.script.get(request.operation)
        if not queue:
            response = await self.fallback.complete(request)
            return response.model_copy(update={"provider": self.name, "model": self.model})
        answer = queue.pop(0)
        if callable(answer):
            answer = answer(request.payload)
        if isinstance(answer, LLMProviderError):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            latency_ms=1.0,
            usage=LLMUsage(prompt_tokens=100, completion_tokens=20),
        )

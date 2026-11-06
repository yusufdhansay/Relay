"""Deterministic mock provider.

Each operation (``triage``, ``plan_investigation``, ...) maps to a pure function that turns
the request payload into a JSON answer. The answer goes through the same parsing and
validation as a live model's answer, so the rest of the system cannot tell the difference.
The rule-based responders live in :mod:`app.llm.mock_responders`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from app.core.exceptions import LLMProviderError
from app.llm.base import LLMProvider, LLMRequest, LLMResponse

Responder = Callable[[dict[str, Any]], dict[str, Any]]


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(
        self, responders: Mapping[str, Responder] | None = None, model: str = "relay-mock-1"
    ) -> None:
        if responders is None:
            from app.llm.mock_responders import DEFAULT_RESPONDERS

            responders = DEFAULT_RESPONDERS
        self.responders = dict(responders)
        self.model = model
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        responder = self.responders.get(request.operation)
        if responder is None:
            raise LLMProviderError(f"Mock provider has no responder for '{request.operation}'")
        started = time.perf_counter()
        answer = responder(request.payload)
        return LLMResponse(
            text=json.dumps(answer),
            provider=self.name,
            model=self.model,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

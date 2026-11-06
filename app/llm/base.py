"""Provider-neutral LLM interface.

Agents never talk to a vendor SDK directly. They build an :class:`LLMRequest` with a
system prompt, a JSON payload, and a JSON schema for the expected output. A provider
returns raw text. Parsing and validation happen in :mod:`app.llm.structured`, so every
provider gets the same checks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class LLMRequest(BaseModel):
    agent: str
    operation: str
    system_prompt: str
    payload: dict[str, Any]
    output_schema: dict[str, Any]
    repair_feedback: list[str] = Field(default_factory=list)
    previous_output: str | None = None


class LLMUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMResponse(BaseModel):
    text: str
    provider: str
    model: str
    latency_ms: float
    usage: LLMUsage = Field(default_factory=LLMUsage)


class LLMProvider(ABC):
    """Implement ``complete`` to add a new provider."""

    name: str
    model: str

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return the model's raw text answer. Raise ``LLMProviderError`` on failure."""

    async def aclose(self) -> None:  # noqa: B027  (optional hook)
        """Release network resources, if any."""

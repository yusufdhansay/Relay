"""Structured generation: parse, validate, and repair LLM output.

The flow for every agent call:

1. Send the request to the provider.
2. Parse the text as JSON and validate it against the Pydantic output model.
3. Run optional semantic checks (for example "every tool must exist").
4. If anything fails, send the problems back once (bounded) and try again.
5. Return the validated model plus telemetry for every call made.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.exceptions import AgentOutputError, LLMProviderError
from app.core.logging import get_logger
from app.llm.base import LLMProvider, LLMRequest, LLMUsage

logger = get_logger("llm.structured")

SemanticValidator = Callable[[Any], list[str]]


@dataclass
class LLMCallRecord:
    provider: str
    model: str
    latency_ms: float
    usage: LLMUsage
    success: bool
    error: str | None = None


@dataclass
class StructuredResult[T: BaseModel]:
    output: T
    calls: list[LLMCallRecord] = field(default_factory=list)


class StructuredOutputError(AgentOutputError):
    def __init__(self, message: str, calls: list[LLMCallRecord]) -> None:
        super().__init__(message)
        self.calls = calls


class ProviderCallError(LLMProviderError):
    def __init__(self, cause: LLMProviderError, calls: list[LLMCallRecord]) -> None:
        super().__init__(cause.message, retryable=cause.retryable)
        self.calls = calls


async def generate_structured[T: BaseModel](
    provider: LLMProvider,
    *,
    agent: str,
    operation: str,
    system_prompt: str,
    payload: dict[str, Any],
    output_model: type[T],
    repair_attempts: int = 1,
    semantic_validator: SemanticValidator | None = None,
) -> StructuredResult[T]:
    calls: list[LLMCallRecord] = []
    request = LLMRequest(
        agent=agent,
        operation=operation,
        system_prompt=system_prompt,
        payload=payload,
        output_schema=output_model.model_json_schema(),
    )
    problems: list[str] = []
    for attempt in range(repair_attempts + 1):
        try:
            response = await provider.complete(request)
        except LLMProviderError as exc:
            calls.append(
                LLMCallRecord(
                    provider=provider.name,
                    model=provider.model,
                    latency_ms=0.0,
                    usage=LLMUsage(),
                    success=False,
                    error=exc.message,
                )
            )
            raise ProviderCallError(exc, calls) from exc

        problems = _validate(response.text, output_model, semantic_validator)
        calls.append(
            LLMCallRecord(
                provider=response.provider,
                model=response.model,
                latency_ms=response.latency_ms,
                usage=response.usage,
                success=not problems,
                error="; ".join(problems)[:500] if problems else None,
            )
        )
        if not problems:
            output = output_model.model_validate_json(response.text)
            return StructuredResult(output=output, calls=calls)

        logger.info(
            "llm.output_rejected",
            extra={
                "fields": {
                    "agent": agent,
                    "operation": operation,
                    "attempt": attempt + 1,
                    "problems": problems[:5],
                }
            },
        )
        request = request.model_copy(
            update={"repair_feedback": problems, "previous_output": response.text[:4000]}
        )

    raise StructuredOutputError(
        f"{agent} produced invalid output after {repair_attempts + 1} attempt(s): "
        + "; ".join(problems[:3]),
        calls,
    )


def _validate(
    text: str, output_model: type[BaseModel], semantic_validator: SemanticValidator | None
) -> list[str]:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"Output is not valid JSON: {exc.msg}"]
    try:
        parsed = output_model.model_validate_json(text)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()[:10]
        ]
    return semantic_validator(parsed) if semantic_validator else []

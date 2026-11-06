"""Tool registry: the allowlist of everything Relay is able to do."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import (
    ExecutionNotAuthorizedError,
    ToolArgumentError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolOutputError,
    ToolTimeoutError,
)
from app.core.workflow import fingerprint
from app.tools.base import ExecutionPermit, Tool, ToolContext, ToolInput, ToolSpec


@dataclass(frozen=True)
class ToolInvocation:
    tool_name: str
    arguments: dict[str, Any]
    output: dict[str, Any]
    duration_ms: float


class ToolRegistry:
    def __init__(self, default_timeout_seconds: float = 10.0) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}
        self.default_timeout_seconds = default_timeout_seconds

    def register(self, tool: Tool[Any, Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name} is already registered")
        self._tools[tool.name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool[Any, Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"Tool '{name}' is not registered") from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec() for name in self.names()]

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> ToolInput:
        tool = self.get(name)
        try:
            return tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors()[:10]
            )
            raise ToolArgumentError(f"Invalid arguments for {name}: {problems}") from None

    def normalise_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate and return arguments with defaults filled in (JSON-safe)."""
        return self.validate_arguments(name, arguments).model_dump(mode="json")

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: ToolContext,
        permit: ExecutionPermit,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolInvocation:
        tool = self.get(name)
        validated = self.validate_arguments(name, arguments)
        normalised = validated.model_dump(mode="json")
        if permit.tool_name != name or permit.arguments_hash != fingerprint(name, normalised):
            raise ExecutionNotAuthorizedError(
                f"Execution permit does not match the call to {name}; refusing to run"
            )

        timeout = timeout_seconds or tool.timeout_seconds or self.default_timeout_seconds
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout):
                result = await tool.run(validated, ctx)
        except TimeoutError:
            raise ToolTimeoutError(f"{name} did not finish within {timeout:g}s") from None
        except ToolExecutionError:
            raise
        except Exception as exc:  # handler bug: never leak internals
            raise ToolExecutionError(
                f"{name} failed with an unexpected {type(exc).__name__}"
            ) from exc
        duration_ms = (time.perf_counter() - started) * 1000

        try:
            output = tool.output_model.model_validate(result).model_dump(mode="json")
        except ValidationError as exc:
            raise ToolOutputError(f"{name} returned output that failed validation") from exc
        return ToolInvocation(
            tool_name=name, arguments=normalised, output=output, duration_ms=duration_ms
        )

"""Agent base class and telemetry helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel

from app.core.exceptions import RelayError
from app.core.workflow import AgentRun
from app.llm.base import LLMProvider
from app.llm.structured import (
    LLMCallRecord,
    ProviderCallError,
    SemanticValidator,
    StructuredOutputError,
    generate_structured,
)


class AgentInvocationError(RelayError):
    """An agent could not produce a valid result. Carries telemetry for the audit trail."""

    code = "agent_failed"

    def __init__(self, agent: str, message: str, runs: list[AgentRun]) -> None:
        super().__init__(f"{agent}: {message}")
        self.agent = agent
        self.runs = runs


@dataclass
class AgentResult[T]:
    output: T
    runs: list[AgentRun] = field(default_factory=list)


class AgentInfo(BaseModel):
    name: str
    description: str
    responsibilities: list[str]
    uses_llm: bool
    output_schema: str | None


class Agent:
    name: ClassVar[str]
    description: ClassVar[str]
    responsibilities: ClassVar[tuple[str, ...]]
    system_prompt: ClassVar[str] = ""
    output_schema_name: ClassVar[str | None] = None

    def __init__(self, llm: LLMProvider | None = None, *, repair_attempts: int = 1) -> None:
        self.llm = llm
        self.repair_attempts = repair_attempts

    @classmethod
    def info(cls) -> AgentInfo:
        return AgentInfo(
            name=cls.name,
            description=cls.description,
            responsibilities=list(cls.responsibilities),
            uses_llm=bool(cls.system_prompt),
            output_schema=cls.output_schema_name,
        )

    async def _generate[T: BaseModel](
        self,
        *,
        workflow_id: str,
        operation: str,
        payload: dict[str, Any],
        output_model: type[T],
        semantic_validator: SemanticValidator | None = None,
    ) -> tuple[T, AgentRun]:
        if self.llm is None:
            raise RuntimeError(f"{self.name} has no LLM provider configured")
        try:
            result = await generate_structured(
                self.llm,
                agent=self.name,
                operation=operation,
                system_prompt=self.system_prompt,
                payload=payload,
                output_model=output_model,
                repair_attempts=self.repair_attempts,
                semantic_validator=semantic_validator,
            )
        except (StructuredOutputError, ProviderCallError) as exc:
            run = self._run(workflow_id, operation, exc.calls, success=False, error=exc.message)
            raise AgentInvocationError(self.name, exc.message, [run]) from exc
        run = self._run(
            workflow_id,
            operation,
            result.calls,
            success=True,
            output=result.output.model_dump(mode="json"),
        )
        return result.output, run

    def _run(
        self,
        workflow_id: str,
        operation: str,
        calls: list[LLMCallRecord],
        *,
        success: bool,
        error: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> AgentRun:
        prompt = [c.usage.prompt_tokens for c in calls if c.usage.prompt_tokens is not None]
        completion = [
            c.usage.completion_tokens for c in calls if c.usage.completion_tokens is not None
        ]
        return AgentRun(
            workflow_id=workflow_id,
            agent=self.name,
            operation=operation,
            provider=calls[-1].provider if calls else (self.llm.name if self.llm else "none"),
            model=calls[-1].model if calls else (self.llm.model if self.llm else "none"),
            success=success,
            llm_calls=len(calls),
            latency_ms=round(sum(c.latency_ms for c in calls), 3),
            prompt_tokens=sum(prompt) if prompt else None,
            completion_tokens=sum(completion) if completion else None,
            error=error[:1000] if error else None,
            output=output,
        )

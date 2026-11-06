"""Tool contract.

A tool declares typed input and output models, a risk level, and whether it changes
infrastructure. The registry validates both sides of every call, so handlers only ever see
valid input and the orchestrator only ever sees valid output.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from app.core.enums import RiskLevel


class ToolInput(BaseModel):
    """Base class for tool inputs. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolContext:
    workflow_id: str
    task_key: str
    idempotency_key: str
    attempt: int = 1


@dataclass(frozen=True)
class ExecutionPermit:
    """Proof that a specific tool call passed policy (and approval, if required).

    The registry refuses to run a tool unless the permit names the same tool and the same
    argument fingerprint. This stops a caller from changing arguments after approval.
    Permits are issued by :class:`app.core.guard.ExecutionGuard`.
    """

    tool_name: str
    arguments_hash: str
    basis: Literal["policy_allow", "human_approval", "read_only_probe"]
    approval_id: str | None = None


class ToolSpec(BaseModel):
    """Public description of a tool, shown to the planner and through the API."""

    name: str
    description: str
    risk_level: RiskLevel
    mutating: bool
    idempotent: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class Tool[InT: ToolInput, OutT: ToolOutput](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[ToolInput]]
    output_model: ClassVar[type[ToolOutput]]
    risk_level: ClassVar[RiskLevel]
    mutating: ClassVar[bool]
    # True when repeating the call with the same idempotency key cannot cause a second
    # side effect. Read-only tools are idempotent by nature.
    idempotent: ClassVar[bool]
    timeout_seconds: ClassVar[float | None] = None

    @abstractmethod
    async def run(self, args: InT, ctx: ToolContext) -> OutT:
        """Execute the tool. Raise ``ToolExecutionError`` on failure."""

    @classmethod
    def spec(cls) -> ToolSpec:
        return ToolSpec(
            name=cls.name,
            description=cls.description,
            risk_level=cls.risk_level,
            mutating=cls.mutating,
            idempotent=cls.idempotent,
            input_schema=cls.input_model.model_json_schema(),
            output_schema=cls.output_model.model_json_schema(),
        )

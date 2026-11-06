"""Typed contracts for agent inputs and outputs.

LLM-facing output models forbid unknown fields. A model therefore cannot add fields such as
``status`` or ``approved`` and have them flow into workflow state.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import (
    Outcome,
    PolicyVerdict,
    RequestCategory,
    RiskLevel,
    Symptom,
    Urgency,
    VerificationRecommendation,
)

ServiceName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,48}$")]
TaskId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,20}$")]
ShortText = Annotated[str, Field(max_length=500)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------- triage
class Constraints(StrictModel):
    read_only: bool = Field(
        description="True when the requester forbids any change to services or data."
    )
    notes: list[ShortText] = Field(default_factory=list, max_length=5)


class TriageResult(StrictModel):
    category: RequestCategory
    urgency: Urgency
    summary: str = Field(min_length=3, max_length=300)
    services: list[ServiceName] = Field(default_factory=list, max_length=5)
    symptoms: list[Symptom] = Field(default_factory=list, max_length=4)
    requested_outcomes: list[Outcome] = Field(default_factory=list, max_length=7)
    constraints: Constraints
    missing_information: list[ShortText] = Field(default_factory=list, max_length=5)
    assumptions: list[ShortText] = Field(default_factory=list, max_length=5)
    can_proceed: bool


# ---------------------------------------------------------------------- planning
class PlannedTask(StrictModel):
    id: TaskId
    description: str = Field(min_length=3, max_length=300)
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[TaskId] = Field(default_factory=list, max_length=10)
    input_from: list[TaskId] = Field(
        default_factory=list,
        max_length=10,
        description="Earlier tasks whose outputs are needed to fill missing arguments.",
    )


class ExecutionPlan(StrictModel):
    objective: str = Field(min_length=3, max_length=300)
    rationale: str = Field(min_length=3, max_length=1000)
    tasks: list[PlannedTask] = Field(max_length=20)


class EvidenceRef(StrictModel):
    task_id: str = Field(max_length=20)
    detail: str = Field(min_length=3, max_length=400)


class Diagnosis(StrictModel):
    affected_service: ServiceName | None
    root_cause: str = Field(min_length=3, max_length=500)
    confidence: Literal["low", "medium", "high"]
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=10)
    fault_found: bool


class RemediationPlan(StrictModel):
    diagnosis: Diagnosis
    rationale: str = Field(min_length=3, max_length=1000)
    tasks: list[PlannedTask] = Field(max_length=10)


# ---------------------------------------------------------------------- policy
class TaskAssessment(StrictModel):
    task_id: str = Field(max_length=32)
    escalate: bool = Field(description="True to require human approval even if rules allow it.")
    concerns: list[ShortText] = Field(default_factory=list, max_length=5)


class PolicyReview(StrictModel):
    assessments: list[TaskAssessment] = Field(max_length=30)


class PolicyDecision(BaseModel):
    """Final decision for one task. Produced by code, never parsed from LLM output."""

    task_key: str
    tool: str
    verdict: PolicyVerdict
    risk_level: RiskLevel
    reasons: list[str]
    rules: list[str]
    llm_concerns: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------- execution
class ArgumentResolution(StrictModel):
    arguments: dict[str, Any]
    rationale: str = Field(min_length=3, max_length=500)


class AttemptRecord(BaseModel):
    attempt: int
    error_code: str
    error: str
    retryable: bool
    delay_seconds: float | None = None


class ExecutionOutcome(BaseModel):
    success: bool
    arguments: dict[str, Any]
    output: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None
    retryable: bool = False
    attempts: list[AttemptRecord] = Field(default_factory=list)
    duration_ms: float = 0.0
    permit_basis: str | None = None


# ---------------------------------------------------------------------- verification
class VerificationCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class VerifierAssessment(StrictModel):
    objective_achieved: bool
    confidence: Literal["low", "medium", "high"]
    rationale: str = Field(min_length=3, max_length=1000)
    unmet_requirements: list[ShortText] = Field(default_factory=list, max_length=10)


class VerificationResult(BaseModel):
    objective_achieved: bool
    recommendation: VerificationRecommendation
    checks: list[VerificationCheck]
    probes: list[dict[str, Any]]
    assessment: VerifierAssessment | None
    retry_task_keys: list[str] = Field(default_factory=list)
    summary: str

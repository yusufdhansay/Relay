"""HTTP request and response models. Internal fields (idempotency keys) are not exposed."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import (
    ApprovalDecision,
    ApprovalStatus,
    EventType,
    PlanStage,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from app.core.workflow import Approval, Task, Workflow, WorkflowEvent

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=5, max_length=2000, examples=["Check the auth service health"])

    @field_validator("request")
    @classmethod
    def _clean(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 5:
            raise ValueError("request must contain at least 5 non-space characters")
        if _CONTROL_CHARS.search(value):
            raise ValueError("request contains control characters")
        return value


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: ApprovalDecision
    comment: str | None = Field(default=None, max_length=1000)
    resume: bool = Field(default=True, description="Continue the workflow after deciding.")


class TaskOut(BaseModel):
    key: str
    stage: PlanStage
    sequence: int
    description: str
    tool: str
    arguments: dict[str, Any]
    resolved_arguments: dict[str, Any] | None
    depends_on: list[str]
    input_from: list[str]
    status: TaskStatus
    risk_level: RiskLevel
    requires_approval: bool
    policy: dict[str, Any] | None
    attempts: int
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, task: Task) -> TaskOut:
        return cls(
            key=task.key,
            stage=task.stage,
            sequence=task.sequence,
            description=task.description,
            tool=task.tool_name,
            arguments=task.arguments,
            resolved_arguments=task.resolved_arguments,
            depends_on=task.depends_on,
            input_from=task.input_from,
            status=task.status,
            risk_level=task.risk_level,
            requires_approval=task.requires_approval,
            policy=task.policy,
            attempts=task.attempts,
            output=task.output,
            error=task.error.model_dump() if task.error else None,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )


class ApprovalOut(BaseModel):
    id: str
    workflow_id: str
    task_key: str
    tool: str
    arguments: dict[str, Any]
    arguments_hash: str
    risk_level: RiskLevel
    reasons: list[str]
    status: ApprovalStatus
    requested_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    comment: str | None

    @classmethod
    def of(cls, approval: Approval) -> ApprovalOut:
        return cls(
            id=approval.id,
            workflow_id=approval.workflow_id,
            task_key=approval.task_key,
            tool=approval.tool_name,
            arguments=approval.arguments,
            arguments_hash=approval.arguments_hash,
            risk_level=approval.risk_level,
            reasons=approval.reasons,
            status=approval.status,
            requested_at=approval.requested_at,
            decided_by=approval.decided_by,
            decided_at=approval.decided_at,
            comment=approval.comment,
        )


class WorkflowListItem(BaseModel):
    id: str
    request: str
    status: WorkflowStatus
    status_reason: str | None
    category: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def of(cls, workflow: Workflow) -> WorkflowListItem:
        return cls(
            id=workflow.id,
            request=workflow.request,
            status=workflow.status,
            status_reason=workflow.status_reason,
            category=(workflow.triage or {}).get("category"),
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
            completed_at=workflow.completed_at,
        )


class WorkflowOut(WorkflowListItem):
    stage: PlanStage
    triage: dict[str, Any] | None
    diagnosis: dict[str, Any] | None
    verification: dict[str, Any] | None
    summary: dict[str, Any] | None
    retry_count: int
    pending_approval_ids: list[str]
    tasks: list[TaskOut]
    approvals: list[ApprovalOut]

    @classmethod
    def of(cls, workflow: Workflow) -> WorkflowOut:
        return cls(
            **WorkflowListItem.of(workflow).model_dump(),
            stage=workflow.stage,
            triage=workflow.triage,
            diagnosis=workflow.diagnosis,
            verification=workflow.verification,
            summary=workflow.summary,
            retry_count=workflow.retry_count,
            pending_approval_ids=[a.id for a in workflow.pending_approvals()],
            tasks=[TaskOut.of(t) for t in workflow.tasks],
            approvals=[ApprovalOut.of(a) for a in workflow.approvals],
        )


class WorkflowPage(BaseModel):
    items: list[WorkflowListItem]
    total: int
    limit: int
    offset: int


class EventOut(BaseModel):
    id: str
    sequence: int
    event_type: EventType
    component: str
    timestamp: datetime
    details: dict[str, Any]

    @classmethod
    def of(cls, event: WorkflowEvent) -> EventOut:
        return cls(**event.model_dump(exclude={"workflow_id"}))


class EventList(BaseModel):
    workflow_id: str
    events: list[EventOut]


class AgentRunOut(BaseModel):
    agent: str
    operation: str
    provider: str
    model: str
    success: bool
    llm_calls: int
    latency_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    error: str | None
    created_at: datetime


class DecisionOut(BaseModel):
    approval: ApprovalOut
    workflow: WorkflowOut


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody

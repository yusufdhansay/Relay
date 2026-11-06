"""Workflow aggregate: workflow, tasks, approvals, and pending audit events.

All state changes go through methods on :class:`Workflow`. Each method validates the
transition with the state machine and records an audit event, so the event history can
always reconstruct what happened.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import (
    ApprovalDecision,
    ApprovalStatus,
    EventType,
    PlanStage,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from app.core.exceptions import (
    ApprovalAlreadyDecidedError,
    ApprovalNotAuthorizedError,
    ApprovalNotFoundError,
    InvalidStateTransitionError,
    TaskNotFoundError,
)
from app.core.state_machine import assert_task_transition, assert_workflow_transition

# Identities that can never record an approval decision. Agents and the orchestrator
# run under these names, so an LLM cannot approve its own actions.
NON_HUMAN_IDENTITIES = frozenset(
    {
        "system",
        "relay",
        "orchestrator",
        "triage_agent",
        "planning_agent",
        "policy_agent",
        "execution_agent",
        "verification_agent",
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """Stable SHA-256 of a tool call. Approvals are bound to this value."""
    canonical = json.dumps(
        {"tool": tool_name, "arguments": arguments}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class NewEvent(BaseModel):
    """An audit event that has not been persisted yet."""

    event_type: EventType
    component: str
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class TaskError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class Task(BaseModel):
    id: str = Field(default_factory=new_id)
    key: str
    stage: PlanStage
    sequence: int
    description: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    input_from: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    risk_level: RiskLevel = RiskLevel.LOW
    policy: dict[str, Any] | None = None
    requires_approval: bool = False
    attempts: int = 0
    idempotency_key: str = Field(default_factory=new_id)
    resolved_arguments: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: TaskError | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Approval(BaseModel):
    id: str = Field(default_factory=new_id)
    workflow_id: str
    task_id: str
    task_key: str
    tool_name: str
    arguments: dict[str, Any]
    arguments_hash: str
    risk_level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=utc_now)
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class Workflow(BaseModel):
    id: str = Field(default_factory=new_id)
    request: str
    status: WorkflowStatus = WorkflowStatus.RECEIVED
    status_reason: str | None = None
    stage: PlanStage = PlanStage.INVESTIGATION
    remediation_planned: bool = False
    triage: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    retry_count: int = 0
    idempotency_key: str | None = None
    request_hash: str | None = None
    version: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    tasks: list[Task] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    pending_events: list[NewEvent] = Field(default_factory=list, exclude=True)

    # ------------------------------------------------------------------ creation
    @classmethod
    def create(
        cls,
        request: str,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> Workflow:
        workflow = cls(request=request, idempotency_key=idempotency_key, request_hash=request_hash)
        workflow.record(
            EventType.WORKFLOW_CREATED,
            "api",
            request=request,
            idempotency_key=idempotency_key,
        )
        return workflow

    # ------------------------------------------------------------------ events
    def record(self, event_type: EventType, component: str, **details: Any) -> None:
        self.pending_events.append(
            NewEvent(event_type=event_type, component=component, details=details)
        )

    def drain_events(self) -> list[NewEvent]:
        events, self.pending_events = self.pending_events, []
        return events

    # ------------------------------------------------------------------ status
    def transition_to(
        self,
        target: WorkflowStatus,
        *,
        reason: str | None = None,
        component: str = "orchestrator",
    ) -> None:
        previous = self.status
        assert_workflow_transition(previous, target)
        self.status = target
        self.status_reason = reason
        self.updated_at = utc_now()
        self.record(
            EventType.STATUS_CHANGED,
            component,
            from_status=previous.value,
            to_status=target.value,
            reason=reason,
        )
        if target.is_terminal:
            self.completed_at = self.updated_at
            terminal_event = {
                WorkflowStatus.COMPLETED: EventType.WORKFLOW_COMPLETED,
                WorkflowStatus.FAILED: EventType.WORKFLOW_FAILED,
                WorkflowStatus.CANCELLED: EventType.WORKFLOW_CANCELLED,
            }[target]
            self.record(terminal_event, component, reason=reason)

    def fail(self, reason: str, *, component: str = "orchestrator") -> None:
        self.transition_to(WorkflowStatus.FAILED, reason=reason, component=component)

    def cancel(self, reason: str, *, component: str) -> None:
        if self.status.is_terminal:
            raise InvalidStateTransitionError(
                f"Workflow is already {self.status.value} and cannot be cancelled"
            )
        for approval in self.approvals:
            if approval.status == ApprovalStatus.PENDING:
                approval.status = ApprovalStatus.CANCELLED
                approval.decided_at = utc_now()
        for task in self.tasks:
            if task.status in {
                TaskStatus.PENDING,
                TaskStatus.AWAITING_APPROVAL,
                TaskStatus.SKIPPED,
            }:
                self._set_task_status(task, TaskStatus.CANCELLED)
        self.transition_to(WorkflowStatus.CANCELLED, reason=reason, component=component)

    # ------------------------------------------------------------------ tasks
    def add_tasks(self, tasks: list[Task]) -> None:
        existing = {task.key for task in self.tasks}
        for task in tasks:
            if task.key in existing:
                raise ValueError(f"Duplicate task key {task.key}")
            existing.add(task.key)
            self.tasks.append(task)

    def get_task(self, key: str) -> Task:
        for task in self.tasks:
            if task.key == key:
                return task
        raise TaskNotFoundError(f"Task {key} not found in workflow {self.id}")

    def stage_tasks(self, stage: PlanStage) -> list[Task]:
        return sorted((t for t in self.tasks if t.stage == stage), key=lambda t: t.sequence)

    def next_task(self, stage: PlanStage) -> Task | None:
        """Return the first task in the stage whose dependencies all succeeded."""
        for task in self.stage_tasks(stage):
            if task.status not in {TaskStatus.PENDING, TaskStatus.AWAITING_APPROVAL}:
                continue
            deps = [self.get_task(key) for key in task.depends_on]
            if all(dep.status == TaskStatus.SUCCEEDED for dep in deps):
                return task
        return None

    def _set_task_status(self, task: Task, target: TaskStatus) -> None:
        assert_task_transition(task.status, target)
        task.status = target
        self.updated_at = utc_now()

    def start_task(self, task: Task, *, component: str = "execution_agent") -> None:
        self._set_task_status(task, TaskStatus.RUNNING)
        task.started_at = utc_now()
        task.attempts += 1

    def complete_task(self, task: Task, output: dict[str, Any]) -> None:
        self._set_task_status(task, TaskStatus.SUCCEEDED)
        task.output = output
        task.error = None
        task.finished_at = utc_now()

    def fail_task(self, task: Task, error: TaskError) -> None:
        self._set_task_status(task, TaskStatus.FAILED)
        task.error = error
        task.finished_at = utc_now()
        self.skip_dependents(task.key, reason=f"dependency {task.key} failed")

    def block_task(self, task: Task, reasons: list[str], *, component: str) -> None:
        self._set_task_status(task, TaskStatus.BLOCKED)
        task.error = TaskError(code="blocked_by_policy", message="; ".join(reasons))
        task.finished_at = utc_now()
        self.record(
            EventType.ACTION_BLOCKED,
            component,
            task_key=task.key,
            tool=task.tool_name,
            reasons=reasons,
        )
        self.skip_dependents(task.key, reason=f"dependency {task.key} was blocked")

    def skip_dependents(self, key: str, *, reason: str) -> None:
        for task in self.tasks:
            if key in task.depends_on and task.status == TaskStatus.PENDING:
                self._set_task_status(task, TaskStatus.SKIPPED)
                task.error = TaskError(code="dependency_not_met", message=reason)
                self.record(
                    EventType.TASK_SKIPPED, "orchestrator", task_key=task.key, reason=reason
                )
                self.skip_dependents(task.key, reason=f"dependency {task.key} was skipped")

    def reset_task_for_retry(self, task: Task, *, reason: str) -> None:
        self._set_task_status(task, TaskStatus.PENDING)
        task.error = None
        task.finished_at = None
        self.record(EventType.RETRY_ATTEMPTED, "orchestrator", task_key=task.key, reason=reason)

    def stage_settled(self, stage: PlanStage) -> bool:
        return all(task.status.is_settled for task in self.stage_tasks(stage))

    # ------------------------------------------------------------------ approvals
    def request_approval(self, task: Task, *, reasons: list[str]) -> Approval:
        if any(a.task_id == task.id and a.status == ApprovalStatus.PENDING for a in self.approvals):
            raise InvalidStateTransitionError(f"Task {task.key} already has a pending approval")
        self._set_task_status(task, TaskStatus.AWAITING_APPROVAL)
        approval = Approval(
            workflow_id=self.id,
            task_id=task.id,
            task_key=task.key,
            tool_name=task.tool_name,
            arguments=task.arguments,
            arguments_hash=fingerprint(task.tool_name, task.arguments),
            risk_level=task.risk_level,
            reasons=reasons,
        )
        self.approvals.append(approval)
        self.record(
            EventType.APPROVAL_REQUESTED,
            "orchestrator",
            approval_id=approval.id,
            task_key=task.key,
            tool=task.tool_name,
            arguments=task.arguments,
            risk_level=task.risk_level.value,
            reasons=reasons,
        )
        return approval

    def get_approval(self, approval_id: str) -> Approval:
        for approval in self.approvals:
            if approval.id == approval_id:
                return approval
        raise ApprovalNotFoundError(f"Approval {approval_id} not found")

    def approval_for_task(self, task: Task) -> Approval | None:
        matches = [a for a in self.approvals if a.task_id == task.id]
        return matches[-1] if matches else None

    def pending_approvals(self) -> list[Approval]:
        return [a for a in self.approvals if a.status == ApprovalStatus.PENDING]

    def decide_approval(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        decided_by: str,
        comment: str | None = None,
    ) -> Approval:
        approval = self.get_approval(approval_id)
        identity = decided_by.strip()
        if not identity or identity.lower() in NON_HUMAN_IDENTITIES:
            raise ApprovalNotAuthorizedError("Approval decisions must come from a human approver")
        if approval.status != ApprovalStatus.PENDING:
            raise ApprovalAlreadyDecidedError(
                f"Approval {approval_id} is already {approval.status.value}"
            )
        if self.status != WorkflowStatus.AWAITING_APPROVAL:
            raise InvalidStateTransitionError(
                f"Workflow is {self.status.value}, not AWAITING_APPROVAL"
            )
        approval.status = (
            ApprovalStatus.APPROVED
            if decision == ApprovalDecision.APPROVE
            else ApprovalStatus.REJECTED
        )
        approval.decided_by = identity
        approval.decided_at = utc_now()
        approval.comment = comment
        self.updated_at = approval.decided_at
        self.record(
            EventType.APPROVAL_GRANTED
            if decision == ApprovalDecision.APPROVE
            else EventType.APPROVAL_REJECTED,
            "approval_service",
            approval_id=approval.id,
            task_key=approval.task_key,
            tool=approval.tool_name,
            decided_by=identity,
            comment=comment,
        )
        return approval

    def reject_task(self, task: Task, approval: Approval) -> None:
        self._set_task_status(task, TaskStatus.REJECTED)
        task.error = TaskError(
            code="approval_rejected",
            message=f"Rejected by {approval.decided_by}"
            + (f": {approval.comment}" if approval.comment else ""),
        )
        task.finished_at = utc_now()
        self.skip_dependents(task.key, reason=f"dependency {task.key} was rejected")

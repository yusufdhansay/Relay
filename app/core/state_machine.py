"""Explicit, deterministic state machines for workflows and tasks.

The orchestrator is the only component that drives transitions. LLM output never sets a
status directly: agents return data, and the orchestrator decides the next state.
"""

from __future__ import annotations

from app.core.enums import TaskStatus, WorkflowStatus
from app.core.exceptions import InvalidStateTransitionError

W = WorkflowStatus
T = TaskStatus

WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    W.RECEIVED: frozenset({W.TRIAGING, W.FAILED, W.CANCELLED}),
    W.TRIAGING: frozenset({W.PLANNING, W.FAILED, W.CANCELLED}),
    W.PLANNING: frozenset({W.EXECUTING, W.FAILED, W.CANCELLED}),
    # EXECUTING -> PLANNING is the hand-off from investigation to remediation planning.
    W.EXECUTING: frozenset({W.AWAITING_APPROVAL, W.PLANNING, W.VERIFYING, W.FAILED, W.CANCELLED}),
    W.AWAITING_APPROVAL: frozenset({W.EXECUTING, W.FAILED, W.CANCELLED}),
    # VERIFYING -> EXECUTING is a bounded workflow-level retry.
    W.VERIFYING: frozenset({W.COMPLETED, W.EXECUTING, W.FAILED, W.CANCELLED}),
    W.COMPLETED: frozenset(),
    W.FAILED: frozenset(),
    W.CANCELLED: frozenset(),
}

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    T.PENDING: frozenset({T.AWAITING_APPROVAL, T.RUNNING, T.SKIPPED, T.BLOCKED, T.CANCELLED}),
    T.AWAITING_APPROVAL: frozenset({T.RUNNING, T.REJECTED, T.CANCELLED}),
    # RUNNING -> PENDING is only used by crash recovery for tasks that are safe to repeat.
    T.RUNNING: frozenset({T.SUCCEEDED, T.FAILED, T.PENDING}),
    # FAILED/SKIPPED -> PENDING is only used by a bounded workflow-level retry.
    T.FAILED: frozenset({T.PENDING}),
    T.SKIPPED: frozenset({T.PENDING, T.CANCELLED}),
    T.SUCCEEDED: frozenset(),
    T.BLOCKED: frozenset(),
    T.REJECTED: frozenset(),
    T.CANCELLED: frozenset(),
}


def can_transition_workflow(current: WorkflowStatus, target: WorkflowStatus) -> bool:
    return target in WORKFLOW_TRANSITIONS[current]


def assert_workflow_transition(current: WorkflowStatus, target: WorkflowStatus) -> None:
    if not can_transition_workflow(current, target):
        raise InvalidStateTransitionError(
            f"Workflow cannot move from {current.value} to {target.value}"
        )


def can_transition_task(current: TaskStatus, target: TaskStatus) -> bool:
    return target in TASK_TRANSITIONS[current]


def assert_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    if not can_transition_task(current, target):
        raise InvalidStateTransitionError(
            f"Task cannot move from {current.value} to {target.value}"
        )

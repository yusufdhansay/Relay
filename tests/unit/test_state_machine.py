import itertools

import pytest

from app.core.enums import TaskStatus, WorkflowStatus
from app.core.exceptions import InvalidStateTransitionError
from app.core.state_machine import (
    TASK_TRANSITIONS,
    WORKFLOW_TRANSITIONS,
    assert_task_transition,
    assert_workflow_transition,
    can_transition_workflow,
)

W = WorkflowStatus

HAPPY_PATH = [
    W.RECEIVED,
    W.TRIAGING,
    W.PLANNING,
    W.EXECUTING,
    W.AWAITING_APPROVAL,
    W.EXECUTING,
    W.PLANNING,
    W.EXECUTING,
    W.VERIFYING,
    W.COMPLETED,
]


def test_every_status_has_a_transition_entry() -> None:
    assert set(WORKFLOW_TRANSITIONS) == set(WorkflowStatus)
    assert set(TASK_TRANSITIONS) == set(TaskStatus)


def test_full_lifecycle_path_is_valid() -> None:
    for current, target in itertools.pairwise(HAPPY_PATH):
        assert_workflow_transition(current, target)


@pytest.mark.parametrize("terminal", [W.COMPLETED, W.FAILED, W.CANCELLED])
def test_terminal_states_have_no_exits(terminal: WorkflowStatus) -> None:
    assert terminal.is_terminal
    for target in WorkflowStatus:
        assert not can_transition_workflow(terminal, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (W.RECEIVED, W.EXECUTING),
        (W.RECEIVED, W.COMPLETED),
        (W.TRIAGING, W.EXECUTING),
        (W.PLANNING, W.COMPLETED),
        (W.AWAITING_APPROVAL, W.COMPLETED),
        (W.AWAITING_APPROVAL, W.VERIFYING),
        (W.EXECUTING, W.COMPLETED),
        (W.COMPLETED, W.EXECUTING),
    ],
)
def test_invalid_workflow_transitions_are_rejected(
    current: WorkflowStatus, target: WorkflowStatus
) -> None:
    with pytest.raises(InvalidStateTransitionError):
        assert_workflow_transition(current, target)


def test_completion_requires_verification() -> None:
    sources = [s for s, targets in WORKFLOW_TRANSITIONS.items() if W.COMPLETED in targets]
    assert sources == [W.VERIFYING]


def test_every_non_terminal_state_can_be_cancelled_and_failed() -> None:
    for status in WorkflowStatus:
        if not status.is_terminal:
            assert can_transition_workflow(status, W.CANCELLED)
            assert can_transition_workflow(status, W.FAILED)


def test_rejected_task_cannot_run() -> None:
    with pytest.raises(InvalidStateTransitionError):
        assert_task_transition(TaskStatus.REJECTED, TaskStatus.RUNNING)
    with pytest.raises(InvalidStateTransitionError):
        assert_task_transition(TaskStatus.BLOCKED, TaskStatus.RUNNING)


def test_succeeded_task_cannot_rerun() -> None:
    with pytest.raises(InvalidStateTransitionError):
        assert_task_transition(TaskStatus.SUCCEEDED, TaskStatus.PENDING)

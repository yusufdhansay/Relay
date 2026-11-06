"""Enumerations shared across the domain, agents, persistence, and API layers."""

from __future__ import annotations

from enum import StrEnum


class WorkflowStatus(StrEnum):
    RECEIVED = "RECEIVED"
    TRIAGING = "TRIAGING"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_WORKFLOW_STATUSES


TERMINAL_WORKFLOW_STATUSES = frozenset(
    {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
)


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

    @property
    def is_settled(self) -> bool:
        """True when the task will not run again without an explicit retry."""
        return self not in {TaskStatus.PENDING, TaskStatus.AWAITING_APPROVAL, TaskStatus.RUNNING}


class PlanStage(StrEnum):
    INVESTIGATION = "INVESTIGATION"
    REMEDIATION = "REMEDIATION"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _RISK_RANK[self]

    @classmethod
    def max(cls, *levels: RiskLevel) -> RiskLevel:
        return max(levels, key=lambda level: level.rank)


_RISK_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}


class PolicyVerdict(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"

    @property
    def rank(self) -> int:
        return {"ALLOW": 0, "REQUIRE_APPROVAL": 1, "BLOCK": 2}[self.value]


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class RequestCategory(StrEnum):
    INCIDENT_INVESTIGATION = "INCIDENT_INVESTIGATION"
    CONFIGURATION_CHANGE = "CONFIGURATION_CHANGE"
    HEALTH_CHECK = "HEALTH_CHECK"
    REPORTING = "REPORTING"
    MAINTENANCE = "MAINTENANCE"
    UNSUPPORTED = "UNSUPPORTED"


class Symptom(StrEnum):
    HIGH_LATENCY = "HIGH_LATENCY"
    ELEVATED_ERRORS = "ELEVATED_ERRORS"
    AVAILABILITY = "AVAILABILITY"
    RESOURCE_PRESSURE = "RESOURCE_PRESSURE"


class Outcome(StrEnum):
    """What the requester wants Relay to achieve."""

    INVESTIGATE = "INVESTIGATE"
    HEALTH_CHECK = "HEALTH_CHECK"
    REPORT = "REPORT"
    REMEDIATE = "REMEDIATE"
    RESTART = "RESTART"
    CHANGE_CONFIGURATION = "CHANGE_CONFIGURATION"
    DELETE_DATA = "DELETE_DATA"


CHANGE_OUTCOMES = frozenset(
    {Outcome.REMEDIATE, Outcome.RESTART, Outcome.CHANGE_CONFIGURATION, Outcome.DELETE_DATA}
)


class VerificationRecommendation(StrEnum):
    COMPLETE = "COMPLETE"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"


class Urgency(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EventType(StrEnum):
    WORKFLOW_CREATED = "WORKFLOW_CREATED"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    STATUS_CHANGED = "STATUS_CHANGED"
    AGENT_INVOKED = "AGENT_INVOKED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    REQUEST_CLASSIFIED = "REQUEST_CLASSIFIED"
    PLAN_GENERATED = "PLAN_GENERATED"
    PLAN_REJECTED = "PLAN_REJECTED"
    DIAGNOSIS_RECORDED = "DIAGNOSIS_RECORDED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    ACTION_BLOCKED = "ACTION_BLOCKED"
    UNTRUSTED_CONTENT_FLAGGED = "UNTRUSTED_CONTENT_FLAGGED"
    TOOL_SELECTED = "TOOL_SELECTED"
    TOOL_EXECUTED = "TOOL_EXECUTED"
    TOOL_FAILED = "TOOL_FAILED"
    RETRY_ATTEMPTED = "RETRY_ATTEMPTED"
    TASK_SKIPPED = "TASK_SKIPPED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    WORKFLOW_RETRY = "WORKFLOW_RETRY"
    WORKFLOW_RESUMED = "WORKFLOW_RESUMED"
    WORKFLOW_RECOVERED = "WORKFLOW_RECOVERED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    WORKFLOW_FAILED = "WORKFLOW_FAILED"
    WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"

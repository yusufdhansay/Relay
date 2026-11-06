"""Execution guard: the single place that issues execution permits.

Before any tool runs, the guard re-checks policy on the final arguments. If approval is
required, it also checks that a human approved this exact call (same tool, same argument
fingerprint). The tool registry refuses to run without a matching permit.
"""

from __future__ import annotations

from typing import Any

from app.core.enums import ApprovalStatus, PolicyVerdict, RiskLevel
from app.core.exceptions import ExecutionNotAuthorizedError
from app.core.policy import PolicyEngine
from app.core.workflow import Task, Workflow, fingerprint
from app.tools.base import ExecutionPermit


class ExecutionGuard:
    def __init__(self, policy: PolicyEngine) -> None:
        self.policy = policy

    def authorize(
        self, workflow: Workflow, task: Task, arguments: dict[str, Any]
    ) -> ExecutionPermit:
        read_only = bool((workflow.triage or {}).get("constraints", {}).get("read_only", False))
        decision = self.policy.evaluate(
            task_key=task.key,
            tool_name=task.tool_name,
            arguments=arguments,
            stage=task.stage,
            read_only=read_only,
        )
        if decision.verdict == PolicyVerdict.BLOCK:
            raise ExecutionNotAuthorizedError(
                f"Policy blocks {task.tool_name}: {'; '.join(decision.reasons)}"
            )

        call_hash = fingerprint(task.tool_name, arguments)
        needs_approval = (
            decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
            or task.requires_approval
            or self.policy.registry.get(task.tool_name).mutating
        )
        if not needs_approval:
            return ExecutionPermit(task.tool_name, call_hash, "policy_allow")

        approval = workflow.approval_for_task(task)
        if approval is None or approval.status != ApprovalStatus.APPROVED:
            raise ExecutionNotAuthorizedError(
                f"{task.tool_name} requires a granted human approval before it can run"
            )
        if approval.arguments_hash != call_hash or approval.tool_name != task.tool_name:
            raise ExecutionNotAuthorizedError(
                f"Arguments for {task.tool_name} differ from the approved arguments"
            )
        return ExecutionPermit(task.tool_name, call_hash, "human_approval", approval.id)

    def authorize_probe(self, tool_name: str, arguments: dict[str, Any]) -> ExecutionPermit:
        """Permit for verifier probes: only low-risk, read-only tools."""
        tool = self.policy.registry.get(tool_name)
        if tool.mutating or tool.risk_level != RiskLevel.LOW:
            raise ExecutionNotAuthorizedError(f"{tool_name} cannot be used as a read-only probe")
        normalised = self.policy.registry.normalise_arguments(tool_name, arguments)
        return ExecutionPermit(tool_name, fingerprint(tool_name, normalised), "read_only_probe")

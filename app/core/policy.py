"""Deterministic policy rules.

These rules are the source of truth for whether an action may run. The Policy Agent may
add an LLM review on top, but that review can only make a decision stricter. The same
rules run again at execution time on the final arguments.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.core.enums import PlanStage, PolicyVerdict, RiskLevel
from app.core.exceptions import ToolArgumentError, ToolNotFoundError
from app.schemas.agents import PolicyDecision
from app.tools.registry import ToolRegistry

RULES = {
    "R1_UNKNOWN_TOOL": "Only registered tools may run.",
    "R2_CRITICAL_BLOCKED": "Critical-risk actions (such as deleting data) are always blocked.",
    "R3_READ_ONLY_REQUEST": "The requester forbade changes, so changing actions are blocked.",
    "R4_INVESTIGATION_READ_ONLY": "The investigation stage may only use read-only tools.",
    "R5_APPROVAL_FOR_CHANGES": "Medium and high risk actions need human approval.",
    "R6_CRITICAL_SERVICE": "Changes to tier-1 services are raised one risk level.",
    "R7_INVALID_ARGUMENTS": "Actions with invalid arguments are blocked.",
    "R8_LLM_ESCALATION": "The policy reviewer asked for human approval.",
    "R0_READ_ONLY_ALLOWED": "Low-risk read-only actions run without approval.",
}


class PolicyEngine:
    def __init__(self, registry: ToolRegistry, critical_services: Iterable[str] = ()) -> None:
        self.registry = registry
        self.critical_services = frozenset(critical_services)

    def evaluate(
        self,
        *,
        task_key: str,
        tool_name: str,
        arguments: dict[str, Any],
        stage: PlanStage,
        read_only: bool,
        arguments_complete: bool = True,
    ) -> PolicyDecision:
        try:
            tool = self.registry.get(tool_name)
        except ToolNotFoundError:
            return self._decision(
                task_key, tool_name, PolicyVerdict.BLOCK, RiskLevel.CRITICAL, ["R1_UNKNOWN_TOOL"]
            )

        risk = tool.risk_level
        rules: list[str] = []
        verdict = PolicyVerdict.ALLOW

        if risk == RiskLevel.CRITICAL:
            return self._decision(
                task_key, tool_name, PolicyVerdict.BLOCK, risk, ["R2_CRITICAL_BLOCKED"]
            )
        if tool.mutating and read_only:
            return self._decision(
                task_key, tool_name, PolicyVerdict.BLOCK, risk, ["R3_READ_ONLY_REQUEST"]
            )
        if tool.mutating and stage == PlanStage.INVESTIGATION:
            return self._decision(
                task_key, tool_name, PolicyVerdict.BLOCK, risk, ["R4_INVESTIGATION_READ_ONLY"]
            )
        if arguments_complete:
            try:
                self.registry.validate_arguments(tool_name, arguments)
            except ToolArgumentError as exc:
                decision = self._decision(
                    task_key, tool_name, PolicyVerdict.BLOCK, risk, ["R7_INVALID_ARGUMENTS"]
                )
                decision.reasons.append(exc.message)
                return decision

        if tool.mutating and arguments.get("service") in self.critical_services:
            risk = RiskLevel.max(risk, _raise(risk))
            rules.append("R6_CRITICAL_SERVICE")
        if risk.rank >= RiskLevel.MEDIUM.rank:
            verdict = PolicyVerdict.REQUIRE_APPROVAL
            rules.append("R5_APPROVAL_FOR_CHANGES")
        if not rules:
            rules.append("R0_READ_ONLY_ALLOWED")
        return self._decision(task_key, tool_name, verdict, risk, rules)

    @staticmethod
    def escalate(decision: PolicyDecision, concerns: list[str]) -> PolicyDecision:
        """Apply an LLM escalation. Can only tighten: ALLOW becomes REQUIRE_APPROVAL."""
        if decision.verdict != PolicyVerdict.ALLOW:
            return decision.model_copy(update={"llm_concerns": concerns})
        return decision.model_copy(
            update={
                "verdict": PolicyVerdict.REQUIRE_APPROVAL,
                "risk_level": RiskLevel.max(decision.risk_level, RiskLevel.MEDIUM),
                "rules": [*decision.rules, "R8_LLM_ESCALATION"],
                "reasons": [*decision.reasons, RULES["R8_LLM_ESCALATION"]],
                "llm_concerns": concerns,
            }
        )

    @staticmethod
    def _decision(
        task_key: str, tool: str, verdict: PolicyVerdict, risk: RiskLevel, rules: list[str]
    ) -> PolicyDecision:
        return PolicyDecision(
            task_key=task_key,
            tool=tool,
            verdict=verdict,
            risk_level=risk,
            rules=rules,
            reasons=[RULES[r] for r in rules],
        )


def _raise(risk: RiskLevel) -> RiskLevel:
    """Raise one level, capped at HIGH. CRITICAL is reserved for always-blocked tools."""
    order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH]
    return order[min(order.index(risk) + 1, len(order) - 1)]

"""Verification Agent: independently check whether the objective was achieved.

The verifier does not trust what the Execution Agent reports. It:

1. Runs deterministic checks over the recorded task states and outputs.
2. Probes the environment again with read-only tools (for example, re-reads service
   health after a restart).
3. Asks an LLM for a judgment over that evidence.

The objective counts as achieved only if every deterministic check passes AND the LLM
agrees. The LLM can veto success but cannot create it.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import Agent, AgentResult
from app.agents.context import task_view
from app.core.enums import (
    CHANGE_OUTCOMES,
    Outcome,
    PlanStage,
    TaskStatus,
    VerificationRecommendation,
)
from app.core.exceptions import RelayError
from app.core.guard import ExecutionGuard
from app.core.workflow import Workflow
from app.schemas.agents import (
    Diagnosis,
    TriageResult,
    VerificationCheck,
    VerificationResult,
    VerifierAssessment,
)
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry

HARD_FAILURES = {TaskStatus.BLOCKED, TaskStatus.REJECTED, TaskStatus.CANCELLED}


class VerificationAgent(Agent):
    name = "verification_agent"
    description = "Checks real tool outputs and fresh probes to decide if the objective was met."
    responsibilities = (
        "Run deterministic checks on task results and the requester's constraints",
        "Re-probe affected services with read-only tools instead of trusting claims",
        "Ask an LLM for a judgment that can veto, but never create, success",
        "Recommend completion, a bounded retry, or escalation",
    )
    output_schema_name = "VerificationResult"
    system_prompt = (
        "You are the Verification Agent. Decide whether the user's objective was achieved "
        "using ONLY the evidence in INPUT: task statuses, tool outputs, deterministic checks, "
        "and fresh probes. Do not trust descriptions of success without supporting output. "
        "If any requirement in the request is not supported by evidence, objective_achieved "
        "must be false and the gap must be listed in unmet_requirements."
    )

    def __init__(
        self, llm, *, registry: ToolRegistry, guard: ExecutionGuard, repair_attempts: int = 1
    ) -> None:
        super().__init__(llm, repair_attempts=repair_attempts)
        self.registry = registry
        self.guard = guard

    async def verify(
        self, workflow: Workflow, triage: TriageResult
    ) -> AgentResult[VerificationResult]:
        diagnosis = Diagnosis.model_validate(workflow.diagnosis) if workflow.diagnosis else None
        checks = self._deterministic_checks(workflow, triage, diagnosis)
        probes = await self._probe(workflow, triage, diagnosis)
        for probe in probes:
            healthy = probe.get("status") == "HEALTHY"
            checks.append(
                VerificationCheck(
                    name=f"service_healthy:{probe['service']}",
                    passed=healthy,
                    detail=probe.get("detail", ""),
                )
            )

        assessment, run = await self._generate(
            workflow_id=workflow.id,
            operation="verify",
            payload={
                "request": workflow.request,
                "triage": triage.model_dump(mode="json"),
                "diagnosis": workflow.diagnosis,
                "tasks": [task_view(t) for t in workflow.tasks],
                "checks": [c.model_dump() for c in checks],
                "probes": probes,
            },
            output_model=VerifierAssessment,
        )
        result = self._decide(workflow, checks, probes, assessment)
        run.output = result.model_dump(mode="json")
        return AgentResult(result, [run])

    # ------------------------------------------------------------------ checks
    def _deterministic_checks(
        self, workflow: Workflow, triage: TriageResult, diagnosis: Diagnosis | None
    ) -> list[VerificationCheck]:
        tasks = workflow.tasks
        outcomes = set(triage.requested_outcomes)
        read_only = triage.constraints.read_only
        checks = [
            VerificationCheck(
                name="all_tasks_settled",
                passed=all(t.status.is_settled for t in tasks),
                detail=", ".join(f"{t.key}={t.status.value}" for t in tasks) or "no tasks",
            )
        ]
        unsuccessful = [t for t in tasks if t.status != TaskStatus.SUCCEEDED]
        checks.append(
            VerificationCheck(
                name="all_tasks_succeeded",
                passed=not unsuccessful,
                detail="; ".join(
                    f"{t.key} ({t.tool_name}) {t.status.value}"
                    + (f": {t.error.message[:200]}" if t.error else "")
                    for t in unsuccessful
                )
                or "every task succeeded",
            )
        )
        evidence = [
            t
            for t in workflow.stage_tasks(PlanStage.INVESTIGATION)
            if t.status == TaskStatus.SUCCEEDED and not self._mutating(t.tool_name)
        ]
        checks.append(
            VerificationCheck(
                name="evidence_collected",
                passed=bool(evidence),
                detail=f"{len(evidence)} read-only task(s) returned data",
            )
        )
        changed = [
            t for t in tasks if self._mutating(t.tool_name) and t.status == TaskStatus.SUCCEEDED
        ]
        if read_only:
            checks.append(
                VerificationCheck(
                    name="read_only_respected",
                    passed=not changed,
                    detail="no changing action ran"
                    if not changed
                    else f"changing actions ran: {[t.key for t in changed]}",
                )
            )
        if Outcome.REPORT in outcomes:
            reports = [
                t
                for t in tasks
                if t.tool_name == "generate_incident_report"
                and t.status == TaskStatus.SUCCEEDED
                and (t.output or {}).get("report_id")
            ]
            checks.append(
                VerificationCheck(
                    name="report_generated",
                    passed=bool(reports),
                    detail=reports[0].output["report_id"] if reports else "no report produced",
                )
            )
        if outcomes & CHANGE_OUTCOMES and not read_only:
            if diagnosis is not None and not diagnosis.fault_found:
                checks.append(
                    VerificationCheck(
                        name="remediation_performed",
                        passed=True,
                        detail="no fault found, so no change was needed",
                    )
                )
            else:
                checks.append(
                    VerificationCheck(
                        name="remediation_performed",
                        passed=bool(changed),
                        detail=f"executed: {[t.key for t in changed]}"
                        if changed
                        else "no remediation action was executed",
                    )
                )
        return checks

    async def _probe(
        self, workflow: Workflow, triage: TriageResult, diagnosis: Diagnosis | None
    ) -> list[dict[str, Any]]:
        services: list[str] = []
        for task in workflow.tasks:
            if self._mutating(task.tool_name) and task.status == TaskStatus.SUCCEEDED:
                service = (task.resolved_arguments or task.arguments).get("service")
                if service and service not in services:
                    services.append(service)
        wants_change = bool(set(triage.requested_outcomes) & CHANGE_OUTCOMES)
        if (
            wants_change
            and not triage.constraints.read_only
            and diagnosis
            and diagnosis.fault_found
            and diagnosis.affected_service
            and diagnosis.affected_service not in services
        ):
            services.append(diagnosis.affected_service)

        probes = []
        for service in services:
            args = {"service": service}
            try:
                permit = self.guard.authorize_probe("get_service_health", args)
                invocation = await self.registry.invoke(
                    "get_service_health",
                    args,
                    ToolContext(workflow.id, f"verify:{service}", f"probe:{workflow.id}:{service}"),
                    permit,
                )
                health = invocation.output["services"][0]
                probes.append(
                    {
                        "service": service,
                        "tool": "get_service_health",
                        "status": health["status"],
                        "latency_p95_ms": health["latency_p95_ms"],
                        "error_rate_percent": health["error_rate_percent"],
                        "detail": f"{health['status']} (p95 {health['latency_p95_ms']}ms, "
                        f"errors {health['error_rate_percent']}%)",
                    }
                )
            except RelayError as exc:
                probes.append(
                    {"service": service, "status": "UNKNOWN", "detail": f"probe failed: {exc}"}
                )
        return probes

    # ------------------------------------------------------------------ decision
    def _decide(
        self,
        workflow: Workflow,
        checks: list[VerificationCheck],
        probes: list[dict[str, Any]],
        assessment: VerifierAssessment,
    ) -> VerificationResult:
        failed_checks = [c for c in checks if not c.passed]
        achieved = not failed_checks and assessment.objective_achieved

        retryable = [
            t.key
            for t in workflow.tasks
            if t.status == TaskStatus.FAILED and t.error is not None and t.error.retryable
        ]
        hard = [
            t.key
            for t in workflow.tasks
            if t.status in HARD_FAILURES
            or (t.status == TaskStatus.FAILED and not (t.error and t.error.retryable))
        ]
        probes_ok = all(p.get("status") == "HEALTHY" for p in probes)

        if achieved:
            recommendation = VerificationRecommendation.COMPLETE
            summary = f"Objective achieved. {assessment.rationale}"
        elif retryable and not hard and probes_ok:
            recommendation = VerificationRecommendation.RETRY
            summary = f"Transient failures in {retryable}; a bounded retry may complete the work."
        else:
            recommendation = VerificationRecommendation.ESCALATE
            gaps = [f"{c.name}: {c.detail}" for c in failed_checks]
            if not failed_checks and not assessment.objective_achieved:
                gaps = [f"verifier judgment: {assessment.rationale}"]
            summary = "Objective not achieved. " + " | ".join(gaps)[:900]

        return VerificationResult(
            objective_achieved=achieved,
            recommendation=recommendation,
            checks=checks,
            probes=probes,
            assessment=assessment,
            retry_task_keys=retryable if recommendation == VerificationRecommendation.RETRY else [],
            summary=summary,
        )

    def _mutating(self, tool_name: str) -> bool:
        return self.registry.has(tool_name) and self.registry.get(tool_name).mutating

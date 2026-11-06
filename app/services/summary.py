"""Deterministic, human-readable workflow summary built from recorded state."""

from __future__ import annotations

from typing import Any

from app.core.enums import TaskStatus, WorkflowStatus
from app.core.workflow import Workflow
from app.tools.registry import ToolRegistry


def build_summary(workflow: Workflow, registry: ToolRegistry) -> dict[str, Any]:
    triage = workflow.triage or {}
    verification = workflow.verification or {}
    diagnosis = workflow.diagnosis

    def mutating(tool: str) -> bool:
        return registry.has(tool) and registry.get(tool).mutating

    investigation = [
        {"task": t.key, "tool": t.tool_name, "status": t.status.value}
        for t in workflow.tasks
        if not mutating(t.tool_name)
    ]
    actions = []
    for task in workflow.tasks:
        if not mutating(task.tool_name):
            continue
        approval = workflow.approval_for_task(task)
        actions.append(
            {
                "task": task.key,
                "tool": task.tool_name,
                "arguments": task.resolved_arguments or task.arguments,
                "status": task.status.value,
                "risk_level": task.risk_level.value,
                "approval": approval.status.value if approval else None,
                "decided_by": approval.decided_by if approval else None,
                "result": task.output,
                "error": task.error.message if task.error else None,
            }
        )
    reports = [
        t.output
        for t in workflow.tasks
        if t.tool_name == "generate_incident_report"
        and t.status == TaskStatus.SUCCEEDED
        and t.output
    ]
    failed_checks = [
        f"{c['name']}: {c['detail']}" for c in verification.get("checks", []) if not c["passed"]
    ]

    return {
        "headline": _headline(workflow, actions, reports),
        "status": workflow.status.value,
        "reason": workflow.status_reason,
        "request": workflow.request,
        "category": triage.get("category"),
        "urgency": triage.get("urgency"),
        "services": triage.get("services", []),
        "read_only": (triage.get("constraints") or {}).get("read_only", False),
        "diagnosis": {
            "affected_service": diagnosis.get("affected_service"),
            "root_cause": diagnosis.get("root_cause"),
            "confidence": diagnosis.get("confidence"),
        }
        if diagnosis
        else None,
        "investigation": investigation,
        "actions": actions,
        "verification": {
            "objective_achieved": verification.get("objective_achieved"),
            "recommendation": verification.get("recommendation"),
            "summary": verification.get("summary"),
            "failed_checks": failed_checks,
            "probes": verification.get("probes", []),
        }
        if verification
        else None,
        "report": {
            "report_id": reports[-1]["report_id"],
            "severity": reports[-1]["severity"],
            "markdown": reports[-1]["markdown"],
        }
        if reports
        else None,
        "retries": workflow.retry_count,
    }


def _headline(workflow: Workflow, actions: list[dict[str, Any]], reports: list[dict]) -> str:
    done = [a for a in actions if a["status"] == TaskStatus.SUCCEEDED.value]
    if workflow.status == WorkflowStatus.COMPLETED:
        if done:
            parts = [
                f"{a['tool']} on {a['arguments'].get('service')} (approved by {a['decided_by']})"
                for a in done
            ]
            return "Completed: " + "; ".join(parts) + ". Verification passed."
        if reports:
            return f"Completed: report {reports[-1]['report_id']} written; no changes were made."
        return "Completed: investigation finished; no changes were needed."
    if workflow.status == WorkflowStatus.CANCELLED:
        return f"Cancelled: {workflow.status_reason}"
    return f"Failed: {workflow.status_reason}"

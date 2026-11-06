"""Rule-based responders for the deterministic mock provider.

Each responder receives the same JSON payload a live model would receive and returns a JSON
answer for the agent's output schema. They use keyword rules and simple evidence parsing,
so demos and tests are repeatable without an API key. They are deliberately simple: they
show the pipeline working, not model quality.
"""

from __future__ import annotations

import re
from typing import Any

from app.llm.mock_provider import Responder

_READ_ONLY = re.compile(
    r"do not (make any |make )?(changes?|modify|change|restart|touch)|"
    r"don'?t (make any |make )?(changes?|modify|change|restart|touch)|no changes|read[- ]only|"
    r"without (making |any )?(changes?|changing|modifying)|(only|just) (investigate|look|report)|"
    r"dry[- ]run",
    re.IGNORECASE,
)


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{w}", text) for w in words)


# ---------------------------------------------------------------------- triage
def triage(payload: dict[str, Any]) -> dict[str, Any]:
    request: str = payload["request"]
    text = request.lower()
    known: list[str] = payload.get("known_services", [])

    services = []
    for name in known:
        base = name.removesuffix("-service")
        if name in text or re.search(rf"\b{re.escape(base)}s?\b", text):
            services.append(name)

    symptoms = []
    if _has(text, "latency", "slow", "response time", "timeout", "timing out"):
        symptoms.append("HIGH_LATENCY")
    if _has(text, "error", "failing", "failure", "5xx", "exception"):
        symptoms.append("ELEVATED_ERRORS")
    if _has(text, "down", "outage", "unavailable", "unreachable"):
        symptoms.append("AVAILABILITY")
    if _has(text, "memory", "cpu", "heap", "leak"):
        symptoms.append("RESOURCE_PRESSURE")

    outcomes = []
    if _has(text, "investigate", "diagnose", "why", "root cause", "look into", "analy"):
        outcomes.append("INVESTIGATE")
    if _has(text, "health", "status", "check"):
        outcomes.append("HEALTH_CHECK")
    if _has(text, "report", "postmortem", "write-up", "write up"):
        outcomes.append("REPORT")
    if _has(text, "resolve", "fix", "remediate", "mitigate", "repair"):
        outcomes.append("REMEDIATE")
    if _has(text, "restart", "reboot", "bounce"):
        outcomes.append("RESTART")
    if _has(text, "config") and _has(text, "apply", "change", "update", "set", "modify", "tune"):
        outcomes.append("CHANGE_CONFIGURATION")
    if _has(text, "delete", "purge", "wipe", "erase", "clear"):
        outcomes.append("DELETE_DATA")
    if symptoms and "INVESTIGATE" not in outcomes:
        outcomes.insert(0, "INVESTIGATE")

    read_only = bool(_READ_ONLY.search(request))
    if read_only:
        # A read-only request can still ask for investigation and a report, never changes.
        outcomes = [o for o in outcomes if o not in {"RESTART", "CHANGE_CONFIGURATION"}]

    if "CHANGE_CONFIGURATION" in outcomes:
        category = "CONFIGURATION_CHANGE"
    elif "DELETE_DATA" in outcomes and not symptoms:
        category = "MAINTENANCE"
    elif symptoms or {"INVESTIGATE", "REMEDIATE", "RESTART"} & set(outcomes):
        category = "INCIDENT_INVESTIGATION"
    elif "REPORT" in outcomes and services:
        category = "REPORTING"
    elif "HEALTH_CHECK" in outcomes:
        category = "HEALTH_CHECK"
    else:
        category = "UNSUPPORTED"

    if category == "UNSUPPORTED":
        urgency = "LOW"
    elif _has(text, "outage", "down", "sev1", "critical", "urgent", "asap"):
        urgency = "CRITICAL" if _has(text, "outage", "down", "sev1") else "HIGH"
    elif "HIGH_LATENCY" in symptoms or "AVAILABILITY" in symptoms:
        urgency = "HIGH"
    elif symptoms or {"REMEDIATE", "RESTART", "CHANGE_CONFIGURATION"} & set(outcomes):
        urgency = "MEDIUM"
    else:
        urgency = "LOW"

    missing, assumptions = [], []
    if not services and category != "UNSUPPORTED":
        missing.append("No target service was named")
        assumptions.append("Check every service and focus on any that is not healthy")
    if category == "UNSUPPORTED":
        missing.append("The request is not an IT operations task Relay can perform")

    target = ", ".join(services) or "an unspecified service"
    summary = f"{category.replace('_', ' ').title()} for {target}"
    if symptoms:
        summary += f" ({', '.join(s.lower().replace('_', ' ') for s in symptoms)})"

    return {
        "category": category,
        "urgency": urgency,
        "summary": summary[:300],
        "services": services,
        "symptoms": symptoms,
        "requested_outcomes": outcomes,
        "constraints": {
            "read_only": read_only,
            "notes": ["User asked for no changes"] if read_only else [],
        },
        "missing_information": missing,
        "assumptions": assumptions,
        "can_proceed": category != "UNSUPPORTED",
    }


# ---------------------------------------------------------------------- planning
def plan_investigation(payload: dict[str, Any]) -> dict[str, Any]:
    triage_result = payload["triage"]
    tools = {t["name"] for t in payload["tools"]}
    services: list[str] = triage_result["services"][:2]
    outcomes = set(triage_result["requested_outcomes"])
    symptoms = set(triage_result["symptoms"])
    only_health = outcomes <= {"HEALTH_CHECK"} and not symptoms

    tasks: list[dict[str, Any]] = []

    def add(tool: str, description: str, arguments: dict[str, Any], deps=None, inputs=None):
        if tool not in tools:
            return None
        task_id = f"t{len(tasks) + 1}"
        tasks.append(
            {
                "id": task_id,
                "description": description,
                "tool": tool,
                "arguments": arguments,
                "depends_on": deps or [],
                "input_from": inputs or [],
            }
        )
        return task_id

    severity = "WARN"
    query = None
    if symptoms == {"ELEVATED_ERRORS"}:
        severity = "ERROR"

    evidence_ids: list[str] = []
    if services:
        for service in services:
            evidence_ids.append(
                add(
                    "get_service_health", f"Check current health of {service}", {"service": service}
                )
            )
            if not only_health:
                evidence_ids.append(
                    add(
                        "get_service_metrics",
                        f"Compare {service} metrics with baseline",
                        {"service": service, "window_minutes": 60},
                    )
                )
                evidence_ids.append(
                    add(
                        "search_service_logs",
                        f"Search recent {severity}+ logs for {service}",
                        {"service": service, "min_severity": severity, "window_minutes": 60}
                        | ({"query": query} if query else {}),
                    )
                )
    else:
        overview = add("get_service_health", "Check health of every service", {})
        evidence_ids.append(overview)
        if not only_health:
            evidence_ids.append(
                add(
                    "get_service_metrics",
                    "Read metrics for the least healthy service",
                    {"window_minutes": 60},
                    deps=[overview],
                    inputs=[overview],
                )
            )
            evidence_ids.append(
                add(
                    "search_service_logs",
                    "Search recent warning and error logs for the least healthy service",
                    {"min_severity": severity, "window_minutes": 60},
                    deps=[overview],
                    inputs=[overview],
                )
            )

    evidence_ids = [e for e in evidence_ids if e]
    if "REPORT" in outcomes:
        add(
            "generate_incident_report",
            "Write an incident report from the collected evidence",
            {"service": services[0]} if services else {},
            deps=evidence_ids,
            inputs=evidence_ids,
        )

    return {
        "objective": triage_result["summary"],
        "rationale": "Collect health, metrics, and logs before deciding on any change."
        if not only_health
        else "A health check answers the request.",
        "tasks": tasks[: payload.get("max_tasks", 10)],
    }


def _health_outputs(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for item in findings:
        if item["tool"] == "get_service_health" and item.get("output"):
            results.extend(item["output"]["services"])
    return results


def _detect_fault(service: str, findings: list[dict[str, Any]]) -> tuple[str | None, list[dict]]:
    """Return a fault label and evidence references from logs and metrics."""
    evidence = []
    fault = None
    for item in findings:
        output = item.get("output") or {}
        args = item.get("arguments") or {}
        if args.get("service") not in (service, None) and output.get("service") != service:
            continue
        if item["tool"] == "search_service_logs":
            for entry in output.get("entries", []):
                message = entry["message"].lower()
                if "connection leak" in message or "waiting for database connection" in message:
                    fault = fault or "CONNECTION_LEAK"
                    evidence.append({"task_id": item["task_id"], "detail": entry["message"][:400]})
                elif "heap" in message or "gc " in message or "gc pause" in message:
                    fault = fault or "MEMORY_LEAK"
                    evidence.append({"task_id": item["task_id"], "detail": entry["message"][:400]})
        if item["tool"] == "get_service_metrics":
            for anomaly in output.get("anomalies", []):
                evidence.append({"task_id": item["task_id"], "detail": anomaly[:400]})
                if "db pool saturated" in anomaly:
                    fault = fault or "CONNECTION_LEAK"
                if "memory_percent" in anomaly:
                    fault = fault or "MEMORY_LEAK"
    return fault, evidence[:10]


def plan_remediation(payload: dict[str, Any]) -> dict[str, Any]:
    triage_result = payload["triage"]
    findings: list[dict[str, Any]] = payload["findings"]
    tools = {t["name"] for t in payload["tools"]}
    outcomes = set(triage_result["requested_outcomes"])

    health = _health_outputs(findings)
    unhealthy = [h for h in health if h["status"] != "HEALTHY"]
    preferred = [h for h in unhealthy if h["service"] in triage_result["services"]]
    target = (preferred or unhealthy or [None])[0]
    service = target["service"] if target else (triage_result["services"] or [None])[0]

    tasks: list[dict[str, Any]] = []
    if service and "DELETE_DATA" in outcomes and "purge_service_logs" in tools:
        tasks.append(
            {
                "id": "r1",
                "description": f"Delete old log data for {service} as requested",
                "tool": "purge_service_logs",
                "arguments": {"service": service, "older_than_hours": 24},
                "depends_on": [],
                "input_from": [],
            }
        )

    if target is None:
        return {
            "diagnosis": {
                "affected_service": service,
                "root_cause": "No degraded service found; all checked services are healthy",
                "confidence": "medium",
                "evidence": [
                    {"task_id": f["task_id"], "detail": "health check reported HEALTHY"}
                    for f in findings
                    if f["tool"] == "get_service_health" and f.get("output")
                ][:3],
                "fault_found": False,
            },
            "rationale": "Nothing to remediate.",
            "tasks": tasks,
        }

    fault, evidence = _detect_fault(service, findings)
    pool_max = next(
        (
            f["output"]["db_pool_max"]
            for f in findings
            if f["tool"] == "get_service_metrics" and f.get("output")
        ),
        None,
    )
    if fault == "CONNECTION_LEAK":
        root_cause = (
            f"Database connection leak (PaymentReconciliationJob holds connections) exhausted "
            f"the {service} connection pool ({pool_max or '?'} max); requests queue and time out."
        )
        fix_key, fix_value = "db_pool_max_size", 40
    elif fault == "MEMORY_LEAK":
        root_cause = f"Memory leak in {service}: heap near its limit causes long GC pauses."
        fix_key, fix_value = "heap_max_mb", 3072
    else:
        root_cause = f"{service} is {target['status']} but the logs show no known fault pattern."
        fix_key, fix_value = None, None
    evidence.insert(
        0,
        {
            "task_id": next(f["task_id"] for f in findings if f["tool"] == "get_service_health"),
            "detail": f"{service} {target['status']}: p95 {target['latency_p95_ms']}ms, "
            f"errors {target['error_rate_percent']}%",
        },
    )

    if "CHANGE_CONFIGURATION" in outcomes and fix_key and "update_service_config" in tools:
        tasks.append(
            {
                "id": f"r{len(tasks) + 1}",
                "description": f"Set {fix_key}={fix_value} on {service} (recommended mitigation)",
                "tool": "update_service_config",
                "arguments": {
                    "service": service,
                    "changes": {fix_key: fix_value},
                    "reason": f"Mitigate {fault.lower().replace('_', ' ')}",
                },
                "depends_on": [],
                "input_from": [],
            }
        )
    elif {"REMEDIATE", "RESTART"} & outcomes and "restart_service" in tools:
        tasks.append(
            {
                "id": f"r{len(tasks) + 1}",
                "description": f"Restart {service} to release leaked resources",
                "tool": "restart_service",
                "arguments": {
                    "service": service,
                    "reason": f"Release resources held by {(fault or 'unknown fault').lower()}",
                },
                "depends_on": [],
                "input_from": [],
            }
        )

    return {
        "diagnosis": {
            "affected_service": service,
            "root_cause": root_cause[:500],
            "confidence": "high" if fault else "low",
            "evidence": evidence[:10],
            "fault_found": True,
        },
        "rationale": "Apply the smallest change that addresses the diagnosed root cause.",
        "tasks": tasks,
    }


# ---------------------------------------------------------------------- policy
def policy_review(payload: dict[str, Any]) -> dict[str, Any]:
    assessments = []
    for decision in payload["decisions"]:
        concerns = []
        if decision["tool"] == "restart_service":
            concerns.append("A restart briefly drops in-flight requests for this service")
        if decision["tool"] == "update_service_config":
            concerns.append("Configuration changes persist until reverted; confirm the new value")
        assessments.append(
            {"task_id": decision["task_id"], "escalate": False, "concerns": concerns}
        )
    return {"assessments": assessments}


# ---------------------------------------------------------------------- argument resolution
def resolve_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    arguments = dict(payload["planned_arguments"])
    deps: list[dict[str, Any]] = payload["dependency_outputs"]
    triage_result = payload.get("triage") or {}
    required = set(payload["input_schema"].get("required", []))

    health = [s for d in deps if d["tool"] == "get_service_health" for s in d["output"]["services"]]
    if "service" in required and "service" not in arguments:
        unhealthy = [h for h in health if h["status"] != "HEALTHY"]
        choice = (unhealthy or health or [{}])[0].get("service")
        arguments["service"] = choice or (triage_result.get("services") or ["payment-service"])[0]

    if payload["tool"] == "generate_incident_report":
        arguments.update(_report_arguments(arguments["service"], deps, triage_result))

    return {"arguments": arguments, "rationale": "Filled from dependency outputs."}


def _report_arguments(
    service: str, deps: list[dict[str, Any]], triage_result: dict[str, Any]
) -> dict[str, Any]:
    findings, evidence, recommendations = [], [], []
    status = "UNKNOWN"
    p95 = errors = 0.0
    for dep in deps:
        output = dep["output"] or {}
        if dep["tool"] == "get_service_health":
            for h in output.get("services", []):
                if h["service"] == service:
                    status, p95, errors = h["status"], h["latency_p95_ms"], h["error_rate_percent"]
                    findings.append(
                        f"{service} is {status}: p95 latency {p95}ms, error rate {errors}%"
                    )
                    evidence.append(
                        {"source": dep["task_id"], "detail": f"health: {status}, p95 {p95}ms"}
                    )
        elif dep["tool"] == "get_service_metrics":
            for anomaly in output.get("anomalies", []):
                findings.append(f"Metric anomaly: {anomaly}")
                evidence.append({"source": dep["task_id"], "detail": anomaly})
        elif dep["tool"] == "search_service_logs":
            entries = output.get("entries", [])
            error_entries = [e for e in entries if e["severity"] == "ERROR"]
            findings.append(
                f"{output.get('total_matched', len(entries))} matching log entries, "
                f"{len(error_entries)} at ERROR level"
            )
            for entry in error_entries[:3]:
                evidence.append(
                    {
                        "source": dep["task_id"],
                        "detail": f"{entry['timestamp']}: {entry['message']}",
                    }
                )
            text = " ".join(e["message"].lower() for e in entries)
            if "connection leak" in text or "waiting for database connection" in text:
                findings.append("Errors are caused by database connection pool exhaustion")
                recommendations.append("Fix the connection leak in PaymentReconciliationJob")
                recommendations.append(
                    "Consider raising db_pool_max_size as a short-term mitigation"
                )
            if "heap" in text or "gc pause" in text:
                findings.append("Long GC pauses indicate a memory leak")
                recommendations.append("Capture a heap dump and fix the leak")

    if status == "DOWN":
        severity = "SEV1"
    elif status == "DEGRADED" and (errors > 1 or p95 > 1000):
        severity = "SEV2"
    elif status == "DEGRADED":
        severity = "SEV3"
    else:
        severity = "SEV4"
    read_only = (triage_result.get("constraints") or {}).get("read_only", False)
    recommendations.append("Keep monitoring error rate and p95 latency until stable")
    return {
        "title": f"Incident report: {service} {status.lower()}"[:120],
        "severity": severity,
        "summary": (
            f"Investigation of {service}. Current status {status}, p95 latency {p95}ms, "
            f"error rate {errors}%."
        ),
        "findings": findings[:20] or ["No anomalies found"],
        "evidence": evidence[:30],
        "actions_taken": ["No changes were made (read-only investigation)"] if read_only else [],
        "recommendations": recommendations[:20],
    }


# ---------------------------------------------------------------------- verification
def verify(payload: dict[str, Any]) -> dict[str, Any]:
    failed = [c for c in payload["checks"] if not c["passed"]]
    return {
        "objective_achieved": not failed,
        "confidence": "high",
        "rationale": "All checks and probes support the outcome."
        if not failed
        else f"{len(failed)} check(s) failed: " + "; ".join(c["name"] for c in failed),
        "unmet_requirements": [f"{c['name']}: {c['detail']}"[:500] for c in failed][:10],
    }


DEFAULT_RESPONDERS: dict[str, Responder] = {
    "triage": triage,
    "plan_investigation": plan_investigation,
    "plan_remediation": plan_remediation,
    "policy_review": policy_review,
    "resolve_arguments": resolve_arguments,
    "verify": verify,
}

"""Relay CLI demo: run the three demonstration scenarios through the HTTP API.

By default the app runs in-process (mock LLM, temporary database). Use ``--base-url`` to
drive a running server instead.

    python -m scripts.demo                       # all scenarios, in-process
    python -m scripts.demo --scenario 1          # one scenario
    python -m scripts.demo --base-url http://127.0.0.1:8000 --token change-me-alice
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
import textwrap
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

SCENARIOS: dict[int, dict[str, Any]] = {
    1: {
        "title": "Diagnose and resolve high latency (approve the restart)",
        "request": "Investigate why the payment service is experiencing high latency "
        "and resolve it if possible.",
        "decision": "approve",
    },
    2: {
        "title": "Investigate without remediation (read-only, incident report)",
        "request": "Investigate the recent increase in payment service errors and prepare an "
        "incident report. Do not make any changes.",
        "decision": None,
    },
    3: {
        "title": "Reject a proposed configuration change",
        "request": "Investigate the service and apply the recommended configuration change.",
        "decision": "reject",
    },
}

DEMO_TOKEN = "demo-approver-token"  # noqa: S105  (in-process demo only)


def show(label: str, text: str = "") -> None:
    print(f"  {label:<18} {text}")


def print_workflow(workflow: dict[str, Any]) -> None:
    triage = workflow.get("triage") or {}
    show("status", f"{workflow['status']}  {workflow.get('status_reason') or ''}")
    if triage:
        show(
            "triage",
            f"{triage['category']} / {triage['urgency']} / services={triage['services']} "
            f"/ read_only={triage['constraints']['read_only']}",
        )
    for task in workflow["tasks"]:
        verdict = (task.get("policy") or {}).get("verdict", "")
        show(
            f"task {task['key']}",
            f"{task['tool']:<26} {task['status']:<18} risk={task['risk_level']:<8} {verdict}",
        )
    if workflow.get("diagnosis"):
        show("diagnosis", textwrap.shorten(workflow["diagnosis"]["root_cause"], 110))


async def run_scenario(client: httpx.AsyncClient, number: int, token: str) -> None:
    scenario = SCENARIOS[number]
    print(f"\n=== Scenario {number}: {scenario['title']}")
    print(f'  request: "{scenario["request"]}"\n')
    await client.post("/api/v1/simulation/reset")
    response = await client.post("/api/v1/workflows", json={"request": scenario["request"]})
    response.raise_for_status()
    workflow = response.json()
    print_workflow(workflow)

    if workflow["status"] == "AWAITING_APPROVAL":
        approval = (await client.get("/api/v1/approvals/pending")).json()[0]
        print()
        show("approval needed", f"{approval['tool']} {approval['arguments']}")
        show("risk", f"{approval['risk_level']}: {'; '.join(approval['reasons'])}")
        decision = scenario["decision"] or "reject"
        show("human decides", decision.upper())
        result = await client.post(
            f"/api/v1/approvals/{approval['id']}/decision",
            json={"decision": decision, "comment": f"demo: {decision}"},
            headers={"X-Approver-Token": token},
        )
        result.raise_for_status()
        workflow = result.json()["workflow"]
        print()
        print_workflow(workflow)

    summary = workflow.get("summary") or {}
    verification = summary.get("verification") or {}
    print()
    show("headline", summary.get("headline", ""))
    if verification:
        show("verification", f"achieved={verification['objective_achieved']}")
        for probe in verification.get("probes", []):
            show("  probe", f"{probe['service']}: {probe['detail']}")
    if summary.get("report"):
        show("report", summary["report"]["report_id"])
        print(textwrap.indent(summary["report"]["markdown"].strip(), "      | "))
    events = (await client.get(f"/api/v1/workflows/{workflow['id']}/events")).json()["events"]
    show("audit events", f"{len(events)} recorded")
    for event in events:
        if event["event_type"] in {
            "APPROVAL_REQUESTED",
            "APPROVAL_GRANTED",
            "APPROVAL_REJECTED",
            "ACTION_BLOCKED",
            "VERIFICATION_COMPLETED",
            "WORKFLOW_COMPLETED",
            "WORKFLOW_FAILED",
            "WORKFLOW_CANCELLED",
        }:
            show(f"  #{event['sequence']}", f"{event['event_type']} by {event['component']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=int, choices=sorted(SCENARIOS))
    parser.add_argument("--base-url", help="Drive a running Relay server instead")
    parser.add_argument("--token", help="Approver token for --base-url mode")
    args = parser.parse_args()
    numbers = [args.scenario] if args.scenario else sorted(SCENARIOS)

    async with AsyncExitStack() as stack:
        if args.base_url:
            if not args.token:
                parser.error("--token is required with --base-url")
            client = await stack.enter_async_context(
                httpx.AsyncClient(base_url=args.base_url, timeout=120)
            )
            token = args.token
        else:
            from app.core.config import Settings
            from app.main import create_app
            from app.services.container import Container

            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="relay-demo-")))
            settings = Settings(
                environment="development",
                database_url=f"sqlite+aiosqlite:///{tmp / 'demo.db'}",
                llm_provider="mock",
                log_level="WARNING",
                approver_tokens=SecretStr(f"demo-operator:{DEMO_TOKEN}"),
            )
            app = create_app(container=Container.build(settings))
            await stack.enter_async_context(app.router.lifespan_context(app))
            client = await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay")
            )
            token = DEMO_TOKEN
        for number in numbers:
            await run_scenario(client, number, token)


if __name__ == "__main__":
    asyncio.run(main())

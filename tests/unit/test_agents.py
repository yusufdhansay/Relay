import pytest

from app.agents import AgentInvocationError
from app.core.enums import Outcome, PlanStage, RequestCategory, Symptom, Urgency
from app.schemas.agents import TriageResult
from tests.fixtures.builders import (
    build_components,
    executing_workflow,
    investigation_task,
    run_task_directly,
)
from tests.fixtures.providers import ScriptedProvider

SCENARIO_1 = (
    "Investigate why the payment service is experiencing high latency and resolve it if possible."
)
SCENARIO_2 = (
    "Investigate the recent increase in payment service errors and prepare an incident report. "
    "Do not make any changes."
)
SCENARIO_3 = "Investigate the service and apply the recommended configuration change."


def _triage_answer(**overrides) -> dict:
    base = {
        "category": "INCIDENT_INVESTIGATION",
        "urgency": "HIGH",
        "summary": "Payment errors",
        "services": ["payment-service"],
        "symptoms": ["ELEVATED_ERRORS"],
        "requested_outcomes": ["INVESTIGATE"],
        "constraints": {"read_only": False, "notes": []},
        "missing_information": [],
        "assumptions": [],
        "can_proceed": True,
    }
    return base | overrides


# ---------------------------------------------------------------------- triage
async def test_triage_scenario_one() -> None:
    result = (await build_components().triage.run("wf", SCENARIO_1)).output
    assert result.category == RequestCategory.INCIDENT_INVESTIGATION
    assert result.urgency == Urgency.HIGH
    assert result.services == ["payment-service"]
    assert result.symptoms == [Symptom.HIGH_LATENCY]
    assert Outcome.REMEDIATE in result.requested_outcomes
    assert not result.constraints.read_only


async def test_triage_scenario_two_is_read_only() -> None:
    result = (await build_components().triage.run("wf", SCENARIO_2)).output
    assert result.constraints.read_only
    assert Outcome.REPORT in result.requested_outcomes
    assert Symptom.ELEVATED_ERRORS in result.symptoms


async def test_triage_scenario_three_has_no_service() -> None:
    result = (await build_components().triage.run("wf", SCENARIO_3)).output
    assert result.category == RequestCategory.CONFIGURATION_CHANGE
    assert result.services == []
    assert result.missing_information and result.can_proceed


async def test_triage_unsupported_request_cannot_proceed() -> None:
    result = (await build_components().triage.run("wf", "Write me a poem about the sea")).output
    assert result.category == RequestCategory.UNSUPPORTED
    assert not result.can_proceed


async def test_triage_enforces_read_only_even_if_model_misses_it() -> None:
    llm = ScriptedProvider({"triage": [_triage_answer()]})
    result = (await build_components(llm).triage.run("wf", SCENARIO_2)).output
    assert result.constraints.read_only is True


async def test_triage_rejects_invented_services() -> None:
    llm = ScriptedProvider(
        {"triage": [_triage_answer(services=["billing-service"]), _triage_answer()]}
    )
    agent_result = await build_components(llm).triage.run("wf", SCENARIO_1)
    assert agent_result.output.services == ["payment-service"]
    assert agent_result.runs[0].llm_calls == 2


async def test_triage_cannot_force_unsupported_request_to_proceed() -> None:
    llm = ScriptedProvider({"triage": [_triage_answer(category="UNSUPPORTED", can_proceed=True)]})
    result = (await build_components(llm).triage.run("wf", "hello")).output
    assert result.can_proceed is False


# ---------------------------------------------------------------------- planning
async def _triage(c, request) -> TriageResult:
    return (await c.triage.run("wf", request)).output


async def test_investigation_plan_uses_read_only_tools() -> None:
    c = build_components()
    triage = await _triage(c, SCENARIO_2)
    wf = executing_workflow(SCENARIO_2, triage.model_dump(mode="json"))
    plan = (await c.planner.plan_investigation(wf, triage)).output
    tools = [t.tool_name for t in plan.tasks]
    assert tools == [
        "get_service_health",
        "get_service_metrics",
        "search_service_logs",
        "generate_incident_report",
    ]
    report = plan.tasks[-1]
    assert report.depends_on == ["I1", "I2", "I3"] and report.input_from == ["I1", "I2", "I3"]
    assert all(not c.registry.get(t).mutating for t in tools)
    assert plan.tasks[2].arguments["min_severity"] == "ERROR"


async def test_planner_cannot_invent_tools() -> None:
    bad = {
        "objective": "x" * 5,
        "rationale": "x" * 5,
        "tasks": [{"id": "a", "description": "run shell", "tool": "ssh_exec", "arguments": {}}],
    }
    llm = ScriptedProvider({"plan_investigation": [bad, bad]})
    c = build_components(llm)
    triage = await _triage(c, SCENARIO_1)
    wf = executing_workflow(SCENARIO_1, triage.model_dump(mode="json"))
    with pytest.raises(AgentInvocationError) as info:
        await c.planner.plan_investigation(wf, triage)
    assert "ssh_exec" in info.value.message
    assert "not available" in llm.requests[-1].repair_feedback[0]


async def test_investigation_plan_cannot_include_changes() -> None:
    bad = {
        "objective": "x" * 5,
        "rationale": "x" * 5,
        "tasks": [
            {
                "id": "a",
                "description": "restart now",
                "tool": "restart_service",
                "arguments": {"service": "payment-service", "reason": "because"},
            }
        ],
    }
    llm = ScriptedProvider({"plan_investigation": [bad, bad]})
    c = build_components(llm)
    triage = await _triage(c, SCENARIO_1)
    wf = executing_workflow(SCENARIO_1, triage.model_dump(mode="json"))
    with pytest.raises(AgentInvocationError, match="restart_service"):
        await c.planner.plan_investigation(wf, triage)


async def test_plan_dependencies_must_point_backwards() -> None:
    c = build_components()
    from app.schemas.agents import PlannedTask

    problems = c.planner.validate_tasks(
        [
            PlannedTask(id="a", description="aaa", tool="get_service_health", depends_on=["b"]),
            PlannedTask(id="b", description="bbb", tool="get_service_health"),
            PlannedTask(id="c", description="ccc", tool="get_service_health", input_from=["a"]),
        ],
        {"get_service_health"},
        require_tasks=True,
    )
    assert any("not an earlier task" in p for p in problems)
    assert any("must be in depends_on" in p for p in problems)


async def _investigated(c, request):
    triage = await _triage(c, request)
    wf = executing_workflow(request, triage.model_dump(mode="json"))
    plan = (await c.planner.plan_investigation(wf, triage)).output
    wf.add_tasks(plan.tasks)
    for task in plan.tasks:
        if task.input_from:
            task.arguments = (await c.executor.resolve_arguments(wf, task)).output
        await run_task_directly(c, wf, task)
    return wf, triage


async def test_remediation_plan_diagnoses_leak_and_proposes_restart() -> None:
    c = build_components()
    wf, triage = await _investigated(c, SCENARIO_1)
    result = (await c.planner.plan_remediation(wf, triage)).output
    assert result.diagnosis is not None and result.diagnosis.fault_found
    assert "connection" in result.diagnosis.root_cause.lower()
    assert {e.task_id for e in result.diagnosis.evidence} <= {"I1", "I2", "I3"}
    assert [t.tool_name for t in result.tasks] == ["restart_service"]
    restart = result.tasks[0]
    assert restart.key == "R1" and restart.stage == PlanStage.REMEDIATION
    assert restart.arguments["service"] == "payment-service"


async def test_remediation_plan_for_config_request_without_named_service() -> None:
    c = build_components()
    wf, triage = await _investigated(c, SCENARIO_3)
    result = (await c.planner.plan_remediation(wf, triage)).output
    assert [t.tool_name for t in result.tasks] == ["update_service_config"]
    assert result.tasks[0].arguments["changes"] == {"db_pool_max_size": 40}
    assert result.tasks[0].arguments["service"] == "payment-service"


async def test_remediation_evidence_must_cite_real_tasks() -> None:
    c = build_components()
    wf, triage = await _investigated(c, SCENARIO_1)
    bad = {
        "diagnosis": {
            "affected_service": "payment-service",
            "root_cause": "made up",
            "confidence": "high",
            "evidence": [{"task_id": "Z9", "detail": "fabricated evidence"}],
            "fault_found": True,
        },
        "rationale": "x" * 5,
        "tasks": [],
    }
    llm = ScriptedProvider({"plan_remediation": [bad, bad]})
    c.planner.llm = llm
    with pytest.raises(AgentInvocationError, match="unknown task 'Z9'"):
        await c.planner.plan_remediation(wf, triage)


async def test_agent_runs_track_latency_and_tokens() -> None:
    llm = ScriptedProvider({"triage": [_triage_answer()]})
    result = await build_components(llm).triage.run("wf-1", SCENARIO_1)
    run = result.runs[0]
    assert run.workflow_id == "wf-1" and run.agent == "triage_agent"
    assert run.prompt_tokens == 100 and run.completion_tokens == 20
    assert run.latency_ms >= 0 and run.success


def test_investigation_task_builder_is_valid() -> None:
    task = investigation_task("I1", 1, "get_service_health", {})
    assert task.stage == PlanStage.INVESTIGATION


# ---------------------------------------------------------------------- verification
async def _remediated(c, request=SCENARIO_1):
    from app.core.enums import ApprovalDecision, WorkflowStatus

    wf, triage = await _investigated(c, request)
    remediation = (await c.planner.plan_remediation(wf, triage)).output
    wf.diagnosis = remediation.diagnosis.model_dump(mode="json")
    wf.add_tasks(remediation.tasks)
    task = remediation.tasks[0]
    task.requires_approval = True
    approval = wf.request_approval(task, reasons=["risk"])
    wf.transition_to(WorkflowStatus.AWAITING_APPROVAL)
    wf.decide_approval(approval.id, ApprovalDecision.APPROVE, decided_by="alice")
    wf.transition_to(WorkflowStatus.EXECUTING)
    wf.start_task(task)
    outcome = await c.executor.execute(wf, task, task.arguments)
    assert outcome.success
    wf.complete_task(task, outcome.output)
    return wf, triage


async def test_verifier_probes_service_after_restart() -> None:
    c = build_components()
    wf, triage = await _remediated(c)
    result = (await c.verifier.verify(wf, triage)).output
    assert result.objective_achieved
    assert result.recommendation == "COMPLETE"
    assert result.probes[0]["service"] == "payment-service"
    assert result.probes[0]["status"] == "HEALTHY"


async def test_verifier_probe_overrides_llm_claim_of_success() -> None:
    c = build_components()
    c.env.services["payment-service"].fault_persistent = True
    wf, triage = await _remediated(c)
    claims_success = {
        "objective_achieved": True,
        "confidence": "high",
        "rationale": "The restart worked.",
        "unmet_requirements": [],
    }
    c.verifier.llm = ScriptedProvider({"verify": [claims_success]})
    result = (await c.verifier.verify(wf, triage)).output
    assert not result.objective_achieved
    assert result.recommendation == "ESCALATE"
    failed = [check.name for check in result.checks if not check.passed]
    assert failed == ["service_healthy:payment-service"]


async def test_verifier_llm_can_veto_success() -> None:
    c = build_components()
    wf, triage = await _remediated(c)
    veto = {
        "objective_achieved": False,
        "confidence": "medium",
        "rationale": "Latency evidence is older than the restart.",
        "unmet_requirements": ["fresh latency data"],
    }
    c.verifier.llm = ScriptedProvider({"verify": [veto]})
    result = (await c.verifier.verify(wf, triage)).output
    assert all(check.passed for check in result.checks)
    assert not result.objective_achieved
    assert result.recommendation == "ESCALATE"

import pytest

from app.core.enums import PlanStage, PolicyVerdict, RiskLevel
from tests.fixtures.builders import build_components, executing_workflow, investigation_task
from tests.fixtures.providers import ScriptedProvider

REMEDIATION = PlanStage.REMEDIATION
INVESTIGATION = PlanStage.INVESTIGATION


@pytest.mark.parametrize(
    ("tool", "args", "verdict", "risk"),
    [
        ("get_service_metrics", {"service": "payment-service"}, PolicyVerdict.ALLOW, RiskLevel.LOW),
        ("search_service_logs", {"service": "payment-service"}, PolicyVerdict.ALLOW, RiskLevel.LOW),
        (
            "generate_incident_report",
            {
                "service": "payment-service",
                "title": "Report title",
                "severity": "SEV3",
                "summary": "Summary text here",
                "findings": ["x"],
            },
            PolicyVerdict.ALLOW,
            RiskLevel.LOW,
        ),
        (
            "restart_service",
            {"service": "inventory-service", "reason": "leak"},
            PolicyVerdict.REQUIRE_APPROVAL,
            RiskLevel.MEDIUM,
        ),
        (
            "update_service_config",
            {"service": "inventory-service", "changes": {"log_level": "WARN"}, "reason": "noise"},
            PolicyVerdict.REQUIRE_APPROVAL,
            RiskLevel.HIGH,
        ),
        (
            "purge_service_logs",
            {"service": "inventory-service", "older_than_hours": 24},
            PolicyVerdict.BLOCK,
            RiskLevel.CRITICAL,
        ),
    ],
)
def test_policy_matches_risk_table(tool, args, verdict, risk) -> None:
    c = build_components()
    decision = c.engine.evaluate(
        task_key="T", tool_name=tool, arguments=args, stage=REMEDIATION, read_only=False
    )
    assert decision.verdict == verdict
    assert decision.risk_level == risk
    assert decision.reasons


def test_tier_one_service_raises_restart_risk() -> None:
    c = build_components()
    decision = c.engine.evaluate(
        task_key="R1",
        tool_name="restart_service",
        arguments={"service": "payment-service", "reason": "leak"},
        stage=REMEDIATION,
        read_only=False,
    )
    assert decision.risk_level == RiskLevel.HIGH
    assert "R6_CRITICAL_SERVICE" in decision.rules
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    ("tool", "args", "stage", "read_only", "rule"),
    [
        (
            "restart_service",
            {"service": "payment-service", "reason": "x" * 5},
            REMEDIATION,
            True,
            "R3_READ_ONLY_REQUEST",
        ),
        (
            "restart_service",
            {"service": "payment-service", "reason": "x" * 5},
            INVESTIGATION,
            False,
            "R4_INVESTIGATION_READ_ONLY",
        ),
        ("shell_exec", {"cmd": "rm -rf /"}, REMEDIATION, False, "R1_UNKNOWN_TOOL"),
        (
            "restart_service",
            {"service": "payment-service"},
            REMEDIATION,
            False,
            "R7_INVALID_ARGUMENTS",
        ),
    ],
)
def test_policy_blocks(tool, args, stage, read_only, rule) -> None:
    c = build_components()
    decision = c.engine.evaluate(
        task_key="T", tool_name=tool, arguments=args, stage=stage, read_only=read_only
    )
    assert decision.verdict == PolicyVerdict.BLOCK
    assert rule in decision.rules


async def test_llm_review_can_escalate_but_never_relax() -> None:
    llm = ScriptedProvider(
        {
            "policy_review": [
                {
                    "assessments": [
                        {"task_id": "I1", "escalate": True, "concerns": ["odd scope"]},
                        {"task_id": "R1", "escalate": False, "concerns": []},
                    ]
                }
            ]
        }
    )
    c = build_components(llm)
    wf = executing_workflow("x", {"constraints": {"read_only": False}})
    read = investigation_task("I1", 1, "get_service_health", {"service": "auth-service"})
    purge = investigation_task(
        "R1", 2, "purge_service_logs", {"service": "auth-service", "older_than_hours": 2}
    )
    purge.stage = REMEDIATION
    result = await c.policy.evaluate(wf, [read, purge], read_only=False, stage=REMEDIATION)
    by_key = {d.task_key: d for d in result.output}
    assert by_key["I1"].verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert "R8_LLM_ESCALATION" in by_key["I1"].rules
    assert by_key["R1"].verdict == PolicyVerdict.BLOCK


async def test_policy_review_failure_keeps_rule_decisions() -> None:
    llm = ScriptedProvider({"policy_review": ["garbage", "still garbage"]})
    c = build_components(llm)
    wf = executing_workflow("x", {"constraints": {"read_only": False}})
    read = investigation_task("I1", 1, "get_service_health", {})
    result = await c.policy.evaluate(wf, [read], read_only=False, stage=INVESTIGATION)
    assert result.output[0].verdict == PolicyVerdict.ALLOW
    assert result.runs and result.runs[0].success is False

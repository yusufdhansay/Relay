"""Run the evaluation suite in mock mode and hold the system guarantees to 100%."""

from evals.cases import CASES
from evals.report import render_markdown
from evals.runner import run_suite


async def test_evaluation_suite_system_guarantees() -> None:
    assert len(CASES) >= 20
    assert len({c.id for c in CASES}) == len(CASES)

    report = await run_suite()

    assert report.system["case_errors"] == 0
    assert report.system["duplicate_side_effects"] == 0
    for name in (
        "policy_compliance_rate",
        "approval_enforcement_rate",
        "read_only_adherence",
        "rejection_handling",
        "failure_handling",
        "workflow_status_accuracy",
    ):
        assert report.system[name]["value"] == 1.0, (name, report.system[name])
    assert all(not c.violations for c in report.cases)
    assert "| S1-latency-approve | COMPLETED | COMPLETED |" in render_markdown(report)

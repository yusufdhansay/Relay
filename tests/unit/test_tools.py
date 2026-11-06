import pytest

from app.core.enums import RiskLevel
from app.core.exceptions import (
    ExecutionNotAuthorizedError,
    ToolArgumentError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from app.core.workflow import fingerprint
from app.tools import ExecutionPermit, ToolContext, ToolRegistry
from app.tools.operations import (
    FailureKind,
    FaultType,
    SimulatedEnvironment,
    build_operations_registry,
)
from app.tools.operations.tools import GetServiceHealth

CTX = ToolContext(workflow_id="wf", task_key="I1", idempotency_key="key-1")


def _permit(registry: ToolRegistry, name: str, args: dict) -> ExecutionPermit:
    normalised = registry.normalise_arguments(name, args)
    return ExecutionPermit(name, fingerprint(name, normalised), "policy_allow")


async def _call(registry: ToolRegistry, name: str, args: dict, ctx: ToolContext = CTX):
    return await registry.invoke(name, args, ctx, _permit(registry, name, args))


@pytest.fixture
def env() -> SimulatedEnvironment:
    return SimulatedEnvironment()


@pytest.fixture
def registry(env: SimulatedEnvironment) -> ToolRegistry:
    return build_operations_registry(env)


def test_registry_lists_expected_tools_with_risk(registry: ToolRegistry) -> None:
    risks = {spec.name: spec.risk_level for spec in registry.specs()}
    assert risks == {
        "generate_incident_report": RiskLevel.LOW,
        "get_service_health": RiskLevel.LOW,
        "get_service_metrics": RiskLevel.LOW,
        "purge_service_logs": RiskLevel.CRITICAL,
        "restart_service": RiskLevel.MEDIUM,
        "search_service_logs": RiskLevel.LOW,
        "update_service_config": RiskLevel.HIGH,
    }
    specs = {s.name: s for s in registry.specs()}
    assert specs["restart_service"].mutating and not specs["get_service_health"].mutating
    assert "properties" in specs["search_service_logs"].input_schema


def test_duplicate_registration_is_rejected(env: SimulatedEnvironment) -> None:
    registry = build_operations_registry(env)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(GetServiceHealth(env))


def test_unknown_tool_is_rejected(registry: ToolRegistry) -> None:
    with pytest.raises(ToolNotFoundError):
        registry.get("rm_rf")


@pytest.mark.parametrize(
    ("tool", "args", "fragment"),
    [
        ("get_service_metrics", {}, "service"),
        ("get_service_metrics", {"service": "Payment Service"}, "pattern"),
        ("search_service_logs", {"service": "payment-service", "limit": 1000}, "limit"),
        (
            "search_service_logs",
            {"service": "payment-service", "min_severity": "FATAL"},
            "min_severity",
        ),
        ("restart_service", {"service": "payment-service"}, "reason"),
        ("restart_service", {"service": "payment-service", "reason": "x", "force": True}, "force"),
        (
            "update_service_config",
            {"service": "payment-service", "changes": {"shell": "rm"}, "reason": "fix"},
            "not a configurable key",
        ),
        (
            "update_service_config",
            {"service": "payment-service", "changes": {"db_pool_max_size": 5000}, "reason": "fix"},
            "between",
        ),
        (
            "update_service_config",
            {"service": "payment-service", "changes": {"db_pool_max_size": True}, "reason": "fix"},
            "integer",
        ),
        (
            "purge_service_logs",
            {"service": "payment-service", "older_than_hours": 0},
            "older_than_hours",
        ),
    ],
)
def test_invalid_arguments_are_rejected(
    registry: ToolRegistry, tool: str, args: dict, fragment: str
) -> None:
    with pytest.raises(ToolArgumentError, match=fragment):
        registry.validate_arguments(tool, args)


async def test_health_reports_degraded_payment_service(registry: ToolRegistry) -> None:
    result = await _call(registry, "get_service_health", {"service": "payment-service"})
    health = result.output["services"][0]
    assert health["status"] == "DEGRADED"
    assert health["latency_p95_ms"] == 2850
    everything = await _call(registry, "get_service_health", {})
    statuses = {s["service"]: s["status"] for s in everything.output["services"]}
    assert statuses == {
        "auth-service": "HEALTHY",
        "inventory-service": "HEALTHY",
        "payment-service": "DEGRADED",
    }


async def test_outputs_are_deterministic(env: SimulatedEnvironment) -> None:
    first = await _call(
        build_operations_registry(env), "get_service_metrics", {"service": "payment-service"}
    )
    second = await _call(
        build_operations_registry(SimulatedEnvironment()),
        "get_service_metrics",
        {"service": "payment-service"},
    )
    assert first.output == second.output
    assert any("db pool saturated" in a for a in first.output["anomalies"])


async def test_log_search_filters_by_severity_query_and_window(registry: ToolRegistry) -> None:
    errors = await _call(
        registry, "search_service_logs", {"service": "payment-service", "min_severity": "ERROR"}
    )
    assert errors.output["total_matched"] == 4
    assert all(e["severity"] == "ERROR" for e in errors.output["entries"])
    pool = await _call(
        registry,
        "search_service_logs",
        {"service": "payment-service", "min_severity": "INFO", "query": "connection pool"},
    )
    assert pool.output["total_matched"] == 1
    recent = await _call(
        registry, "search_service_logs", {"service": "payment-service", "window_minutes": 10}
    )
    assert all(
        "2850ms" in e["message"] or "upstream" in e["message"] for e in recent.output["entries"]
    )


async def test_unknown_service_is_a_non_retryable_error(registry: ToolRegistry) -> None:
    with pytest.raises(ToolExecutionError) as info:
        await _call(registry, "get_service_health", {"service": "billing-service"})
    assert not info.value.retryable


async def test_restart_clears_fault_and_replays_by_idempotency_key(
    registry: ToolRegistry, env: SimulatedEnvironment
) -> None:
    args = {"service": "payment-service", "reason": "release leaked connections"}
    first = await _call(registry, "restart_service", args)
    assert first.output["restarted"] and first.output["status_after"] == "HEALTHY"
    again = await _call(registry, "restart_service", args)
    assert again.output["replayed"] is True
    assert env.mutation_count("restart_service") == 1
    assert env.services["payment-service"].restart_count == 1


async def test_persistent_fault_survives_restart(env: SimulatedEnvironment) -> None:
    env.services["payment-service"].fault_persistent = True
    registry = build_operations_registry(env)
    result = await _call(
        registry, "restart_service", {"service": "payment-service", "reason": "try"}
    )
    assert result.output["status_after"] == "DEGRADED"


async def test_config_change_mitigates_connection_leak(
    registry: ToolRegistry, env: SimulatedEnvironment
) -> None:
    result = await _call(
        registry,
        "update_service_config",
        {"service": "payment-service", "changes": {"db_pool_max_size": 40}, "reason": "pool"},
    )
    assert result.output["applied"]["db_pool_max_size"] == {"old": 10, "new": 40}
    assert result.output["status_after"] == "HEALTHY"
    assert env.services["payment-service"].fault == FaultType.CONNECTION_LEAK


async def test_permit_must_match_tool_and_arguments(
    registry: ToolRegistry, env: SimulatedEnvironment
) -> None:
    approved = {"service": "auth-service", "reason": "approved restart"}
    permit = _permit(registry, "restart_service", approved)
    tampered = {"service": "payment-service", "reason": "approved restart"}
    with pytest.raises(ExecutionNotAuthorizedError):
        await registry.invoke("restart_service", tampered, CTX, permit)
    other_tool = {"service": "auth-service", "changes": {"log_level": "DEBUG"}, "reason": "x" * 5}
    with pytest.raises(ExecutionNotAuthorizedError):
        await registry.invoke("update_service_config", other_tool, CTX, permit)
    assert env.mutation_count() == 0


async def test_timeout_is_enforced(env: SimulatedEnvironment) -> None:
    env.hang_seconds = 5
    env.inject_failure("get_service_health", FailureKind.TIMEOUT)
    registry = build_operations_registry(env, default_timeout_seconds=0.05)
    with pytest.raises(ToolTimeoutError):
        await _call(registry, "get_service_health", {})


async def test_transient_failure_is_marked_retryable(env: SimulatedEnvironment) -> None:
    env.inject_failure("get_service_metrics", FailureKind.TRANSIENT)
    registry = build_operations_registry(env)
    with pytest.raises(ToolExecutionError) as info:
        await _call(registry, "get_service_metrics", {"service": "payment-service"})
    assert info.value.retryable
    ok = await _call(registry, "get_service_metrics", {"service": "payment-service"})
    assert ok.output["service"] == "payment-service"


async def test_incident_report_renders_markdown(
    registry: ToolRegistry, env: SimulatedEnvironment
) -> None:
    result = await _call(
        registry,
        "generate_incident_report",
        {
            "service": "payment-service",
            "title": "Payment errors",
            "severity": "SEV2",
            "summary": "Errors rose due to pool exhaustion.",
            "findings": ["Pool saturated"],
            "evidence": [{"source": "I3", "detail": "Timed out waiting for connection"}],
        },
    )
    assert result.output["report_id"].startswith("INC-")
    assert "## Findings\n- Pool saturated" in result.output["markdown"]
    assert len(env.reports) == 1

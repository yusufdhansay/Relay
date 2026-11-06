"""Deterministic simulated IT operations environment.

Nothing here touches real infrastructure. Services, metrics, and logs are generated from
fixed profiles relative to a fixed reference time, so the same inputs always give the same
outputs. Tests and evaluations can inject faults and tool failures.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.core.exceptions import ToolExecutionError

REFERENCE_TIME = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)

SEVERITY_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}


class FaultType(StrEnum):
    NONE = "NONE"
    CONNECTION_LEAK = "CONNECTION_LEAK"
    MEMORY_LEAK = "MEMORY_LEAK"


class FailureKind(StrEnum):
    TRANSIENT = "transient"  # fails before any side effect, safe to retry
    PERMANENT = "permanent"  # fails before any side effect, not retryable
    TIMEOUT = "timeout"  # hangs before any side effect
    TIMEOUT_AFTER_EFFECT = "timeout_after_effect"  # side effect happens, then hangs


# Allowed configuration keys: (type, minimum, maximum). Strings use a set of choices.
CONFIG_LIMITS: dict[str, tuple[type, Any, Any]] = {
    "db_pool_max_size": (int, 1, 200),
    "request_timeout_ms": (int, 100, 60_000),
    "heap_max_mb": (int, 256, 16_384),
    "log_level": (str, {"DEBUG", "INFO", "WARN", "ERROR"}, None),
    "feature_reconciliation_job": (bool, None, None),
}


@dataclass(frozen=True)
class Metrics:
    latency_p50_ms: float
    latency_p95_ms: float
    error_rate_percent: float
    cpu_percent: float
    memory_percent: float
    db_pool_active: int
    requests_per_minute: int


@dataclass(frozen=True)
class FaultProfile:
    metrics: Metrics
    onset_minutes_ago: int
    # The configuration change that mitigates this fault: key and minimum value.
    config_fix_key: str
    config_fix_min: int
    config_fix_recommended: int
    logs: tuple[tuple[int, str, str], ...]  # (minutes ago, severity, message template)


FAULT_PROFILES: dict[FaultType, FaultProfile] = {
    FaultType.CONNECTION_LEAK: FaultProfile(
        metrics=Metrics(940, 2850, 4.2, 38, 61, 10, 1150),
        onset_minutes_ago=35,
        config_fix_key="db_pool_max_size",
        config_fix_min=30,
        config_fix_recommended=40,
        logs=(
            (40, "INFO", "PaymentReconciliationJob started batch 1182"),
            (
                35,
                "WARN",
                "Connection held for 312s by PaymentReconciliationJob; possible connection leak",
            ),
            (
                30,
                "WARN",
                "Database connection pool near capacity (active=9, max={db_pool_max_size})",
            ),
            (
                25,
                "ERROR",
                "Timed out waiting for database connection after 5000ms "
                "(active={db_pool_max_size}, max={db_pool_max_size}, waiting=31)",
            ),
            (18, "ERROR", "POST /v1/payments failed: upstream timeout after 5000ms"),
            (
                12,
                "ERROR",
                "Timed out waiting for database connection after 5000ms "
                "(active={db_pool_max_size}, max={db_pool_max_size}, waiting=48)",
            ),
            (5, "WARN", "p95 latency 2850ms exceeds SLO of 500ms"),
            (2, "ERROR", "POST /v1/payments failed: upstream timeout after 5000ms"),
        ),
    ),
    FaultType.MEMORY_LEAK: FaultProfile(
        metrics=Metrics(610, 1900, 1.1, 71, 94, 6, 980),
        onset_minutes_ago=50,
        config_fix_key="heap_max_mb",
        config_fix_min=3072,
        config_fix_recommended=3072,
        logs=(
            (50, "WARN", "Heap usage at 82% of {heap_max_mb}MB after full GC"),
            (38, "WARN", "GC pause of 1400ms detected (old generation)"),
            (27, "ERROR", "GC overhead limit approaching: heap usage 93% of {heap_max_mb}MB"),
            (15, "ERROR", "Request handler stalled for 1900ms during full GC"),
            (6, "WARN", "p95 latency 1900ms exceeds SLO of 500ms"),
        ),
    ),
}


@dataclass
class ServiceState:
    name: str
    tier: int
    version: str
    baseline: Metrics
    config: dict[str, Any]
    fault: FaultType = FaultType.NONE
    fault_persistent: bool = False  # True: a restart does not clear the fault
    uptime_seconds: int = 1_209_600
    restart_count: int = 0
    extra_logs: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def fault_active(self) -> bool:
        if self.fault == FaultType.NONE:
            return False
        profile = FAULT_PROFILES[self.fault]
        return self.config.get(profile.config_fix_key, 0) < profile.config_fix_min


def default_services() -> list[ServiceState]:
    base_config = {
        "request_timeout_ms": 5000,
        "heap_max_mb": 2048,
        "log_level": "INFO",
    }
    return [
        ServiceState(
            name="payment-service",
            tier=1,
            version="2.14.0",
            baseline=Metrics(85, 180, 0.3, 32, 55, 4, 1200),
            config={**base_config, "db_pool_max_size": 10, "feature_reconciliation_job": True},
            fault=FaultType.CONNECTION_LEAK,
        ),
        ServiceState(
            name="auth-service",
            tier=1,
            version="3.2.1",
            baseline=Metrics(40, 95, 0.1, 25, 48, 6, 3100),
            config={**base_config, "db_pool_max_size": 20},
        ),
        ServiceState(
            name="inventory-service",
            tier=2,
            version="1.8.4",
            baseline=Metrics(60, 140, 0.2, 29, 52, 3, 640),
            config={**base_config, "db_pool_max_size": 15},
        ),
    ]


_HEALTHY_LOGS: tuple[tuple[int, str, str], ...] = (
    (55, "INFO", "Health check passed"),
    (32, "INFO", "Configuration reloaded (log_level={log_level})"),
    (20, "WARN", "Cache hit ratio 0.82 below target 0.85"),
    (8, "INFO", "Health check passed"),
)


class SimulatedEnvironment:
    def __init__(self, services: list[ServiceState] | None = None) -> None:
        self._initial = copy.deepcopy(services) if services else None
        self.reset()

    def reset(self) -> None:
        """Restore the initial services and clear all recorded state."""
        initial = copy.deepcopy(self._initial) if self._initial else default_services()
        self.services = {s.name: s for s in initial}
        self.reports: dict[str, dict[str, Any]] = {}
        self.mutations: list[dict[str, Any]] = []
        self._idempotent_results: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, list[FailureKind]] = {}
        self.calls: dict[str, int] = {}
        self.hang_seconds = 30.0
        self._post_restart_logs: dict[str, list[tuple[int, str, str]]] = {}

    # ------------------------------------------------------------------ fault injection
    def inject_failure(self, tool_name: str, kind: FailureKind, times: int = 1) -> None:
        self._failures.setdefault(tool_name, []).extend([kind] * times)

    async def before_call(self, tool_name: str) -> FailureKind | None:
        """Count the call and apply any injected failure that happens before side effects."""
        self.calls[tool_name] = self.calls.get(tool_name, 0) + 1
        queue = self._failures.get(tool_name)
        if not queue:
            return None
        kind = queue.pop(0)
        if kind == FailureKind.TRANSIENT:
            raise ToolExecutionError(
                f"{tool_name}: upstream temporarily unavailable", retryable=True
            )
        if kind == FailureKind.PERMANENT:
            raise ToolExecutionError(
                f"{tool_name}: operation rejected by upstream", retryable=False
            )
        if kind == FailureKind.TIMEOUT:
            await asyncio.sleep(self.hang_seconds)
        return kind

    async def after_effect(self, kind: FailureKind | None) -> None:
        if kind == FailureKind.TIMEOUT_AFTER_EFFECT:
            await asyncio.sleep(self.hang_seconds)

    # ------------------------------------------------------------------ reads
    def service(self, name: str) -> ServiceState:
        try:
            return self.services[name]
        except KeyError:
            known = ", ".join(sorted(self.services))
            raise ToolExecutionError(f"Unknown service '{name}'. Known services: {known}") from None

    def current_metrics(self, service: ServiceState) -> Metrics:
        if service.fault_active:
            return FAULT_PROFILES[service.fault].metrics
        if service.fault != FaultType.NONE:
            # Mitigated by configuration: close to baseline, slightly elevated.
            b = service.baseline
            return Metrics(
                b.latency_p50_ms * 1.2,
                b.latency_p95_ms * 1.3,
                b.error_rate_percent,
                b.cpu_percent + 3,
                b.memory_percent + 2,
                b.db_pool_active + 2,
                b.requests_per_minute,
            )
        return service.baseline

    def status(self, service: ServiceState) -> str:
        metrics = self.current_metrics(service)
        if metrics.error_rate_percent >= 25:
            return "DOWN"
        if metrics.latency_p95_ms > 500 or metrics.error_rate_percent > 1.0:
            return "DEGRADED"
        return "HEALTHY"

    def metric_series(self, service: ServiceState, window_minutes: int, points: int = 12):
        interval = max(1, window_minutes // points)
        current = self.current_metrics(service)
        onset = FAULT_PROFILES[service.fault].onset_minutes_ago if service.fault_active else None
        series = []
        for i in range(points - 1, -1, -1):
            minutes_ago = i * interval
            timestamp = REFERENCE_TIME - timedelta(minutes=minutes_ago)
            if onset is None or minutes_ago > onset:
                values = service.baseline if onset is not None else current
            else:
                progress = 1 - (minutes_ago / onset) if onset else 1.0
                values = _blend(service.baseline, current, max(0.25, progress))
            series.append((timestamp, values))
        return interval, series

    def logs(self, service: ServiceState) -> list[tuple[datetime, str, str]]:
        templates: list[tuple[int, str, str]] = list(_HEALTHY_LOGS)
        if service.fault != FaultType.NONE:
            templates += list(FAULT_PROFILES[service.fault].logs)
        templates += service.extra_logs
        templates += self._post_restart_logs.get(service.name, [])
        entries = []
        for minutes_ago, severity, message in templates:
            text = message.format(**service.config) if "{" in message else message
            entries.append((REFERENCE_TIME - timedelta(minutes=minutes_ago), severity, text))
        entries.sort(key=lambda e: e[0], reverse=True)
        return entries

    def add_log(self, service: str, severity: str, message: str, minutes_ago: int = 1) -> None:
        self.service(service).extra_logs.append((minutes_ago, severity, message))

    # ------------------------------------------------------------------ writes
    def replay(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._idempotent_results.get(idempotency_key)

    def restart(self, name: str, reason: str, idempotency_key: str) -> dict[str, Any]:
        service = self.service(name)
        previous_uptime = service.uptime_seconds
        service.restart_count += 1
        service.uptime_seconds = 90
        if not service.fault_persistent:
            service.fault = FaultType.NONE
        self._post_restart_logs.setdefault(name, []).append(
            (0, "INFO", f"Service restarted by Relay (reason: {reason[:80]})")
        )
        result = {
            "service": name,
            "restarted": True,
            "restart_number": service.restart_count,
            "previous_uptime_seconds": previous_uptime,
            "status_after": self.status(service),
        }
        self._record_mutation("restart_service", idempotency_key, result)
        return result

    def update_config(
        self, name: str, changes: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        service = self.service(name)
        applied = {
            key: {"old": service.config.get(key), "new": value} for key, value in changes.items()
        }
        service.config.update(changes)
        result = {"service": name, "applied": applied, "status_after": self.status(service)}
        self._record_mutation("update_service_config", idempotency_key, result)
        return result

    def purge_logs(self, name: str, older_than_hours: int, idempotency_key: str) -> dict[str, Any]:
        service = self.service(name)
        removed = len(service.extra_logs)
        service.extra_logs.clear()
        result = {"service": name, "deleted_entries": removed, "older_than_hours": older_than_hours}
        self._record_mutation("purge_service_logs", idempotency_key, result)
        return result

    def store_report(self, report: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        existing = self.replay(idempotency_key)
        if existing is not None:
            return existing
        self.reports[report["report_id"]] = report
        self._idempotent_results[idempotency_key] = report
        return report

    def _record_mutation(self, tool: str, idempotency_key: str, result: dict[str, Any]) -> None:
        self.mutations.append({"tool": tool, "idempotency_key": idempotency_key, **result})
        self._idempotent_results[idempotency_key] = result

    def mutation_count(self, tool: str | None = None) -> int:
        return sum(1 for m in self.mutations if tool is None or m["tool"] == tool)


def _blend(a: Metrics, b: Metrics, weight: float) -> Metrics:
    def mix(x: float, y: float) -> float:
        return round(x + (y - x) * weight, 1)

    return Metrics(
        mix(a.latency_p50_ms, b.latency_p50_ms),
        mix(a.latency_p95_ms, b.latency_p95_ms),
        mix(a.error_rate_percent, b.error_rate_percent),
        mix(a.cpu_percent, b.cpu_percent),
        mix(a.memory_percent, b.memory_percent),
        round(mix(a.db_pool_active, b.db_pool_active)),
        round(mix(a.requests_per_minute, b.requests_per_minute)),
    )

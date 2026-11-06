"""Triage Agent: classify the request and extract entities and constraints."""

from __future__ import annotations

import re

from app.agents.base import Agent, AgentResult
from app.core.enums import RequestCategory
from app.schemas.agents import TriageResult

# Phrases that always mean "do not change anything". Detected in code as well as by the
# model, and the stricter answer wins. A model that misses the restriction cannot cause a
# change.
READ_ONLY_PATTERNS = (
    r"\bdo not (make any |make )?(changes?|modify|change|restart|touch)\b",
    r"\bdon'?t (make any |make )?(changes?|modify|change|restart|touch)\b",
    r"\bno changes\b",
    r"\bread[- ]only\b",
    r"\bwithout (making |any )?(changes?|changing|modifying)\b",
    r"\b(only|just) (investigate|look|report|diagnose)\b",
    r"\bdry[- ]run\b",
)
_READ_ONLY = re.compile("|".join(READ_ONLY_PATTERNS), re.IGNORECASE)


def detect_read_only(request: str) -> bool:
    return bool(_READ_ONLY.search(request))


class TriageAgent(Agent):
    name = "triage_agent"
    description = "Classifies the request and extracts services, symptoms, goals, and limits."
    responsibilities = (
        "Classify the request by category and urgency",
        "Extract target services, symptoms, and requested outcomes",
        "Detect explicit constraints such as 'do not make any changes'",
        "List missing information and the assumptions made",
        "Decide whether Relay can act on the request at all",
    )
    output_schema_name = "TriageResult"
    system_prompt = (
        "You are the Triage Agent for an IT operations platform. Read the user's request and "
        "return a structured classification.\n"
        "- category: INCIDENT_INVESTIGATION for problems with a service (latency, errors, "
        "outages, restarts), CONFIGURATION_CHANGE when the user asks to change configuration, "
        "HEALTH_CHECK for status questions, REPORTING for report-only requests, MAINTENANCE "
        "for data housekeeping, UNSUPPORTED for anything outside IT operations.\n"
        "- services: only names from INPUT.known_services. Never invent a service.\n"
        "- requested_outcomes: every outcome the user asked for (INVESTIGATE, HEALTH_CHECK, "
        "REPORT, REMEDIATE, RESTART, CHANGE_CONFIGURATION, DELETE_DATA). 'Resolve it if "
        "possible' means REMEDIATE.\n"
        "- constraints.read_only: true if the user forbids changes in any wording.\n"
        "- missing_information/assumptions: say what is unclear and what you assume.\n"
        "- can_proceed: false only when the request is UNSUPPORTED or impossible to act on."
    )

    def __init__(self, llm, *, known_services: list[str], repair_attempts: int = 1) -> None:
        super().__init__(llm, repair_attempts=repair_attempts)
        self.known_services = sorted(known_services)

    async def run(self, workflow_id: str, request: str) -> AgentResult[TriageResult]:
        known = set(self.known_services)

        def validate(result: TriageResult) -> list[str]:
            unknown = [s for s in result.services if s not in known]
            return [f"Unknown services {unknown}; use only known_services"] if unknown else []

        result, run = await self._generate(
            workflow_id=workflow_id,
            operation="triage",
            payload={"request": request, "known_services": self.known_services},
            output_model=TriageResult,
            semantic_validator=validate,
        )
        result = self._enforce(request, result)
        run.output = result.model_dump(mode="json")
        return AgentResult(result, [run])

    @staticmethod
    def _enforce(request: str, result: TriageResult) -> TriageResult:
        """Deterministic corrections that only ever make the result safer."""
        constraints = result.constraints
        if detect_read_only(request) and not constraints.read_only:
            constraints = constraints.model_copy(
                update={
                    "read_only": True,
                    "notes": [*constraints.notes, "Read-only restriction detected in request"][:5],
                }
            )
        can_proceed = result.can_proceed and result.category != RequestCategory.UNSUPPORTED
        return result.model_copy(update={"constraints": constraints, "can_proceed": can_proceed})

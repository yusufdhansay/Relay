"""Context management: shrink tool outputs before they are sent to a model.

Tool outputs can be large (metric series, many log lines). Agents only need the parts
that carry signal, and every byte sent to a model costs tokens and latency.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.workflow import Task

MAX_LOG_ENTRIES = 20
MAX_SERIES_POINTS = 4
MAX_TEXT = 600

# Text in tool output that reads like an instruction to the model. Flagging does not make
# anything safe on its own (policy and approvals do that); it helps the model and the
# audit trail notice possible prompt injection.
_INSTRUCTION_LIKE = re.compile(
    r"(ignore (all |any )?(previous|prior|above) instructions|disregard .{0,20}(rules|policy)|"
    r"you are now|system prompt|^\s*(system|assistant)\s*:|approve (this|all)|"
    r"run the following|execute the following)",
    re.IGNORECASE,
)


def looks_like_instruction(text: str) -> bool:
    return bool(_INSTRUCTION_LIKE.search(text))


def find_instruction_like(value: Any, limit: int = 3) -> list[str]:
    """Return up to ``limit`` strings in a tool output that look like injected instructions."""
    found: list[str] = []

    def walk(item: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(item, str):
            if looks_like_instruction(item):
                found.append(item[:200])
        elif isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return found


def compact_output(tool_name: str, output: dict[str, Any] | None) -> dict[str, Any] | None:
    if output is None:
        return None
    data = dict(output)
    if tool_name == "get_service_metrics" and isinstance(data.get("series"), list):
        data["series"] = data["series"][-MAX_SERIES_POINTS:]
    if tool_name == "search_service_logs" and isinstance(data.get("entries"), list):
        entries = []
        for entry in data["entries"][:MAX_LOG_ENTRIES]:
            message = str(entry.get("message", ""))[:MAX_TEXT]
            item = {**entry, "message": message}
            if looks_like_instruction(message):
                item["untrusted_instruction_detected"] = True
            entries.append(item)
        data["entries"] = entries
    if tool_name == "generate_incident_report":
        data.pop("markdown", None)
    return data


def task_view(task: Task, *, include_output: bool = True) -> dict[str, Any]:
    """A compact, JSON-safe description of a task for agent payloads."""
    view: dict[str, Any] = {
        "task_id": task.key,
        "stage": task.stage.value,
        "tool": task.tool_name,
        "description": task.description,
        "arguments": task.resolved_arguments or task.arguments,
        "status": task.status.value,
    }
    if task.error:
        view["error"] = {"code": task.error.code, "message": task.error.message[:MAX_TEXT]}
    if include_output:
        view["output"] = compact_output(task.tool_name, task.output)
    return view

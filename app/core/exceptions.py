"""Domain exceptions.

Each exception has a stable machine-readable ``code``. The API layer maps codes to HTTP
status codes and never exposes stack traces.
"""

from __future__ import annotations


class RelayError(Exception):
    code = "relay_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# Workflow and state errors
class InvalidStateTransitionError(RelayError):
    code = "invalid_state_transition"


class WorkflowNotFoundError(RelayError):
    code = "workflow_not_found"


class TaskNotFoundError(RelayError):
    code = "task_not_found"


class ApprovalNotFoundError(RelayError):
    code = "approval_not_found"


class ApprovalAlreadyDecidedError(RelayError):
    code = "approval_already_decided"


class ApprovalNotAuthorizedError(RelayError):
    code = "approval_not_authorized"


class ConcurrencyConflictError(RelayError):
    code = "concurrency_conflict"


class WorkflowBusyError(RelayError):
    code = "workflow_busy"


class WorkflowNotResumableError(RelayError):
    code = "workflow_not_resumable"


class DuplicateRequestConflictError(RelayError):
    code = "idempotency_key_conflict"


# Tool errors
class ToolNotFoundError(RelayError):
    code = "tool_not_found"


class ToolArgumentError(RelayError):
    code = "invalid_tool_arguments"


class ToolExecutionError(RelayError):
    """Raised by tool handlers. ``retryable`` marks transient failures."""

    code = "tool_execution_failed"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ToolTimeoutError(ToolExecutionError):
    code = "tool_timeout"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ToolOutputError(RelayError):
    code = "invalid_tool_output"


class ExecutionNotAuthorizedError(RelayError):
    """Raised when something tries to run a tool without a valid policy or approval."""

    code = "execution_not_authorized"


# Agent and LLM errors
class PlanValidationError(RelayError):
    code = "invalid_plan"

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


class AgentOutputError(RelayError):
    code = "invalid_agent_output"


class LLMProviderError(RelayError):
    code = "llm_provider_error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ConfigurationError(RelayError):
    code = "configuration_error"

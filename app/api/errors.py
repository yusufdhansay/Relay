"""Map domain errors to HTTP responses. Stack traces and internals are never returned."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.exceptions import RelayError
from app.core.logging import get_logger

logger = get_logger("api")

STATUS_BY_CODE = {
    "workflow_not_found": 404,
    "task_not_found": 404,
    "approval_not_found": 404,
    "invalid_state_transition": 409,
    "approval_already_decided": 409,
    "workflow_not_resumable": 409,
    "workflow_busy": 409,
    "concurrency_conflict": 409,
    "idempotency_key_conflict": 409,
    "approval_not_authorized": 403,
    "invalid_tool_arguments": 422,
    "configuration_error": 500,
}


def _error(status: int, code: str, message: str, details: list | None = None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RelayError)
    async def relay_error(_: Request, exc: RelayError) -> JSONResponse:
        status = STATUS_BY_CODE.get(exc.code, 400)
        if status >= 500:
            logger.error("api.server_error", extra={"fields": {"code": exc.code}})
            return _error(status, exc.code, "The server is not configured correctly")
        return _error(status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo submitted values back: they may contain secrets.
        details = [
            {"location": [str(p) for p in err.get("loc", [])], "message": err.get("msg", "")}
            for err in exc.errors()
        ]
        return _error(422, "validation_error", "The request is not valid", details)

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("api.unexpected_error", exc_info=exc)
        return _error(500, "internal_error", "An internal error occurred")

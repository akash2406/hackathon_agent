"""Typed API errors and the handlers that turn every failure into an ``ErrorEnvelope``.

Sits at the outer edge of the "user asks a question" flow: whatever goes wrong
(bad token, Foundry down, database down, unexpected bug) the client receives the
same honest envelope shape with a correlation id, never a raw stack trace and
never a fabricated answer dressed up as success.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .contracts import ErrorBody, ErrorCode, ErrorEnvelope

log = logging.getLogger(__name__)

CORRELATION_HEADER = "x-correlation-id"


class ApiError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, status_code: int, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


class PersistenceError(Exception):
    """Database is unreachable or a statement failed. Raised by the repository layer."""


def correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


def envelope(request: Request, code: ErrorCode, message: str, status_code: int, retryable: bool = False) -> JSONResponse:
    cid = correlation_id(request)
    body = ErrorEnvelope(error=ErrorBody(code=code, message=message, correlation_id=cid, retryable=retryable))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"), headers={CORRELATION_HEADER: cid})


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def _correlation(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = request.state.correlation_id
        return response

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return envelope(request, exc.code, exc.message, exc.status_code, exc.retryable)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return envelope(request, ErrorCode.INVALID_REQUEST, f"Invalid request: {details}", 422)

    @app.exception_handler(PersistenceError)
    async def _persistence(request: Request, exc: PersistenceError) -> JSONResponse:
        log.exception("persistence failure", extra={"correlation_id": correlation_id(request)})
        return envelope(
            request, ErrorCode.PERSISTENCE_UNAVAILABLE, "The conversation store is unavailable. Please retry.", 503, True
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"correlation_id": correlation_id(request)})
        return envelope(request, ErrorCode.INTERNAL_ERROR, "Unexpected server error. No answer was produced.", 500)

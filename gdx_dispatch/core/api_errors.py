"""
Standardized API error responses for GDX.

Every endpoint in gdx_dispatch/routers/* currently returns different error shapes —
some use `{"detail": "..."}` via JSONResponse, some raise HTTPException, some
return `{"error": "..."}`, some return bare dicts with status 500. Frontend
error handling has to special-case each one.

This module provides the canonical shape + helpers so new code can migrate:

    {
      "error": {
        "code": "machine_readable_code",
        "message": "Human readable message",
        "details": { ... } | null,
        "request_id": "req_xxx" | null
      }
    }

Migration guide:
  - Replace `raise HTTPException(status_code=404, detail="...")`
    with      `raise NotFoundError("...")`
  - Replace `return JSONResponse({"error": "..."}, status_code=409)`
    with      `raise ConflictError("...")`
  - Wire into app.py once:
        from gdx_dispatch.core.api_errors import register_api_errors
        register_api_errors(app)
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError


class StandardErrorResponse(BaseModel):
    """Canonical error envelope returned by every GDX API endpoint."""
    error: dict[str, Any]


class APIError(Exception):
    """Base class for every domain error. Subclass for specific HTTP codes."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class NotFoundError(APIError):
    def __init__(self, message: str = "Resource not found", details: dict[str, Any] | None = None) -> None:
        super().__init__(status.HTTP_404_NOT_FOUND, "not_found", message, details)


class ValidationFailedError(APIError):
    def __init__(self, message: str = "Validation failed", details: dict[str, Any] | None = None) -> None:
        super().__init__(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", message, details)


class ConflictError(APIError):
    def __init__(self, message: str = "Resource conflict", details: dict[str, Any] | None = None) -> None:
        super().__init__(status.HTTP_409_CONFLICT, "conflict", message, details)


class PermissionDeniedError(APIError):
    def __init__(self, message: str = "Permission denied", details: dict[str, Any] | None = None) -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, "permission_denied", message, details)


class UnauthorizedError(APIError):
    def __init__(self, message: str = "Unauthorized", details: dict[str, Any] | None = None) -> None:
        super().__init__(status.HTTP_401_UNAUTHORIZED, "unauthorized", message, details)


def _request_id(request: Request | None) -> str | None:
    if request is None:
        return None
    return getattr(getattr(request, "state", None), "request_id", None)


def _envelope(code: str, message: str, details: dict[str, Any] | None, req_id: str | None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": req_id,
        }
    }


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.code, exc.message, exc.details, _request_id(request)),
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # Map common HTTP codes to canonical codes so the frontend can branch on code
    code_map = {
        400: "bad_request",
        401: "unauthorized",
        403: "permission_denied",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        410: "gone",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_error",
        503: "service_unavailable",
    }
    code = code_map.get(exc.status_code, f"http_{exc.status_code}")
    message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
    details = exc.detail if isinstance(exc.detail, dict) else None
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, message, details, _request_id(request)),
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic per-field errors
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_envelope(
            "validation_error",
            "Request validation failed",
            {"errors": exc.errors()},
            _request_id(request),
        ),
    )


async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(
            "database_error",
            "An internal database error occurred",
            {"sql_exception": type(exc).__name__},
            _request_id(request),
        ),
    )


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> JSONResponse:
    """Return a standardized JSONResponse without raising.

    Use this in routes that want to return an error as a regular response
    (e.g., when returning a typed error object is more convenient than raising).
    Pass the FastAPI `request` to include its request_id in the envelope.
    """
    return JSONResponse(
        status_code=status_code,
        content=_envelope(code, message, details, _request_id(request)),
    )


def register_api_errors(app: FastAPI) -> None:
    """Wire the standard error handlers onto a FastAPI app instance.

    Call once in create_app() after routers are registered:
        from gdx_dispatch.core.api_errors import register_api_errors
        register_api_errors(app)
    """
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)

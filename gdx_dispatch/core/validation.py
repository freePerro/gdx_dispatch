"""Shared request-validation helpers for router boundaries.

Context: red-team Pattern 4 — many routers accepted bare ``Body(...)``
dicts with no schema enforcement. Extra keys silently ignored, missing
keys bubbled as 500s, malformed UUIDs crashed ORM calls. This module
gives every router a small, consistent toolkit:

- ``StrictBaseModel``: Pydantic base with ``extra="forbid"`` and
  ``str_strip_whitespace=True``. Use as the parent class for request
  models so typos and injection attempts surface as 4xx, not 200.
- ``require_fields(payload, required)``: raises a structured 400 on the
  first missing key. Structured body matches the platform error-body
  contract: ``{"error_type": ..., "detail": ..., "field": ...}``.
- ``reject_extra_fields(payload, allowed)``: raises a structured 400 on
  the first unexpected key. Mirror of ``require_fields`` for the
  opposite direction.
- ``validate_uuid(value, field_name)``: returns a ``UUID`` or raises 400.
- ``validate_email(value)``: strict RFC-ish regex. Minimum 3 chars
  before ``@``, no consecutive dots.
"""
from __future__ import annotations

import re
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict


class StrictBaseModel(BaseModel):
    """Base for request models — forbids extras, strips whitespace."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


def require_fields(payload: dict, required: list[str]) -> None:
    """Raise 400 on the first missing required key."""
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "invalid_payload",
                "detail": "request body must be a JSON object",
                "field": None,
            },
        )
    for name in required:
        if name not in payload or payload[name] is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_type": "missing_field",
                    "detail": f"required field '{name}' missing",
                    "field": name,
                },
            )


def reject_extra_fields(payload: dict, allowed: set[str]) -> None:
    """Raise 400 on the first unexpected key."""
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "invalid_payload",
                "detail": "request body must be a JSON object",
                "field": None,
            },
        )
    for key in payload:
        if key not in allowed:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_type": "unexpected_field",
                    "detail": f"unexpected field '{key}'",
                    "field": key,
                },
            )


def validate_uuid(value: str, field_name: str) -> UUID:
    """Return a UUID or raise 400 with a structured body."""
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "invalid_uuid",
                "detail": f"field '{field_name}' is not a valid UUID",
                "field": field_name,
            },
        )


# RFC-ish: minimum 3 chars local-part, one @, domain with a dot, no spaces.
# Consecutive dots rejected separately (regex alone is noisy).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(value: str) -> str:
    """Return a validated email string or raise 400."""
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "invalid_email",
                "detail": "email must be a string",
                "field": "email",
            },
        )
    stripped = value.strip()
    if ".." in stripped:
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "invalid_email",
                "detail": "email must not contain consecutive dots",
                "field": "email",
            },
        )
    if not _EMAIL_RE.match(stripped):
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "invalid_email",
                "detail": "email is not a valid address",
                "field": "email",
            },
        )
    local, _, _ = stripped.partition("@")
    if len(local) < 3:
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "invalid_email",
                "detail": "email local-part must be at least 3 characters",
                "field": "email",
            },
        )
    return stripped

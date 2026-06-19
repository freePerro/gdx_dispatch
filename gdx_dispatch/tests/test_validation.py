"""Unit tests for gdx_dispatch.core.validation."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from gdx_dispatch.core.validation import (
    StrictBaseModel,
    reject_extra_fields,
    require_fields,
    validate_email,
    validate_uuid,
)


class _Example(StrictBaseModel):
    name: str
    count: int = 0


def test_strict_base_model_forbids_extra_and_strips_whitespace():
    # Strip whitespace from string inputs.
    m = _Example(name="  hello  ", count=3)
    assert m.name == "hello"
    # Forbid extras.
    with pytest.raises(Exception):  # pydantic ValidationError
        _Example(name="x", count=1, extra_key="nope")


def test_require_fields_happy_path():
    # Happy path — no raise.
    require_fields({"a": 1, "b": 2}, ["a", "b"])


def test_require_fields_missing():
    with pytest.raises(HTTPException) as exc:
        require_fields({"a": 1}, ["a", "b"])
    assert exc.value.status_code == 400
    assert exc.value.detail["error_type"] == "missing_field"
    assert exc.value.detail["field"] == "b"


def test_reject_extra_fields_happy_path():
    reject_extra_fields({"a": 1}, {"a", "b"})


def test_reject_extra_fields_failure():
    with pytest.raises(HTTPException) as exc:
        reject_extra_fields({"a": 1, "junk": 2}, {"a"})
    assert exc.value.status_code == 400
    assert exc.value.detail["error_type"] == "unexpected_field"
    assert exc.value.detail["field"] == "junk"


def test_validate_uuid_good_and_bad():
    u = uuid.uuid4()
    assert validate_uuid(str(u), "id") == u
    with pytest.raises(HTTPException) as exc:
        validate_uuid("not-a-uuid", "id")
    assert exc.value.status_code == 400
    assert exc.value.detail["error_type"] == "invalid_uuid"


def test_validate_email_good_and_bad():
    assert validate_email("doug@example.com") == "doug@example.com"
    # Bare string, no @.
    with pytest.raises(HTTPException):
        validate_email("notanemail")
    # Missing @.
    with pytest.raises(HTTPException):
        validate_email("foo.example.com")
    # Consecutive dots.
    with pytest.raises(HTTPException):
        validate_email("foo..bar@example.com")

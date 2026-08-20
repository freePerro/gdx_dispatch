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


# ── ReDoS regression (CodeQL py/polynomial-redos, PR #372) ──────────────────


def test_email_regex_stays_linear_on_a_pathological_input():
    """`_EMAIL_RE` must not be quadratic in the number of dots.

    The pattern used to spell the domain `[^@\\s]+\\.[^@\\s]+`. Both sides of
    that literal dot could themselves match dots, so every dot in the input was
    a candidate split point and a NON-matching string forced the engine through
    all of them. The trailing space below can never satisfy the final `+`,
    which is what makes the engine exhaust every position.

    Measured on the old pattern: n=16000 → 2.1s, n=40000 → 13.0s (a 4x input
    for 16x the time — textbook quadratic). The linear form does n=40000 in
    ~0.003s. The 2-second budget here is ~600x the fixed cost and ~1/6th of the
    old cost at this size, so it flags a regression without being timing-flaky
    on a loaded CI box.
    """
    import time

    from gdx_dispatch.core.validation import _EMAIL_RE

    payload = "a@" + "b." * 40000 + " "
    started = time.perf_counter()
    assert _EMAIL_RE.match(payload) is None
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, (
        f"_EMAIL_RE took {elapsed:.2f}s on {len(payload)} chars — the dot-split "
        "ambiguity is back; keep the domain as dot-free labels joined by "
        "explicit dots"
    )


def test_log_redact_email_regex_is_the_same_linear_pattern():
    """log_redact keeps its own copy, and it runs over arbitrary log VALUES —
    no length cap at all, where the API paths bound email input at 254 chars.
    A fix applied to one copy and not the other is the worse outcome."""
    from gdx_dispatch.core.log_redact import _EMAIL_RE as REDACT_RE
    from gdx_dispatch.core.validation import _EMAIL_RE as VALIDATE_RE

    assert REDACT_RE.pattern == VALIDATE_RE.pattern


def test_email_regex_rejects_the_malformed_domains_it_used_to_accept():
    """The linear form is stricter in exactly one direction: a leading or
    trailing dot in the domain. The old pattern accepted `a@b.c.` because
    `[^@\\s]+` happily swallowed the interior dot."""
    from gdx_dispatch.core.validation import _EMAIL_RE

    for good in ("a@b.c", "sue@acme.example", "first.last@sub.domain.co.uk"):
        assert _EMAIL_RE.match(good), f"{good} must still be accepted"
    for bad in ("a@b.c.", "a@.b", "a@b..c", "a@bc", "a b@c.d", "a@b c.d"):
        assert _EMAIL_RE.match(bad) is None, f"{bad} must be rejected"

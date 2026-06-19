"""Regression brake — middleware-caught 500s must reach the control-plane
error sink so they appear on the CC dashboard (/cockpit/support/errors).

2026-05-19 incident: the job-save AttributeError 500 was caught by
``ErrorHandlerMiddleware.dispatch`` (NOT FastAPI's
``global_exception_handler``), and that path logged the error but never
called ``record_server_error``. Result: a 6-day prod outage produced
ZERO rows in ``server_errors`` and was invisible to operators looking at
the CC error dashboard, even though the dashboard itself works.

This is a behavioral test (calls ``_handle`` and observes whether the
sink is invoked) — not a source-substring scan — because the claim under
test is behavioral: "a middleware-caught 500 is recorded; a 4xx / 503 is
not." A static scan was explicitly called out as theater in the
2026-05-19 /audit; this exercises the gate logic directly without
needing the app or a DB.
"""
from __future__ import annotations

import types

import pytest

from gdx_dispatch.core.error_handler import ErrorHandlerMiddleware


def _fake_request(path: str = "/api/jobs", method: str = "POST"):
    state = types.SimpleNamespace(request_id="req-test-1", tenant={"id": None}, user=None)
    url = types.SimpleNamespace(path=path, query="")
    return types.SimpleNamespace(state=state, url=url, method=method, headers={})


class _FakeOperationalError(Exception):
    """Name contains 'OperationalError' → _handle maps it to status 503."""


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    def _spy(*, request, exc, status_code, request_id=None):  # noqa: ANN001
        calls.append({"status_code": status_code, "exc": type(exc).__name__})

    # _sink does `from gdx_dispatch.modules.error_sink import record_server_error`
    # at call time, so patching the package attribute intercepts it.
    monkeypatch.setattr(
        "gdx_dispatch.modules.error_sink.record_server_error", _spy, raising=False
    )
    return calls


def _handle(exc: Exception):
    mw = ErrorHandlerMiddleware(app=None)
    return mw._handle(_fake_request(), exc)


def test_middleware_500_is_sinked(captured) -> None:
    """An unmapped exception → status 500 → MUST hit the sink. This is the
    exact shape of the job-save AttributeError that went invisible."""
    resp = _handle(AttributeError("'JobCreate' object has no attribute 'x'"))
    assert resp.status_code == 500
    assert len(captured) == 1, (
        "middleware-caught 500 was NOT written to server_errors — it will "
        "be invisible on /cockpit/support/errors, reopening the 2026-05-19 "
        "blind spot."
    )
    assert captured[0]["status_code"] == 500


def test_middleware_502_is_sinked(captured) -> None:
    """Mapped server-side fault (StripeError → 502) is still a server
    fault and must be recorded."""

    class StripeError(Exception):
        pass

    resp = _handle(StripeError("card network down"))
    assert resp.status_code == 502
    assert len(captured) == 1 and captured[0]["status_code"] == 502


def test_middleware_4xx_is_not_sinked(captured) -> None:
    """Client errors (ValueError → 400) must NOT spam the server-error
    sink — mirrors global_exception_handler's `status >= 500` gate."""
    resp = _handle(ValueError("bad input"))
    assert resp.status_code == 400
    assert captured == [], "4xx must not be written to server_errors"


def test_middleware_503_is_not_sinked(captured) -> None:
    """503 (transient DB-unavailable) is excluded, exactly as
    global_exception_handler excludes it — avoids flooding the sink
    during a DB blip."""
    resp = _handle(_FakeOperationalError("connection reset"))
    assert resp.status_code == 503
    assert captured == [], "503 must be excluded from the sink"

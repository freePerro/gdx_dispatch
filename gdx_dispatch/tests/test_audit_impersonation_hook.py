"""Phase D — tenant-side impersonation audit hook (cc2-s46 follow-through).

When a request arrives at the tenant API with a JWT carrying
``imp_actor_id`` (minted by the CC impersonate endpoint), every audit
row written during that request gets a ``_impersonation`` key in its
``details`` dict — preserving the forensic trail back to the operator
who minted the token.

This is unit-level: we exercise ``log_audit_event_sync`` directly with
a mock Request that has a populated ``state.user``. End-to-end (real
JWT decode → request.state.user populated → audit) is a follow-up
integration test that needs a full app + tenant DB harness.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from gdx_dispatch.core.audit import _extract_impersonation


def _stub_request(*, user: dict | None) -> MagicMock:
    """Build a Request-shaped mock with state.user populated (or not)."""
    state = SimpleNamespace()
    if user is not None:
        state.user = user
    req = MagicMock()
    req.state = state
    return req


def test_extract_impersonation_returns_none_when_no_user():
    req = _stub_request(user=None)
    assert _extract_impersonation(req) == (None, None)


def test_extract_impersonation_returns_none_when_user_has_no_imp():
    req = _stub_request(user={
        "user_id": "u-123",
        "tenant_id": "t-456",
        "role": "user",
        # No imp_actor_id / imp_purpose — normal user token.
    })
    assert _extract_impersonation(req) == (None, None)


def test_extract_impersonation_returns_actor_and_purpose():
    req = _stub_request(user={
        "user_id": "u-123",
        "tenant_id": "t-456",
        "role": "user",
        "imp_actor_id": "operator-uuid-aaa",
        "imp_purpose": "investigate ticket #999",
    })
    actor, purpose = _extract_impersonation(req)
    assert actor == "operator-uuid-aaa"
    assert purpose == "investigate ticket #999"


def test_extract_impersonation_handles_none_request():
    """Audit logger sometimes called without a request (e.g. background tasks)."""
    assert _extract_impersonation(None) == (None, None)


def test_extract_impersonation_handles_non_dict_user():
    """Defensive: state.user might be set to something weird by middleware."""
    req = _stub_request(user=None)
    req.state.user = "string-not-dict"
    assert _extract_impersonation(req) == (None, None)

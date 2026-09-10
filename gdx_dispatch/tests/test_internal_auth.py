"""The plugin-host /internal/* auth policy (#596).

`plugin_host/app.py` had two copies of this rule — one in http middleware, one
inline in the websocket, because Starlette middleware never runs for websocket
scope. Both read `if token and ...`, so both failed OPEN with the token unset,
which is how production ran. These tests pin the policy itself; the wiring is
pinned in test_plugin_events.py against a live TestClient.
"""
from __future__ import annotations

import pytest

from gdx_dispatch.core import internal_auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GDX_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("GDX_ENV", raising=False)
    # SECRET_KEY too: a token is DERIVED from it when no explicit one is set,
    # so leaving it in the ambient environment would make "no token" tests pass
    # for the wrong reason (or fail confusingly on a box that exports it).
    monkeypatch.delenv("SECRET_KEY", raising=False)


@pytest.mark.parametrize("env", ["", "dev", "development", "test", "testing", "local", "ci"])
def test_not_enforced_in_known_dev_environments(monkeypatch, env):
    """A fresh clone and the test suite must work with nothing configured."""
    monkeypatch.setenv("GDX_ENV", env)
    assert internal_auth.enforced() is False
    assert internal_auth.rejection_reason("") is None


@pytest.mark.parametrize("env", ["production", "prod", "staging", "prod-eu", "Production", "qa"])
def test_enforced_for_anything_not_a_known_dev_name(monkeypatch, env):
    """The retired Twilio gate enforced for an ALLOWLIST of prod-like names, so
    `prod-eu` silently disabled it. Inverting that is the point: unknown
    enforces. `Production` also proves the comparison is case-folded."""
    monkeypatch.setenv("GDX_ENV", env)
    assert internal_auth.enforced() is True


def test_enforced_whenever_a_token_is_configured_even_in_dev(monkeypatch):
    """Setting a token is an explicit request to have it checked."""
    monkeypatch.setenv("GDX_ENV", "dev")
    monkeypatch.setenv("GDX_INTERNAL_TOKEN", "tok")
    assert internal_auth.enforced() is True
    assert internal_auth.rejection_reason("") == "internal token required"
    assert internal_auth.rejection_reason("tok") is None


def test_fails_closed_when_enforced_and_no_token_configured(monkeypatch):
    """THE regression this module exists for. Revert `rejection_reason` to the
    old `if token and ...` and this is the assertion that goes red."""
    monkeypatch.setenv("GDX_ENV", "production")
    reason = internal_auth.rejection_reason("")
    assert reason is not None
    assert "not configured" in reason
    # A caller guessing a token gets no further than one presenting none.
    assert internal_auth.rejection_reason("guess") is not None


def test_the_unset_env_contract_is_deliberate(monkeypatch):
    """Unset GDX_ENV does NOT enforce. Pinned so it is a decision, not a drift.

    This matches core.inbound_email_auth and core/pii.py, and is the OPPOSITE of
    gdx_dispatch/app.py:1114 (`is_prod = env in ("", ...)`). That divergence is
    filed separately. If it is ever reconciled the other way, this test is the
    one that should be changed on purpose rather than discovered by surprise.
    """
    monkeypatch.delenv("GDX_ENV", raising=False)
    assert internal_auth.enforced() is False
    # …but a configured token still enforces, which is what makes an explicit
    # deployment safe even with GDX_ENV unset.
    monkeypatch.setenv("GDX_INTERNAL_TOKEN", "tok")
    assert internal_auth.enforced() is True


def test_the_refusal_body_never_says_whether_it_is_misconfigured():
    """`REFUSAL_DETAIL` is what goes on the wire. Telling an unauthenticated
    caller "the gate is unconfigured" tells them a window is open."""
    assert internal_auth.REFUSAL_DETAIL == "internal token required"
    # The detailed reasons exist, and are strictly for the log.
    assert "not configured" not in internal_auth.REFUSAL_DETAIL


def test_wrong_token_is_refused(monkeypatch):
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv("GDX_INTERNAL_TOKEN", "right")
    assert internal_auth.rejection_reason("wrong") == "internal token required"
    assert internal_auth.rejection_reason("") == "internal token required"
    assert internal_auth.rejection_reason("right") is None


def test_non_ascii_header_is_refused_not_a_500(monkeypatch):
    """Starlette latin-1-decodes header values and hmac.compare_digest raises
    TypeError on non-ASCII str operands — comparing as bytes avoids turning a
    hostile header into an unhandled 500."""
    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv("GDX_INTERNAL_TOKEN", "right")
    assert internal_auth.rejection_reason("café—π") == "internal token required"


def test_the_caller_side_sends_the_header_the_host_reads(monkeypatch):
    """One definition of the header name, used by both sides. They were two
    string literals in two files before, differing only in case."""
    from gdx_dispatch.core.plugin_consent import internal_auth_headers
    from gdx_dispatch.plugin_host.app import INTERNAL_TOKEN_HEADER

    monkeypatch.setenv("GDX_INTERNAL_TOKEN", "tok")
    headers = internal_auth_headers()
    assert headers == {INTERNAL_TOKEN_HEADER: "tok"}
    # and the value it sends is one the host accepts
    assert internal_auth.rejection_reason(headers[INTERNAL_TOKEN_HEADER]) is None


def test_a_401_from_plugin_host_is_an_error_not_a_quiet_zero(monkeypatch, caplog):
    """The fail-closed flip creates a new way for event delivery to break, and
    the task must not report it as an ordinary "nobody was interested".

    Before this, a 401 fell into the generic 4xx branch: log.warning + return 0.
    The task then completes successfully while silently dropping every plugin
    event for as long as the token is missing — a silent no-op, which this
    repo treats as a defect of the highest class.
    """
    from unittest.mock import MagicMock, patch

    from gdx_dispatch.core import plugin_events

    resp = MagicMock(status_code=401)
    with (
        patch.object(plugin_events, "SessionLocal"),
        patch.object(plugin_events, "event_recipients", return_value=(["n8n"], [])),
        patch.object(plugin_events.httpx, "post", return_value=resp),
        caplog.at_level("ERROR"),
    ):
        out = plugin_events.deliver_plugin_event_task.run({"event": "invoice.paid"})

    assert out == 0
    assert "plugin_event_dispatch_unauthorized" in caplog.text
    assert "GDX_INTERNAL_TOKEN" in caplog.text
    assert "dropped" in caplog.text.lower()


def test_the_caller_side_logs_loudly_when_enforced_but_unconfigured(monkeypatch, caplog):
    """A 401 from plugin-host only says "someone was refused". The caller is the
    side that knows WHICH container is missing the variable."""
    from gdx_dispatch.core.plugin_consent import internal_auth_headers

    monkeypatch.setenv("GDX_ENV", "production")
    with caplog.at_level("ERROR"):
        assert internal_auth_headers() == {}
    assert "No internal token available" in caplog.text
    assert "GDX_INTERNAL_TOKEN" in caplog.text

    # …and stays quiet in dev, where the gate is deliberately off.
    caplog.clear()
    monkeypatch.setenv("GDX_ENV", "dev")
    with caplog.at_level("ERROR"):
        assert internal_auth_headers() == {}
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# Deriving the token from SECRET_KEY — what makes fail-closed deployable
# ---------------------------------------------------------------------------

def test_the_token_is_derived_from_secret_key_when_none_is_set(monkeypatch):
    """Neither live stack can be handed a new env var by a PR: prod mints no
    secrets and demo's compose is untracked. Deriving from a secret they already
    share is what lets this ship without taking the plugin surface down."""
    monkeypatch.setenv("SECRET_KEY", "a-shared-secret-key")
    derived = internal_auth.token()
    assert derived
    # Stable: two containers with the same SECRET_KEY compute the same token.
    assert internal_auth.token() == derived
    # One-way: the seed is not recoverable from, or present in, the token.
    assert "a-shared-secret-key" not in derived
    assert derived != "a-shared-secret-key"


def test_a_different_secret_key_yields_a_different_token(monkeypatch):
    """Prod and demo have different SECRET_KEYs (verified 2026-09-09), so they
    must not end up sharing an internal token."""
    monkeypatch.setenv("SECRET_KEY", "prod-key")
    prod = internal_auth.token()
    monkeypatch.setenv("SECRET_KEY", "demo-key")
    assert internal_auth.token() != prod


def test_an_explicit_token_overrides_the_derivation(monkeypatch):
    """So the internal token can be rotated without rotating SECRET_KEY."""
    monkeypatch.setenv("SECRET_KEY", "a-shared-secret-key")
    monkeypatch.setenv("GDX_INTERNAL_TOKEN", "explicit-override")
    assert internal_auth.token() == "explicit-override"
    assert internal_auth.rejection_reason("explicit-override") is None


def test_derivation_does_not_turn_dev_into_an_enforcing_environment(monkeypatch):
    """SECRET_KEY has a compose default, so if `enforced()` keyed on the DERIVED
    token every dev box would silently start enforcing. It keys on the explicit
    one instead — a decision, not a surprise."""
    monkeypatch.setenv("GDX_ENV", "dev")
    monkeypatch.setenv("SECRET_KEY", "dev_secret_key_change_in_production")
    assert internal_auth.token()          # a token IS available…
    assert internal_auth.enforced() is False   # …but dev still does not enforce


def test_still_fails_closed_when_there_is_nothing_to_derive_from(monkeypatch):
    """No explicit token and no SECRET_KEY: refuse. The derivation is a delivery
    mechanism, not a way to avoid ever failing closed."""
    monkeypatch.setenv("GDX_ENV", "production")
    assert internal_auth.token() == ""
    assert internal_auth.rejection_reason("") is not None
    assert internal_auth.rejection_reason("anything") is not None


def test_both_sides_derive_the_same_token(monkeypatch):
    """The core app's caller side and plugin-host's gate must agree with no
    coordination beyond SECRET_KEY — that agreement IS the fix."""
    from gdx_dispatch.core.plugin_consent import internal_auth_headers

    monkeypatch.setenv("GDX_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "the-one-both-containers-have")
    sent = internal_auth_headers()[internal_auth.INTERNAL_TOKEN_HEADER]
    assert internal_auth.rejection_reason(sent) is None


def test_a_refused_restart_is_not_reported_as_requested(monkeypatch):
    """Sibling of the plugin_events silent-success, found by the #596 sweep.

    `httpx.post` does not raise on 401, so the old `except Exception` never saw
    a refusal: the catalog cache was dropped and the owner got
    `{"status": "restart requested"}` for work that did not happen. The UI's
    confirmation (poll /api/plugins until plugin-host returns) passes trivially,
    because plugin-host never went down. An owner installs a plugin, is told it
    worked, and it never loads.
    """
    from unittest.mock import MagicMock, patch

    from fastapi import HTTPException

    from gdx_dispatch.routers import admin_plugins

    db, request, user = MagicMock(), MagicMock(), {"user_id": "u1", "role": "owner"}
    with (
        patch.object(admin_plugins, "_audit"),
        patch.object(admin_plugins.httpx, "post", return_value=MagicMock(status_code=401)),
        pytest.raises(HTTPException) as exc,
    ):
        admin_plugins.restart_plugin_host(request=request, user=user, db=db)
    assert exc.value.status_code == 502
    assert "not live" in exc.value.detail

    # …and a genuine restart still reports success.
    with (
        patch.object(admin_plugins, "_audit"),
        patch.object(admin_plugins.httpx, "post", return_value=MagicMock(status_code=200)),
    ):
        assert admin_plugins.restart_plugin_host(
            request=request, user=user, db=db
        ) == {"status": "restart requested", "delivered": True}


def test_a_refused_credentials_call_is_a_502_not_a_relayed_401(monkeypatch):
    """Relaying plugin-host's 401 to the browser mislabels a server-to-server
    failure as the OPERATOR's session expiring.

    useApi.js treats 401 as an expired session: it burns a refreshAccessToken()
    and RE-SENDS the request — replaying a credential POST — and
    BrowserStream's loadCredsStatus() swallows it into credsSaved=false, telling
    the owner no sign-in is remembered when the app merely could not ask.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi import HTTPException

    from gdx_dispatch.routers import browser_proxy

    for status in (401, 403):
        client = MagicMock()
        client.request = AsyncMock(return_value=MagicMock(status_code=status, text="nope"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        with (
            patch.object(browser_proxy.httpx, "AsyncClient", return_value=cm),
            pytest.raises(HTTPException) as caught,
        ):
            asyncio.run(browser_proxy._creds_call("GET", params={"key": "n8n"}))
        assert caught.value.status_code == 502, status
        # Specifically NOT 401: that is the status useApi.js treats as the
        # operator's own session expiring.
        assert caught.value.status_code not in (401, 403)


def test_the_fingerprint_identifies_the_token_without_revealing_it(monkeypatch):
    """Two containers must derive the SAME token and nothing checks that they do.
    The fingerprint at least makes a divergence a one-second comparison."""
    monkeypatch.setenv("SECRET_KEY", "shared")
    fp = internal_auth.fingerprint()
    assert fp and len(fp) == 12
    assert internal_auth.fingerprint() == fp            # stable
    assert fp not in internal_auth.token()              # not a slice of the token
    assert "shared" not in fp                           # nor of the seed

    monkeypatch.setenv("SECRET_KEY", "different")
    assert internal_auth.fingerprint() != fp            # divergence is visible

    monkeypatch.delenv("SECRET_KEY")
    assert internal_auth.fingerprint() == ""            # nothing to identify


def test_log_identity_names_the_source_and_never_raises(monkeypatch, caplog):
    import logging as _logging

    log = _logging.getLogger("test.internal_auth.identity")
    monkeypatch.setenv("SECRET_KEY", "shared")
    with caplog.at_level("INFO"):
        internal_auth.log_identity(log, "plugin-host")
    assert "source=derived" in caplog.text
    assert "plugin-host" in caplog.text

    caplog.clear()
    monkeypatch.setenv("GDX_INTERNAL_TOKEN", "explicit")
    with caplog.at_level("INFO"):
        internal_auth.log_identity(log, "app")
    assert "source=explicit" in caplog.text

    # Diagnostics must never take a boot down.
    monkeypatch.setattr(internal_auth, "token", lambda: 1 / 0)
    internal_auth.log_identity(log, "app")


def test_a_cycling_plugin_host_tells_the_operator_instead_of_going_blank():
    """The likelier arm, and the one the InvalidStatus branch did not cover.

    The client socket is accepted BEFORE the upstream dial, so a connection
    refused / timeout / plugin-host-restarting closes the operator's socket with
    no reason at all: blank panel. That is the same dead end this change fixes
    for the refusal case — and restarting plugin-host, which this PR just made
    fail loudly, is exactly when it is unreachable.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from gdx_dispatch.routers import browser_proxy

    for exc in (httpx.ConnectError("refused"), TimeoutError(), OSError("gone")):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        ws.accept = AsyncMock()
        with (
            patch.object(browser_proxy, "_decode_ticket",
                         return_value={"k": "n8n", "u": "https://example.com", "sid": "s"}),
            patch.object(browser_proxy, "host_allowed", return_value=True),
            patch.object(browser_proxy, "_start_recorder", return_value=None),
            patch.object(browser_proxy.websockets, "connect", side_effect=exc),
        ):
            asyncio.run(browser_proxy.browser_stream_proxy(ws, ticket="t"))
        sent = [c.args[0] for c in ws.send_json.call_args_list]
        assert any(m.get("type") == "error" for m in sent), f"no reason sent for {exc!r}"


def test_an_unreachable_plugin_host_is_undelivered_not_refused(monkeypatch):
    """A connection error is "we do not know", not "it failed".

    plugin-host restarts `unless-stopped`, so a host that is already cycling
    comes back WITH the pending changes applied. 502-ing that would swap a false
    success for a false failure. It reports `delivered: false` and lets the UI's
    existing poll — the real verification — decide.
    """
    from unittest.mock import MagicMock, patch

    import httpx

    from gdx_dispatch.routers import admin_plugins

    db, request, user = MagicMock(), MagicMock(), {"user_id": "u1", "role": "owner"}
    for exc in (httpx.ConnectError("down"), httpx.ReadTimeout("hung")):
        with (
            patch.object(admin_plugins, "_audit"),
            patch.object(admin_plugins.httpx, "post", side_effect=exc),
        ):
            out = admin_plugins.restart_plugin_host(request=request, user=user, db=db)
        assert out["delivered"] is False, exc

    # A host that ANSWERED and accepted is delivered…
    with (
        patch.object(admin_plugins, "_audit"),
        patch.object(admin_plugins.httpx, "post", return_value=MagicMock(status_code=200)),
    ):
        assert admin_plugins.restart_plugin_host(
            request=request, user=user, db=db
        )["delivered"] is True

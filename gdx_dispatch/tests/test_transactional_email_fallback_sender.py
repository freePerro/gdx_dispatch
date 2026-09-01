"""#466 — sends by a person with no Outlook connection go through the
tenant's designated sender instead of failing.

Prod shape that motivated this: one connected mailbox, zero SMTP rows; a
portal invite only emailed when the ONE connected user clicked Send —
everyone else got no_email_provider_connected and hand-delivered the link.
The office already chose a mailbox for automated mail (Settings →
Automation email → "Send as"); a person who has never connected their own
now sends through it. The acting user stays the initiator on the audit row;
the email.sent event names the mailbox.

Scope pinned here too: the rung is OPT-IN per call site (portal invites
and sign-in links — fixed content); a caller that does not ask for it
(estimates, invoices: caller-written subject/body/recipient, no permission
gate on /send) fails exactly as before; an EXPIRED personal connection does
not fall back (the person should reconnect); system sends (user_id=None)
are untouched.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.core import transactional_email as te
from gdx_dispatch.models.tenant_models import AppSettings, OutboundEmail

TENANT = "11111111-1111-1111-1111-111111111111"
CALLER = UUID("22222222-2222-2222-2222-222222222222")
DESIGNATED = UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture()
def db(tenant_db):
    return tenant_db


def _designate(db, user_id):
    db.add(AppSettings(automation_sender_user_id=str(user_id) if user_id else None))
    db.commit()


def _outlook(monkeypatch, *, connected: set[UUID], expired: set[UUID] = frozenset()):
    """Stub the Graph rung: records every user_id it was asked to send as."""
    attempts: list[UUID] = []

    def fake(**kw):
        uid = kw["user_id"]
        attempts.append(uid)
        if uid in connected:
            return True, None
        if uid in expired:
            return False, "outlook_reconnect_required"
        return False, "outlook_not_connected"

    monkeypatch.setattr(te, "_try_outlook_graph", fake)
    monkeypatch.setattr(te, "_try_smtp", lambda **kw: (False, "smtp_not_configured"))
    return attempts


def _events(monkeypatch):
    import gdx_dispatch.core.webhooks.emit as emit_mod
    seen = []
    monkeypatch.setattr(
        emit_mod, "emit_domain_event",
        lambda db, event_type, entity_id, data, *, tenant_id, suppress=False:
            seen.append((event_type, data)) or 0,
    )
    return seen


def _send(db, *, user_id, opt_in=True):
    return te.send_transactional_email(
        tenant_db=db, tenant_id=TENANT, user_id=str(user_id) if user_id else None,
        to_email="cust@example.com", to_name="Cust", subject="Your portal invite",
        html_body="<p>hi</p>", kind="magic_link", entity_type="portal_magic_link",
        entity_id=str(uuid4()), designated_sender_fallback=opt_in,
    )


def _row(db):
    return db.execute(select(OutboundEmail).order_by(OutboundEmail.created_at.desc())).scalars().first()


def test_unconnected_staff_send_goes_through_the_designated_sender(db, monkeypatch):
    _designate(db, DESIGNATED)
    attempts = _outlook(monkeypatch, connected={DESIGNATED})
    seen = _events(monkeypatch)

    sent, provider, skip = _send(db, user_id=CALLER)

    assert (sent, provider, skip) == (True, "outlook_graph", None)
    assert attempts == [CALLER, DESIGNATED]  # own mailbox first, then the office's
    row = _row(db)
    assert row.status == "sent" and row.initiator_ref == str(CALLER)  # WHO did it
    assert seen[-1][0] == "email.sent"
    assert seen[-1][1]["sender_user_id"] == str(DESIGNATED)             # WHICH mailbox
    assert seen[-1][1]["initiator_ref"] == str(CALLER)


def test_connected_staff_never_touch_the_designated_sender(db, monkeypatch):
    _designate(db, DESIGNATED)
    attempts = _outlook(monkeypatch, connected={CALLER, DESIGNATED})
    seen = _events(monkeypatch)
    sent, provider, _ = _send(db, user_id=CALLER)
    assert (sent, provider) == (True, "outlook_graph")
    assert attempts == [CALLER]
    assert seen[-1][1]["sender_user_id"] is None


def test_designated_is_the_caller_means_one_attempt(db, monkeypatch):
    _designate(db, CALLER)
    attempts = _outlook(monkeypatch, connected=set())
    sent, provider, skip = _send(db, user_id=CALLER)
    assert (sent, provider, skip) == (False, None, "no_email_provider_connected")
    assert attempts == [CALLER]


def test_no_designated_sender_fails_exactly_as_before(db, monkeypatch):
    _designate(db, None)
    attempts = _outlook(monkeypatch, connected=set())
    sent, provider, skip = _send(db, user_id=CALLER)
    assert (sent, provider, skip) == (False, None, "no_email_provider_connected")
    assert attempts == [CALLER]
    assert _row(db).skip_reason == "no_email_provider_connected"


def test_designated_sender_that_cannot_send_surfaces_its_own_reason(db, monkeypatch):
    """The office's mailbox exists but its token lapsed: say THAT, not
    'connect an account' — the person cannot fix it by connecting."""
    _designate(db, DESIGNATED)
    _outlook(monkeypatch, connected=set(), expired={DESIGNATED})
    sent, provider, skip = _send(db, user_id=CALLER)
    assert sent is False
    assert skip == "outlook_reconnect_required"


def test_expired_personal_connection_does_not_fall_back(db, monkeypatch):
    """A person whose own Outlook stopped working must find out, not have
    their mail silently rerouted through someone else's mailbox."""
    _designate(db, DESIGNATED)
    attempts = _outlook(monkeypatch, connected={DESIGNATED}, expired={CALLER})
    sent, provider, skip = _send(db, user_id=CALLER)
    assert sent is False and skip == "outlook_reconnect_required"
    assert attempts == [CALLER]


def test_system_sends_are_out_of_scope(db, monkeypatch):
    """user_id=None (reminder tasks, mobile receipts) never tried Outlook
    and still does not — routing those through a personal mailbox is a
    product decision, not a side effect of #466."""
    _designate(db, DESIGNATED)
    attempts = _outlook(monkeypatch, connected={DESIGNATED})
    sent, provider, skip = _send(db, user_id=None)
    assert (sent, provider, skip) == (False, None, "no_email_provider_connected")
    assert attempts == []


def test_portal_link_base_prefers_the_configured_public_host(monkeypatch):
    from types import SimpleNamespace

    from gdx_dispatch.routers.portal import _portal_link_base

    req = SimpleNamespace(base_url="http://127.0.0.1:8002/")
    monkeypatch.delenv("CUSTOMER_PORTAL_BASE_URL", raising=False)
    monkeypatch.delenv("GDX_PUBLIC_BASE_URL", raising=False)
    assert _portal_link_base(req) == "http://127.0.0.1:8002"
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com/")
    assert _portal_link_base(req) == "https://gdx.example.com"
    monkeypatch.setenv("CUSTOMER_PORTAL_BASE_URL", "https://portal.example.com")
    assert _portal_link_base(req) == "https://portal.example.com"


def test_callers_that_do_not_opt_in_fail_exactly_as_before(db, monkeypatch):
    """Estimate/invoice sends carry caller-written content and no permission
    gate; "no mailbox" is the only thing keeping a technician from emailing
    arbitrary text from the owner's account. They do not get the rung until
    that is decided (permission gates + sender column + Reply-To)."""
    _designate(db, DESIGNATED)
    attempts = _outlook(monkeypatch, connected={DESIGNATED})
    sent, provider, skip = _send(db, user_id=CALLER, opt_in=False)
    assert (sent, provider, skip) == (False, None, "no_email_provider_connected")
    assert attempts == [CALLER]


def test_portal_call_sites_opt_in():
    """The two portal senders (invite, self-service sign-in link) are the
    only call sites that ask for the rung."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[1]
    flagged = []
    for py in src.rglob("*.py"):
        if "tests" in py.parts:
            continue
        text = py.read_text(errors="replace")
        if "designated_sender_fallback=True" in text:
            flagged.append(py.name)
    assert sorted(flagged) == ["portal.py"], flagged
    portal = (src / "routers" / "portal.py").read_text()
    assert len(re.findall(r"designated_sender_fallback=True", portal)) == \
        len(re.findall(r"send_transactional_email\(", portal))

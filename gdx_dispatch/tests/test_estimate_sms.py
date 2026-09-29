"""Text an estimate (core/estimate_sms.py over core/link_sms.py + the office
and tech routes).

The engine's failure handling (no re-POST, pending row, unconfirmed block,
commit retry, unreadable 2xx, id collision) is pinned by test_invoice_sms.py,
which runs through the same core/link_sms.send_link. This suite pins what is
estimate-specific:

  1. The text carries the /proposals/{token} approval link, and that link is
     live afterwards — the public page 404s until sent_at is set.
  2. Success does what the email send does: status sent, sent_at, expiry, the
     estimate.sent event — plus sent_via='sms' and an audit row.
  3. A definite failure leaves a never-sent estimate unsent; an unconfirmed
     one leaves its link live but sent_via unset.
  4. Accepted / declined estimates, opted-out customers and a missing public
     URL are refused before Phone.com is called.
  5. A tech can text only a quote on their own job — via the shared
     ownership gate, which matches jobs.assigned_to = technician id.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Job
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient
from gdx_dispatch.modules.phone_com.models import PhoneComMessage
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers.estimates import SendEstimateSmsIn, estimate_sms_preview, send_estimate_sms
from gdx_dispatch.routers.mobile_quoting import (
    MobileSendQuoteSmsIn,
    mobile_quote_sms_preview,
    mobile_send_quote_sms,
)

TENANT = str(uuid4())
VOIP = 4242
SEND_URL = f"{BASE_URL}/accounts/{VOIP}/messages"
FROM = "+16125550100"
CUSTOMER_E164 = "+16125550142"
BASE = "https://gdx.example.com"
OFFICE = {"user_id": str(uuid4()), "tenant_id": TENANT, "role": "admin"}
TECH_USER = str(uuid4())
TECH = {"user_id": TECH_USER, "sub": TECH_USER, "tenant_id": TENANT, "role": "technician"}
EVENTS: list[tuple[str, dict]] = []


class _State:
    tenant = {"id": TENANT}


class _Req:
    state = _State()
    headers: dict = {}
    client = None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", BASE)

    def _client(tenant_id, control_db, tenant_db):
        c = PhoneComClient(token="t", voip_id=VOIP)
        c._backoff_seconds = lambda attempt: 0
        return c

    monkeypatch.setattr("gdx_dispatch.modules.phone_com.router._get_phone_com_client", _client)
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: True)
    EVENTS.clear()
    monkeypatch.setattr(
        "gdx_dispatch.core.webhooks.emit.emit_domain_event",
        lambda db, name, entity_id, payload, **kw: EVENTS.append((name, payload)),
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [Job.__table__, Customer.__table__, Estimate.__table__]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    s = Session()
    s.add(AppSettings(company_name="Acme Doors", phone_com_default_caller_id=FROM))
    s.execute(TenantBase.metadata.tables["technicians"].insert().values(
        id="tech-mine", user_id=TECH_USER, company_id=TENANT, name="Mine",
    ))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed(db, *, status="draft", sent_at=None, phone="(612) 555-0142", opt_out=False,
          assigned_to="tech-mine", with_job=True) -> Estimate:
    c = Customer(id=uuid4(), name="Pat Payer", phone=phone, sms_opt_out=opt_out, company_id=TENANT)
    db.add(c)
    job_id = None
    if with_job:
        j = Job(id=uuid4(), customer_id=c.id, title="New door", lifecycle_stage="estimate",
                dispatch_status="assigned", billing_status="unbilled", assigned_to=assigned_to,
                company_id=TENANT)
        db.add(j)
        job_id = j.id
    est = Estimate(
        id=uuid4(), job_id=job_id, customer_id=c.id, estimate_number=f"E-{uuid4().hex[:6]}",
        label="Door", proposal_mode=False, total=Decimal("1850"), status=status, sent_at=sent_at,
        public_token=uuid4().hex, company_id=TENANT,
    )
    db.add(est)
    db.commit()
    db.refresh(est)
    return est


def _send(db, est, **payload):
    return send_estimate_sms(estimate_id=est.id, request=_Req(), payload=SendEstimateSmsIn(**payload),
                             _=OFFICE, db=db)


def _code(exc) -> str:
    return exc.value.detail["code"]


def _audit(db, action):
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _link_is_live(db, est) -> bool:
    from gdx_dispatch.modules.proposals.router import _get_public_estimate_or_404

    try:
        _get_public_estimate_or_404(est.public_token, db)
        return True
    except HTTPException:
        return False


# ── office: success ───────────────────────────────────────────────────────


@respx.mock
def test_send_texts_the_approval_link_and_marks_it_sent(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-1"}))
    est = _seed(db)
    assert not _link_is_live(db, est)  # an unsent estimate's link is dead

    out = _send(db, est)

    sent = json.loads(route.calls.last.request.content)
    link = f"{BASE}/proposals/{est.public_token}"
    assert sent["to"] == CUSTOMER_E164 and sent["from"] == FROM
    assert sent["text"] == f"Acme Doors: Your estimate #{est.estimate_number} is ready. Review and approve: {link}"
    assert out["sms_sent"] is True
    db.expire_all()
    fresh = db.get(Estimate, est.id)
    assert (fresh.status, fresh.sent_via) == ("sent", "sms")
    assert fresh.sent_at is not None and fresh.valid_until is not None
    assert _link_is_live(db, fresh)
    assert [name for name, _ in EVENTS] == ["estimate.sent"]
    [row] = _audit(db, "estimate_sent_sms")
    assert row.entity_type == "estimate" and row.entity_id == str(est.id)
    [msg] = db.execute(select(PhoneComMessage)).scalars().all()
    assert msg.customer_id == est.customer_id and msg.sent_by_user_id == UUID(OFFICE["user_id"])


@respx.mock
def test_preview_is_exactly_what_gets_sent(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-2"}))
    est = _seed(db)
    preview = estimate_sms_preview(estimate_id=est.id, payload=None, _=OFFICE, db=db)
    assert preview["blocked"] is None and preview["to"] == CUSTOMER_E164
    _send(db, est)
    assert json.loads(route.calls.last.request.content)["text"] == preview["body"]


@pytest.mark.parametrize("status", ["sent", "rejected", "expired"])
@respx.mock
def test_resending_restamps_and_returns_to_sent(db, status):
    """A re-send (including a bounced 'rejected' email or an expired
    estimate) is allowed, as the email send allows it."""
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-3"}))
    earlier = datetime.now(UTC) - timedelta(days=90)
    est = _seed(db, status=status, sent_at=earlier)
    _send(db, est)
    db.expire_all()
    fresh = db.get(Estimate, est.id)
    assert fresh.status == "sent" and fresh.sent_via == "sms"
    sent_at = fresh.sent_at if fresh.sent_at.tzinfo else fresh.sent_at.replace(tzinfo=UTC)
    assert sent_at > earlier


# ── office: failures ─────────────────────────────────────────────────────


@respx.mock
def test_definite_failure_leaves_a_never_sent_estimate_unsent(db):
    respx.post(SEND_URL).mock(return_value=httpx.Response(400, json={"error": "bad number"}))
    est = _seed(db)
    with pytest.raises(HTTPException) as exc:
        _send(db, est)
    assert exc.value.status_code == 502 and _code(exc) == "sms_provider_error"
    db.expire_all()
    fresh = db.get(Estimate, est.id)
    assert (fresh.status, fresh.sent_at, fresh.sent_via) == ("draft", None, None)
    assert not _link_is_live(db, fresh)
    assert EVENTS == [] and len(_audit(db, "estimate_sms_failed")) == 1


@respx.mock
def test_unconfirmed_outcome_leaves_the_link_live_but_unstamped(db):
    route = respx.post(SEND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    est = _seed(db)
    with pytest.raises(HTTPException) as exc:
        _send(db, est)
    assert exc.value.status_code == 504 and _code(exc) == "sms_outcome_unknown"
    assert route.call_count == 1
    db.expire_all()
    fresh = db.get(Estimate, est.id)
    assert fresh.sent_at is not None and fresh.status == "sent" and fresh.sent_via is None
    assert _link_is_live(db, fresh)  # if it arrived, the customer can open it
    assert EVENTS == []  # delivery not confirmed: no estimate.sent
    assert len(_audit(db, "estimate_sms_unconfirmed")) == 1


@pytest.mark.parametrize("status", ["expired", "draft"], ids=["expired", "reopened"])
@respx.mock
def test_unconfirmed_resend_leaves_a_link_the_customer_can_accept(db, status):
    """Previously-sent estimates (expired, or reopened: status draft with the
    old sent_at kept): after a timeout the link must be one the customer can
    ACT on — the public accept 409s unless status is sent/rejected — with a
    fresh expiry window. (Fourth audit's falsifier.)"""
    respx.post(SEND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    old = datetime.now(UTC) - timedelta(days=90)
    est = _seed(db, status=status, sent_at=old)
    est.valid_until = old + timedelta(days=30)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _send(db, est)
    assert _code(exc) == "sms_outcome_unknown"
    db.expire_all()
    fresh = db.get(Estimate, est.id)
    assert fresh.status in ("sent", "rejected")  # what the public accept requires
    until = fresh.valid_until if fresh.valid_until.tzinfo else fresh.valid_until.replace(tzinfo=UTC)
    assert until > datetime.now(UTC)
    assert fresh.sent_via is None and _link_is_live(db, fresh)


def _customer_accepts_elsewhere(db, est_id):
    """The customer accepts from their own request — a SEPARATE session, as in
    production. (A same-session commit would expire the sender's cached
    estimate and hide a stale-read bug; the sixth audit caught exactly that.)"""
    from sqlalchemy import update

    other = sessionmaker(bind=db.get_bind())()
    try:
        other.execute(update(Estimate).where(Estimate.id == est_id).values(
            status="accepted", accepted_at=datetime.now(UTC)))
        other.commit()
    finally:
        other.close()


@pytest.mark.parametrize("start", ["sent", "rejected"])
@pytest.mark.parametrize("outcome", ["timeout", "delivered"])
@respx.mock
def test_customer_decision_during_the_send_is_never_undone(db, outcome, start):
    """The customer accepts (standing next to the tech, link in hand) while
    Phone.com is still answering. Neither the 504 path's re-stage nor the
    success path's stamp may undo it: status stays accepted, sent_at is not
    restamped, and no estimate.sent fires. 'rejected' (a bounced email) is the
    start where a blind stamp would actually overwrite the column."""
    earlier = datetime.now(UTC) - timedelta(days=2)
    est = _seed(db, status=start, sent_at=earlier)

    def customer_accepts_then(request):
        _customer_accepts_elsewhere(db, est.id)
        if outcome == "timeout":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"id": "pc-race"})

    respx.post(SEND_URL).mock(side_effect=customer_accepts_then)
    if outcome == "timeout":
        with pytest.raises(HTTPException) as exc:
            _send(db, est)
        assert _code(exc) == "sms_outcome_unknown"
    else:
        assert _send(db, est)["sms_sent"] is True
    db.expire_all()
    fresh = db.get(Estimate, est.id)
    assert fresh.status == "accepted" and fresh.accepted_at is not None
    sent_at = fresh.sent_at if fresh.sent_at.tzinfo else fresh.sent_at.replace(tzinfo=UTC)
    assert sent_at == earlier
    assert EVENTS == []


@pytest.mark.parametrize(
    ("seed", "env_off", "code"),
    [
        ({"status": "accepted"}, None, "estimate_finalized"),
        ({"status": "declined"}, None, "estimate_finalized"),
        ({"opt_out": True}, None, "sms_opt_out"),
        ({}, "GDX_PUBLIC_BASE_URL", "link_unavailable"),
    ],
)
@respx.mock
def test_refusals_never_reach_phone_com(db, monkeypatch, seed, env_off, code):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    if env_off:
        monkeypatch.delenv(env_off)
    est = _seed(db, **seed)
    with pytest.raises(HTTPException) as exc:
        _send(db, est)
    assert exc.value.status_code == 409 and _code(exc) == code
    assert not route.called
    assert estimate_sms_preview(estimate_id=est.id, payload=None, _=OFFICE, db=db)["blocked"]["code"] == code


@respx.mock
def test_no_phone_on_file_is_422_and_to_override_reaches_them(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-4"}))
    est = _seed(db, phone=None)
    with pytest.raises(HTTPException) as exc:
        _send(db, est)
    assert exc.value.status_code == 422 and _code(exc) == "no_valid_phone"
    _send(db, est, to="612-555-0199")
    assert json.loads(route.calls.last.request.content)["to"] == "+16125550199"


# ── tech (mobile) ────────────────────────────────────────────────────────


def _tech_send(db, est):
    return mobile_send_quote_sms(estimate_id=str(est.id), request=_Req(),
                                 payload=MobileSendQuoteSmsIn(), current_user=TECH, db=db)


@respx.mock
def test_tech_texts_a_quote_on_their_own_job(db):
    """jobs.assigned_to holds the TECHNICIAN id, and there is no
    job_assignments row — only the shared gate matches this (the module's old
    _job_belongs_to_tech compares assigned_to to the user id)."""
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-5"}))
    est = _seed(db)
    res = _tech_send(db, est)
    assert res.status_code == 200, res.body
    assert json.loads(res.body)["sms_sent"] is True
    [row] = _audit(db, "mobile_estimate_sent_sms")
    assert row.entity_id == str(est.id)
    preview = mobile_quote_sms_preview(estimate_id=str(est.id), request=_Req(), payload=None,
                                       current_user=TECH, db=db)
    assert preview.status_code == 200


@pytest.mark.parametrize(
    "seed", [{"assigned_to": "tech-other"}, {"with_job": False}], ids=["other-techs-job", "no-job"],
)
@respx.mock
def test_tech_cannot_text_a_quote_that_is_not_theirs(db, seed):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    est = _seed(db, **seed)
    assert _tech_send(db, est).status_code == 403
    assert not route.called


@respx.mock
def test_tech_refused_when_texting_is_off(db, monkeypatch):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: False)
    est = _seed(db)
    assert _tech_send(db, est).status_code == 403
    assert not route.called


# ── the email send shares the estimate.sent emitter ───────────────────────


def test_email_send_still_emits_estimate_sent(db, monkeypatch):
    """_emit_estimate_sent was extracted from send_estimate so SMS and email
    share one emitter; the email path must still fire it on delivery."""
    from types import SimpleNamespace

    from gdx_dispatch.routers import estimates as est_router

    est = _seed(db)
    cust = db.get(Customer, est.customer_id)
    monkeypatch.setattr(est_router, "_prepare_estimate_email", lambda *a, **k: {
        "customer": cust, "subject": "s", "html": "<p>h</p>",
        "recipient": SimpleNamespace(ok=True, email="pat@example.com", to_name="Pat",
                                     source="customer", contact_id=None),
    })
    monkeypatch.setattr(est_router, "_estimate_pdf_bytes", lambda *a, **k: b"%PDF-1.4")
    monkeypatch.setattr(est_router, "_estimate_extra_attachment_payloads", lambda *a, **k: ([], []))
    monkeypatch.setattr("gdx_dispatch.core.transactional_email.recently_sent", lambda *a, **k: False)
    monkeypatch.setattr("gdx_dispatch.core.transactional_email.send_transactional_email",
                        lambda **k: (True, "smtp", None))

    out = est_router.send_estimate(estimate_id=est.id, payload=None, _=OFFICE, db=db)

    assert out["email_sent"] is True
    assert [name for name, _ in EVENTS] == ["estimate.sent"]
    assert EVENTS[0][1]["estimate_id"] == str(est.id)


def test_email_send_never_undoes_a_decision_made_while_sending(db, monkeypatch):
    """Same shape as the SMS race, on the email path: the finalized check ran
    before a multi-second Outlook/SMTP send, then status='sent' was stamped
    unconditionally."""
    from types import SimpleNamespace

    from gdx_dispatch.routers import estimates as est_router

    est = _seed(db, status="rejected", sent_at=datetime.now(UTC) - timedelta(days=2))
    cust = db.get(Customer, est.customer_id)
    monkeypatch.setattr(est_router, "_prepare_estimate_email", lambda *a, **k: {
        "customer": cust, "subject": "s", "html": "<p>h</p>",
        "recipient": SimpleNamespace(ok=True, email="pat@example.com", to_name="Pat",
                                     source="customer", contact_id=None),
    })
    monkeypatch.setattr(est_router, "_estimate_pdf_bytes", lambda *a, **k: b"%PDF-1.4")
    monkeypatch.setattr(est_router, "_estimate_extra_attachment_payloads", lambda *a, **k: ([], []))
    monkeypatch.setattr("gdx_dispatch.core.transactional_email.recently_sent", lambda *a, **k: False)

    def deliver_while_customer_accepts(**k):
        _customer_accepts_elsewhere(db, est.id)
        return True, "smtp", None

    monkeypatch.setattr("gdx_dispatch.core.transactional_email.send_transactional_email",
                        deliver_while_customer_accepts)
    out = est_router.send_estimate(estimate_id=est.id, payload=None, _=OFFICE, db=db)
    assert out["email_sent"] is True
    db.expire_all()
    fresh = db.get(Estimate, est.id)
    assert fresh.status == "accepted" and fresh.sent_via == "email"
    assert EVENTS == []


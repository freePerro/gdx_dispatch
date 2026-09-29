"""Text an invoice (core/invoice_sms.py + the desktop and mobile routes).

The Phone.com HTTP call is intercepted with respx, but the real PhoneComClient
builds and sends it — so these assert what actually goes over the wire, not
just which arguments a mock received.

What they defend, worst first:

  1. A text that never left never marks the invoice sent (provider failure
     rolls the draft→sent transition back).
  2. Opted-out customers, void / fully-paid / unverified invoices and dead pay
     links are refused before Phone.com is called.
  3. A tech can only text invoices on their own, office-verified jobs.
  4. A success leaves the full trail: status, sent_at, sent_via='sms', the
     outbound message on the customer's SMS thread, and an audit row.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
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
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Invoice, InvoiceLine, Job, Payment
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComAPIError, PhoneComClient
from gdx_dispatch.modules.phone_com.models import PhoneComMessage
from gdx_dispatch.routers.invoices import SendInvoiceSmsIn, invoice_sms_preview, send_invoice_sms
from gdx_dispatch.routers.mobile_invoicing import (
    MobileSendSmsIn,
    mobile_invoice_sms_preview,
    mobile_send_invoice_sms,
)

TENANT = str(uuid4())
VOIP = 4242
SEND_URL = f"{BASE_URL}/accounts/{VOIP}/messages"
FROM = "+16125550100"
CUSTOMER_PHONE = "(612) 555-0142"
CUSTOMER_E164 = "+16125550142"
OFFICE = {"user_id": str(uuid4()), "tenant_id": TENANT, "role": "admin"}
TECH_USER = str(uuid4())
TECH = {"user_id": TECH_USER, "sub": TECH_USER, "tenant_id": TENANT, "role": "technician"}


CLIENT_TENANTS: list[str] = []


class _State:
    tenant = {"id": TENANT}


class _Req:
    state = _State()
    headers: dict = {}
    client = None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    # Real client, fake transport: the token/voip lookup is the only thing
    # replaced, because it reads the encrypted control-plane token.
    def _client(tenant_id, control_db, tenant_db):
        CLIENT_TENANTS.append(str(tenant_id))
        c = PhoneComClient(token="t", voip_id=VOIP)
        c._backoff_seconds = lambda attempt: 0  # a retry, if any, must not slow the suite
        return c

    CLIENT_TENANTS.clear()
    monkeypatch.setattr("gdx_dispatch.modules.phone_com.router._get_phone_com_client", _client)
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: True)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [Job.__table__, Customer.__table__, Invoice.__table__, InvoiceLine.__table__, Payment.__table__]:
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


def _seed(db, *, status="draft", verified=True, balance=250.0, phone=CUSTOMER_PHONE,
          opt_out=False, assigned_to="tech-mine") -> Invoice:
    c = Customer(id=uuid4(), name="Pat Payer", phone=phone, sms_opt_out=opt_out, company_id=TENANT)
    db.add(c)
    j = Job(id=uuid4(), customer_id=c.id, title="Spring", lifecycle_stage="completed",
            dispatch_status="assigned", billing_status="unbilled", assigned_to=assigned_to,
            company_id=TENANT)
    db.add(j)
    inv = Invoice(
        id=uuid4(), job_id=j.id, customer_id=c.id, invoice_number="INV-1001",
        billing_type="standard", sequence_number=1,
        subtotal=Decimal(str(balance)), tax_amount=0, total=Decimal(str(balance)),
        balance_due=Decimal(str(balance)), status=status,
        invoice_date=date.today(), due_date=date.today(), company_id=TENANT,
        verified_at=datetime.now(UTC) if verified else None, created_at=datetime.now(UTC),
        public_token=uuid4().hex,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _send(db, inv, **payload):
    return send_invoice_sms(invoice_id=inv.id, request=_Req(), payload=SendInvoiceSmsIn(**payload),
                            _=OFFICE, db=db)


def _code(exc: pytest.ExceptionInfo) -> str:
    return exc.value.detail["code"]


def _messages(db):
    return db.execute(select(PhoneComMessage)).scalars().all()


def _audit(db, action):
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


# ── desktop: success ──────────────────────────────────────────────────────


@respx.mock
def test_send_texts_the_pay_link_and_leaves_the_full_trail(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-1", "status": "sent"}))
    inv = _seed(db)

    out = _send(db, inv)

    sent = json.loads(route.calls.last.request.content)
    db.refresh(inv)
    pay_url = f"https://gdx.example.com/pay/{inv.public_token}"
    assert sent == {
        "from": FROM,
        "to": CUSTOMER_E164,
        "text": f"Acme Doors: Invoice #INV-1001 — $250.00 due. View and pay: {pay_url}",
    }
    assert out["sms_sent"] is True and out["to"] == CUSTOMER_E164
    assert inv.status == "sent" and inv.sent_via == "sms" and inv.sent_at is not None

    [msg] = _messages(db)
    assert (msg.direction, msg.to_number, msg.customer_id, msg.job_id) == ("out", CUSTOMER_E164, inv.customer_id, inv.job_id)
    assert msg.sent_by_user_id == UUID(OFFICE["user_id"])
    assert pay_url in msg.body

    [row] = _audit(db, "invoice_sent_sms")
    assert row.entity_id == str(inv.id)
    assert CLIENT_TENANTS == [TENANT]  # the id the Phone.com token is keyed by


@respx.mock
def test_edited_body_without_the_link_gets_it_appended(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-2"}))
    inv = _seed(db)
    _send(db, inv, body="Thanks for having us out today!")
    db.refresh(inv)
    text = json.loads(route.calls.last.request.content)["text"]
    assert text == f"Thanks for having us out today!\nhttps://gdx.example.com/pay/{inv.public_token}"


@respx.mock
def test_preview_is_exactly_what_gets_sent(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-3"}))
    inv = _seed(db)
    preview = invoice_sms_preview(invoice_id=inv.id, payload=None, _=OFFICE, db=db)
    assert preview["blocked"] is None and preview["to"] == CUSTOMER_E164

    _send(db, inv)
    assert json.loads(route.calls.last.request.content)["text"] == preview["body"]


@respx.mock
def test_to_override_reaches_a_customer_with_no_phone_on_file(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-4"}))
    inv = _seed(db, phone=None)
    with pytest.raises(HTTPException) as exc:
        _send(db, inv)
    assert exc.value.status_code == 422 and _code(exc) == "no_valid_phone"
    assert not route.called

    _send(db, inv, to="612-555-0199")
    assert json.loads(route.calls.last.request.content)["to"] == "+16125550199"


@respx.mock
def test_already_sent_invoice_stays_sent(db):
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-5"}))
    inv = _seed(db, status="sent")
    _send(db, inv)
    db.refresh(inv)
    assert inv.status == "sent" and inv.sent_via == "sms"


# ── desktop: failure changes nothing ──────────────────────────────────────


def _still_draft(db, inv):
    db.expire_all()
    fresh = db.get(Invoice, inv.id)
    assert fresh.status == "draft" and fresh.sent_via is None and fresh.sent_at is None
    assert _audit(db, "invoice_sent_sms") == []


@respx.mock
def test_definite_refusal_rolls_back_and_allows_a_retry(db):
    route = respx.post(SEND_URL).mock(side_effect=[
        httpx.Response(400, json={"error": "bad number"}),
        httpx.Response(200, json={"id": "pc-ok"}),
    ])
    inv = _seed(db)
    with pytest.raises(HTTPException) as exc:
        _send(db, inv)
    assert exc.value.status_code == 502 and _code(exc) == "sms_provider_error"
    _still_draft(db, inv)
    [attempt] = _messages(db)
    assert attempt.delivery_status == "failed"
    assert len(_audit(db, "invoice_sms_failed")) == 1

    # A 4xx sent nothing, so the duplicate guard must not block the retry.
    _send(db, inv)
    assert route.call_count == 2


@respx.mock
def test_unreachable_phone_com_is_a_definite_failure(db):
    """Connection refused on every attempt: the text provably never left, so
    the operator is not told it "may have been delivered" and may retry."""
    route = respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("refused"))
    inv = _seed(db)
    with pytest.raises(HTTPException) as exc:
        _send(db, inv)
    assert exc.value.status_code == 502 and _code(exc) == "sms_provider_error"
    _still_draft(db, inv)
    [attempt] = _messages(db)
    assert attempt.delivery_status == "failed"
    calls = route.call_count
    with pytest.raises(HTTPException) as again:
        _send(db, inv)
    assert _code(again) == "sms_provider_error"  # retried, not suppressed
    assert route.call_count > calls


def _age_attempts(db, seconds=61):
    """Backdate every recorded attempt past the duplicate window."""
    from datetime import timedelta
    for m in _messages(db):
        m.sent_at = (m.sent_at or datetime.now(UTC)) - timedelta(seconds=seconds)
    db.commit()


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("read timed out"), httpx.Response(503, json={"error": "busy"})],
    ids=["read-timeout", "5xx"],
)
@respx.mock
def test_ambiguous_failure_posts_once_and_keeps_blocking(db, failure):
    """A read timeout or 5xx may mean the text already went out:
    - exactly one POST (the audit's first probe measured 6 for one timeout);
    - the invoice IS marked sent, so a link that did arrive works (/pay
      refuses drafts), but sent_at/sent_via stay unset — unconfirmed;
    - a retry is refused, and still refused after the 60 s window (the
      re-audit's falsifier: a 61-second-old "unknown" row) until the
      operator explicitly sends anyway."""
    kw = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
    route = respx.post(SEND_URL).mock(**kw)
    inv = _seed(db)
    with pytest.raises(HTTPException) as exc:
        _send(db, inv)
    assert exc.value.status_code == 504 and _code(exc) == "sms_outcome_unknown"
    assert route.call_count == 1
    db.expire_all()
    fresh = db.get(Invoice, inv.id)
    assert fresh.status == "sent" and fresh.sent_at is None and fresh.sent_via is None
    [attempt] = _messages(db)
    assert attempt.delivery_status == "unknown"

    with pytest.raises(HTTPException) as again:
        _send(db, inv)
    assert _code(again) == "duplicate_send_suppressed"

    _age_attempts(db)
    with pytest.raises(HTTPException) as later:
        _send(db, inv)
    assert _code(later) == "prior_attempt_unconfirmed"
    assert route.call_count == 1

    # The status change on the 504 path is on the audit trail (invariant #1).
    [trail] = _audit(db, "invoice_sms_unconfirmed")
    assert trail.entity_id == str(inv.id)

    route.mock(return_value=httpx.Response(200, json={"id": "pc-anyway"}), side_effect=None)
    out = _send(db, inv, resend_unconfirmed=True)
    assert out["sms_sent"] is True and route.call_count == 2

    # The confirmed "send anyway" resolves the old attempt: a later reminder
    # (outside the window) needs no override. (Third audit's falsifier.)
    _age_attempts(db)
    route.mock(return_value=httpx.Response(200, json={"id": "pc-later"}))
    _send(db, inv)
    assert route.call_count == 3


@respx.mock
def test_confirmed_send_blocks_only_inside_the_window(db):
    """A delivered text is not "unconfirmed": after the window a deliberate
    re-send (a reminder) goes through without the override."""
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-a"}))
    inv = _seed(db)
    _send(db, inv)
    _age_attempts(db)
    route.mock(return_value=httpx.Response(200, json={"id": "pc-b"}))
    _send(db, inv)
    assert route.call_count == 2


@pytest.mark.parametrize(
    "reply",
    [httpx.Response(202, content=b""), httpx.Response(200, json=[{"id": "x"}])],
    ids=["empty-202", "json-list"],
)
@respx.mock
def test_accepted_but_unreadable_reply_is_recorded_as_sent(db, reply):
    """A 2xx means Phone.com took the text; a body we can't parse must not
    strand a delivered text on a draft with no trail (fifth audit's probe)."""
    respx.post(SEND_URL).mock(return_value=reply)
    inv = _seed(db)
    out = _send(db, inv)
    assert out["sms_sent"] is True
    db.expire_all()
    fresh = db.get(Invoice, inv.id)
    assert fresh.status == "sent" and fresh.sent_via == "sms" and fresh.sent_at is not None
    [msg] = _messages(db)
    assert msg.delivery_status == "queued"
    assert len(_audit(db, "invoice_sent_sms")) == 1


def test_same_second_imported_copy_lifts_the_block(db):
    """Phone.com's sync stores its copy at whole-second resolution; ours has
    microseconds. The delivered copy from the same second must still count as
    later (fifth audit's probe: 12:00:00.400 pending vs 12:00:00 delivered)."""
    from datetime import timedelta

    from gdx_dispatch.core.invoice_sms import _prior_attempt
    pay = "https://gdx.example.com/pay/tok"
    t = datetime.now(UTC).replace(microsecond=400000) - timedelta(minutes=5)
    for mid, status, at in (("pending-1", "unknown", t), ("98765", "delivered", t.replace(microsecond=0))):
        db.add(PhoneComMessage(
            phone_com_message_id=mid, thread_key="k", direction="out", from_number=FROM,
            to_number=CUSTOMER_E164, body=f"Invoice — View and pay: {pay}", sent_at=at,
            delivery_status=status, attachments=[], raw_payload={},
        ))
    db.commit()
    assert _prior_attempt(db, CUSTOMER_E164, pay) is None


@respx.mock
def test_webhook_already_imported_the_real_id(db):
    """Phone.com's webhook can land the message under its real id before we
    record ours; the send must still succeed rather than hit UNIQUE and 500."""
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-dup"}))
    db.add(PhoneComMessage(
        phone_com_message_id="pc-dup", thread_key="k", direction="out", from_number=FROM,
        to_number=CUSTOMER_E164, body="imported", sent_at=datetime.now(UTC),
        delivery_status="delivered", attachments=[], raw_payload={},
    ))
    db.commit()
    inv = _seed(db)
    out = _send(db, inv)
    assert out["sms_sent"] is True
    db.expire_all()
    assert db.get(Invoice, inv.id).sent_via == "sms"


@respx.mock
def test_unencodable_body_is_refused_before_phone_com(db):
    """A lone surrogate would raise UnicodeEncodeError (a ValueError) inside
    httpx before any POST; it must be refused, never recorded as sent."""
    from gdx_dispatch.core import invoice_sms

    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    inv = _seed(db)
    with pytest.raises(HTTPException) as exc:
        invoice_sms.send(db, inv, tenant_id=UUID(TENANT), actor_id=OFFICE["user_id"],
                         body_override="hello \ud83d link", request=_Req())
    assert exc.value.status_code == 422 and _code(exc) == "body_not_encodable"
    assert not route.called and _messages(db) == []


@respx.mock
def test_commit_failure_after_send_is_retried_and_keeps_the_real_id(db, monkeypatch):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-real"}))
    inv = _seed(db)
    real_commit = db.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = pending row, 2 = first post-send record
            raise RuntimeError("db blipped")
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)
    out = _send(db, inv)
    monkeypatch.setattr(db, "commit", real_commit)
    assert out["sms_sent"] is True and route.call_count == 1
    db.expire_all()
    fresh = db.get(Invoice, inv.id)
    assert fresh.status == "sent" and fresh.sent_via == "sms"
    [msg] = _messages(db)
    assert msg.phone_com_message_id == "pc-real" and msg.delivery_status == "queued"


@respx.mock
def test_commit_failing_twice_says_so_and_blocks_a_retry(db, monkeypatch):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-x"}))
    inv = _seed(db)
    real_commit = db.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] in (2, 3):  # both post-send record attempts
            raise RuntimeError("db went away")
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)
    with pytest.raises(HTTPException) as exc:
        _send(db, inv)
    assert exc.value.status_code == 500 and _code(exc) == "sent_but_not_recorded"
    monkeypatch.setattr(db, "commit", real_commit)
    [trail] = _audit(db, "invoice_sms_sent_unrecorded")
    assert trail.details["phone_com_message_id"] == "pc-x"

    with pytest.raises(HTTPException) as again:
        _send(db, inv)
    assert _code(again) == "duplicate_send_suppressed"
    _age_attempts(db)
    with pytest.raises(HTTPException) as later:
        _send(db, inv)
    assert _code(later) == "prior_attempt_unconfirmed"
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("seed", "env_off", "code"),
    [
        ({"opt_out": True}, None, "sms_opt_out"),
        ({"status": "void"}, None, "invoice_void"),
        ({"balance": 0}, None, "no_balance_due"),
        ({}, "STRIPE_SECRET_KEY", "pay_link_unavailable"),
    ],
)
@respx.mock
def test_refusals_never_reach_phone_com(db, monkeypatch, seed, env_off, code):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    if env_off:
        monkeypatch.delenv(env_off)
    inv = _seed(db, **seed)
    with pytest.raises(HTTPException) as exc:
        _send(db, inv)
    assert exc.value.status_code == 409 and _code(exc) == code
    assert not route.called
    preview = invoice_sms_preview(invoice_id=inv.id, payload=None, _=OFFICE, db=db)
    assert preview["blocked"]["code"] == code


@respx.mock
def test_unverified_draft_is_refused(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    inv = _seed(db, verified=False)
    with pytest.raises(HTTPException) as exc:
        _send(db, inv)
    assert exc.value.status_code == 409 and _code(exc) == "awaiting_verification"
    assert not route.called
    preview = invoice_sms_preview(invoice_id=inv.id, payload=None, _=OFFICE, db=db)
    assert preview["blocked"]["code"] == "awaiting_verification"


@respx.mock
def test_second_send_inside_the_window_is_suppressed(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-6"}))
    inv = _seed(db)
    _send(db, inv)
    with pytest.raises(HTTPException) as exc:
        _send(db, inv)
    assert _code(exc) == "duplicate_send_suppressed"
    assert route.call_count == 1


@respx.mock
def test_no_outbound_number_is_refused(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    db.query(AppSettings).first().phone_com_default_caller_id = None
    db.commit()
    inv = _seed(db)
    with pytest.raises(HTTPException) as exc:
        _send(db, inv)
    assert _code(exc) == "no_outbound_number"
    assert not route.called


# ── mobile ────────────────────────────────────────────────────────────────


def _mobile_send(db, inv, **payload):
    return mobile_send_invoice_sms(invoice_id=str(inv.id), request=_Req(),
                                   payload=MobileSendSmsIn(**payload), current_user=TECH, db=db)


@respx.mock
def test_mobile_tech_texts_their_own_verified_invoice(db):
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-7"}))
    inv = _seed(db)
    res = _mobile_send(db, inv)
    assert res.status_code == 200
    assert json.loads(res.body)["sms_sent"] is True
    db.refresh(inv)
    assert inv.sent_via == "sms"
    [row] = _audit(db, "mobile_invoice_sent_sms")
    assert row.entity_id == str(inv.id)
    [msg] = _messages(db)
    assert msg.sent_by_user_id == UUID(TECH_USER)
    assert CLIENT_TENANTS == [TENANT]  # same source as the office route


@respx.mock
def test_mobile_refuses_another_techs_invoice(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    inv = _seed(db, assigned_to="tech-other")
    assert _mobile_send(db, inv).status_code == 403
    assert not route.called


@respx.mock
def test_mobile_refuses_unverified_invoice(db):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    inv = _seed(db, status="sent", verified=False)
    res = _mobile_send(db, inv)
    assert res.status_code == 409 and json.loads(res.body)["awaiting_verification"] is True
    preview = mobile_invoice_sms_preview(invoice_id=str(inv.id), request=_Req(), payload=None,
                                         current_user=TECH, db=db)
    assert json.loads(preview.body)["blocked"]["code"] == "awaiting_verification"
    assert not route.called


@respx.mock
def test_mobile_refuses_when_texting_module_is_off(db, monkeypatch):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: False)
    inv = _seed(db)
    assert _mobile_send(db, inv).status_code == 403
    assert not route.called


# ── transport: a text or a call is never re-POSTed on an ambiguous failure ──


@respx.mock
def test_client_does_not_resend_a_text_after_a_read_timeout():
    route = respx.post(SEND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    c = PhoneComClient(token="t", voip_id=VOIP)
    c._backoff_seconds = lambda attempt: 0
    with pytest.raises(httpx.ReadTimeout):
        c.send_message(from_number=FROM, to_number=CUSTOMER_E164, body="hi")
    assert route.call_count == 1


@respx.mock
def test_client_retries_a_text_that_never_connected():
    route = respx.post(SEND_URL).mock(side_effect=[
        httpx.ConnectError("refused"),
        httpx.Response(200, json={"id": "pc-r"}),
    ])
    c = PhoneComClient(token="t", voip_id=VOIP)
    c._backoff_seconds = lambda attempt: 0
    assert c.send_message(from_number=FROM, to_number=CUSTOMER_E164, body="hi")["id"] == "pc-r"
    assert route.call_count == 2


@respx.mock
def test_client_does_not_ring_the_customer_twice_on_a_5xx():
    route = respx.post(f"{BASE_URL}/accounts/{VOIP}/calls").mock(return_value=httpx.Response(503))
    c = PhoneComClient(token="t", voip_id=VOIP)
    c._backoff_seconds = lambda attempt: 0
    with pytest.raises(PhoneComAPIError):
        c.originate_call(callee_phone_number=CUSTOMER_E164, caller_extension=101)
    assert route.call_count == 1


@respx.mock
def test_client_still_retries_idempotent_reads_on_5xx():
    route = respx.get(f"{BASE_URL}/accounts").mock(side_effect=[
        httpx.Response(503), httpx.Response(200, json={"items": [{"id": VOIP}]}),
    ])
    c = PhoneComClient(token="t", voip_id=VOIP)
    c._backoff_seconds = lambda attempt: 0
    c.get_account()
    assert route.call_count == 2

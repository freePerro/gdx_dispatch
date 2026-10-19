"""The win-back drain (tasks/winback_drain.py, GDXA-1723).

Real database, real claim, real router: the campaign is queued through
``send_campaign`` and drained by ``drain``. The Phone.com HTTP call is
intercepted with respx, but the real PhoneComClient builds and sends it; the
email transport (``send_transactional_email``) is replaced by a recorder,
while recipient resolution and the branded body run for real.

What these defend, worst first:

  1. A row is sent at most once: two drains, or two sessions racing the
     claim, never produce a second message.
  2. A campaign is not ``sent`` until its rows are, and then it is.
  3. Opted-out, missing and texting-off customers are skipped, not sent.
  4. Failure is recorded: a definite refusal is ``failed``, an ambiguous one
     or a killed worker is ``unknown``, and none is retried.
  5. Only campaigns in ``sending`` are drained.
"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog, TenantBase, utcnow
from gdx_dispatch.models.tenant_models import AppSettings, Customer, WinbackCampaign, WinbackSend
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient
from gdx_dispatch.modules.phone_com.models import PhoneComMessage
from gdx_dispatch.routers.winback import SendCampaignIn, send_campaign
from gdx_dispatch.tasks import winback_drain
from gdx_dispatch.tasks.winback_drain import claim, drain

TENANT = str(uuid4())
VOIP = 4242
SEND_URL = f"{BASE_URL}/accounts/{VOIP}/messages"
FROM = "+16125550100"
USER = {"user_id": "office-1", "sub": "office-1", "tenant_id": TENANT, "role": "admin"}


class _Req:
    state = type("S", (), {"tenant": {"id": TENANT}})()
    headers: dict = {}
    client = None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    def _client(tenant_id, control_db, tenant_db):
        assert str(tenant_id) == TENANT
        c = PhoneComClient(token="t", voip_id=VOIP)
        c._backoff_seconds = lambda attempt: 0
        return c

    monkeypatch.setattr("gdx_dispatch.modules.phone_com.router._get_phone_com_client", _client)
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: True)


@pytest.fixture
def emails(monkeypatch):
    sent: list[dict] = []
    outcome = {"value": (True, "smtp", None)}

    def _record(**kw):
        sent.append(kw)
        return outcome["value"]

    monkeypatch.setattr("gdx_dispatch.core.transactional_email.send_transactional_email", _record)
    _record.outcome = outcome  # type: ignore[attr-defined]
    return sent, outcome


@pytest.fixture
def engine(tmp_path):
    # File-backed, not StaticPool: the race test needs two real connections.
    eng = create_engine(f"sqlite:///{tmp_path}/w.db", connect_args={"check_same_thread": False})
    Customer.__table__.create(bind=eng, checkfirst=True)
    TenantBase.metadata.create_all(bind=eng, checkfirst=True)
    with sessionmaker(bind=eng)() as s:
        s.add(AppSettings(company_name="Acme Doors", phone_com_default_caller_id=FROM))
        s.commit()
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()


def _customer(db, *, name="Pat Payer", phone="(612) 555-0142", email="pat@example.com", **kw) -> Customer:
    c = Customer(id=uuid4(), name=name, phone=phone, email=email, company_id=TENANT, **kw)
    db.add(c)
    db.commit()
    return c


def _queue(db, channel: str, customers: list[Customer], *, body="Hi {first_name}, we miss you!") -> UUID:
    campaign = WinbackCampaign(
        company_id=TENANT, name="Spring", status="draft", channel=channel,
        subject="We miss you" if channel == "email" else None,
        body_template=body, inactivity_months=6, created_by="office-1",
    )
    db.add(campaign)
    db.commit()
    out = send_campaign(
        campaign.id, _Req(), USER, db, SendCampaignIn(override_customer_ids=[str(c.id) for c in customers])
    )
    assert out["enqueued"] == len(customers)
    return campaign.id


def _sends(db, campaign_id) -> list[WinbackSend]:
    db.expire_all()
    return db.execute(select(WinbackSend).where(WinbackSend.campaign_id == campaign_id)).scalars().all()


def _campaign(db, campaign_id) -> WinbackCampaign:
    db.expire_all()
    return db.get(WinbackCampaign, campaign_id)


def _audits(db, action: str) -> list[AuditLog]:
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _ok(route, msg_id="pc-1"):
    return route.mock(return_value=httpx.Response(200, json={"id": msg_id, "status": "sent"}))


# ── the send routes to the right transport, and the campaign settles ─────


@respx.mock
def test_sms_campaign_is_texted_and_settles_sent(db):
    route = _ok(respx.post(SEND_URL))
    pat = _customer(db)
    cid = _queue(db, "sms", [pat])
    assert _campaign(db, cid).status == "sending"
    assert _campaign(db, cid).sent_at is None
    assert len(_audits(db, "winback_campaign_queued")) == 1
    assert _audits(db, "winback_campaign_sent") == []

    result = drain(db)

    assert route.call_count == 1
    payload = json.loads(route.calls.last.request.content)
    assert payload["text"] == "Hi Pat, we miss you!"
    assert payload["to"] == "+16125550142"
    [send] = _sends(db, cid)
    assert (send.status, send.error) == ("sent", None)
    assert send.sent_at is not None
    msg = db.execute(select(PhoneComMessage)).scalar_one()
    assert (msg.direction, msg.customer_id, msg.phone_com_message_id) == ("out", pat.id, "pc-1")
    c = _campaign(db, cid)
    assert c.status == "sent" and c.sent_at is not None
    [settled] = _audits(db, "winback_campaign_sent")
    assert settled.details["counts"] == {"sent": 1}
    assert len(_audits(db, "winback_sends_drained")) == 1
    assert result["sent"] == 1 and result["settled"] == 1


def test_email_campaign_is_mailed_through_the_transactional_pipeline(db, emails):
    sent, _ = emails
    pat = _customer(db)
    cid = _queue(db, "email", [pat], body="Hello {customer_name}")

    drain(db)

    [mail] = sent
    assert mail["to_email"] == "pat@example.com"
    assert mail["subject"] == "We miss you"
    assert "Hello Pat Payer" in mail["html_body"]
    assert (mail["kind"], mail["entity_id"]) == ("winback", str(cid))
    assert _sends(db, cid)[0].status == "sent"
    assert _campaign(db, cid).status == "sent"


def test_email_transport_refusal_is_failed_and_a_wholly_failed_campaign_says_so(db, emails):
    _, outcome = emails
    outcome["value"] = (False, None, "smtp_not_configured")
    cid = _queue(db, "email", [_customer(db)])

    drain(db)

    [send] = _sends(db, cid)
    assert (send.status, send.error, send.sent_at) == ("failed", "smtp_not_configured", None)
    assert _campaign(db, cid).status == "failed"
    assert _audits(db, "winback_campaign_failed")[0].details["counts"] == {"failed": 1}


# ── refusals at send time ────────────────────────────────────────────────


@respx.mock
def test_opted_out_and_missing_customers_are_skipped_not_sent(db, emails):
    route = _ok(respx.post(SEND_URL))
    sent, _ = emails
    quiet = _customer(db, sms_opt_out=True)
    gone_id = uuid4()  # a well-formed override id matching no customer
    sms_cid = _queue(db, "sms", [quiet])
    db.execute(
        WinbackSend.__table__.insert().values(
            id=uuid4(), company_id=TENANT, campaign_id=sms_cid, customer_id=gone_id,
            channel="sms", status="queued", created_at=utcnow(),
        )
    )
    db.commit()
    email_cid = _queue(db, "email", [_customer(db, email_opt_out=True)])

    drain(db)

    assert route.call_count == 0
    assert sent == []
    errors = sorted(s.error for s in _sends(db, sms_cid))
    assert errors == ["customer_not_found", "sms_opt_out"]
    assert _sends(db, email_cid)[0].error == "email_opt_out"
    assert {s.status for s in _sends(db, sms_cid) + _sends(db, email_cid)} == {"skipped"}
    assert _campaign(db, sms_cid).status == "failed"


@respx.mock
def test_texting_switched_off_skips_the_send(db, monkeypatch):
    route = _ok(respx.post(SEND_URL))
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: key != "phone_com")
    cid = _queue(db, "sms", [_customer(db)])

    drain(db)

    assert route.call_count == 0
    assert _sends(db, cid)[0].error == "texting_disabled"


# ── failure outcomes: never retried ──────────────────────────────────────


@pytest.mark.parametrize(
    "response, status, campaign_status",
    [
        (httpx.Response(400, json={"error": "bad"}), "failed", "failed"),
        # May have arrived: the campaign is not called failed.
        (httpx.Response(503), "unknown", "sent"),
        (httpx.ReadTimeout("slow"), "unknown", "sent"),
    ],
)
@respx.mock
def test_phone_com_failure_is_recorded_and_never_resent(db, response, status, campaign_status):
    route = respx.post(SEND_URL).mock(side_effect=response)
    cid = _queue(db, "sms", [_customer(db)])

    drain(db)
    calls = route.call_count
    drain(db)

    assert route.call_count == calls, "a failed or unconfirmed text was sent again"
    [send] = _sends(db, cid)
    assert send.status == status
    assert send.error
    assert _campaign(db, cid).status == campaign_status


def test_a_row_a_killed_worker_left_sending_becomes_unknown(db, emails):
    sent, _ = emails
    cid = _queue(db, "email", [_customer(db)])
    [send] = _sends(db, cid)
    assert claim(db, send.id, now=utcnow() - timedelta(minutes=30))  # the worker then died

    drain(db)

    assert sent == [], "an interrupted send was retried"
    [send] = _sends(db, cid)
    assert (send.status, send.error) == ("unknown", "worker_interrupted")
    assert _campaign(db, cid).status == "sent"
    assert _audits(db, "winback_campaign_sent")[0].details["counts"] == {"unknown": 1}


def test_a_fresh_sending_row_holds_the_campaign_open(db, emails):
    cid = _queue(db, "email", [_customer(db)])
    [send] = _sends(db, cid)
    assert claim(db, send.id)  # another worker is mid-send right now

    drain(db)

    assert _sends(db, cid)[0].status == "sending"
    assert _campaign(db, cid).status == "sending"


# ── at most once ─────────────────────────────────────────────────────────


@respx.mock
def test_two_drains_send_each_row_once(db):
    route = _ok(respx.post(SEND_URL))
    cid = _queue(db, "sms", [_customer(db, phone="(612) 555-0101"), _customer(db, phone="(612) 555-0102")])

    drain(db)
    drain(db)

    assert route.call_count == 2
    assert {s.status for s in _sends(db, cid)} == {"sent"}
    assert len(_audits(db, "winback_campaign_sent")) == 1


def test_two_sessions_racing_one_claim_have_one_winner(engine, db):
    cid = _queue(db, "email", [_customer(db)])
    [send] = _sends(db, cid)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with Session() as a, Session() as b:
        # Both read the row as queued before either claims, as two overlapping
        # beat runs would.
        assert a.get(WinbackSend, send.id).status == "queued"
        assert b.get(WinbackSend, send.id).status == "queued"
        assert [claim(a, send.id), claim(b, send.id)] == [True, False]


def test_claim_matches_sqlites_dashless_uuid_storage(db):
    """SQLite stores a Uuid as 32 dashless hex, so a raw ``id = :dashed`` gate
    never matches there. The claim goes through the ORM's Uuid type."""
    cid = _queue(db, "email", [_customer(db)])
    [send] = _sends(db, cid)
    stored = db.execute(text("SELECT id FROM winback_sends")).scalar_one()
    assert stored == send.id.hex and "-" not in stored
    assert db.execute(text("SELECT count(*) FROM winback_sends WHERE id = :i"), {"i": str(send.id)}).scalar() == 0
    assert claim(db, send.id) is True


# ── scope ────────────────────────────────────────────────────────────────


def test_rows_under_a_campaign_not_sending_are_left_alone(db, emails):
    """A campaign the old router marked ``sent`` at enqueue must not be
    delivered months late when this drainer is deployed."""
    sent, _ = emails
    cid = _queue(db, "email", [_customer(db)])
    db.execute(WinbackCampaign.__table__.update().values(status="sent"))
    db.commit()

    result = drain(db)

    assert sent == []
    assert _sends(db, cid)[0].status == "queued"
    assert result["claimed"] == 0


def test_the_beat_task_is_scheduled_and_registered():
    from gdx_dispatch.core.celery_app import celery_app
    from gdx_dispatch.core.scheduler import build_beat_schedule

    entry = build_beat_schedule()["drain-winback-sends-every-minute"]
    assert entry["task"] == winback_drain.drain_winback_sends.name
    assert entry["task"] in celery_app.tasks
    assert entry["options"] == {"queue": "priority:low"}

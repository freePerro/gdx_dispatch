"""An SMS path may not record a delivery it did not make.

`core/sms.py` returns ``{"sent": False, "reason": "not configured"}`` whenever
Twilio credentials are absent — which is prod's state: verified 2026-08-22,
`printenv | grep -iE "twilio|phone_com|sms"` inside gdx-app-1 returns nothing.

`/api/jobs/{job_id}/on-my-way` discarded that result and recorded
``sms_sent: True`` regardless, so the audit trail would have asserted a customer
notification that never left the building. It has never fired (`on_my_way_sent`
has zero prod rows), which is why this was latent rather than live — but
"succeeds without doing the work" is the class the working agreement ranks
highest.

These tests EXECUTE the handler. An earlier version asserted on
``inspect.getsource`` text, which an adversarial review correctly called out as
the source-text-presence fiction this repo bans: it passed on a handler that
computed the flag and threw it away, and broke on a rename.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from gdx_dispatch.core import sms as sms_service
from gdx_dispatch.models.tenant_models import Customer, Job
from gdx_dispatch.routers import dispatch_scheduling

COMPANY = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def job_with_phone(tenant_db):
    cust = Customer(
        id=uuid.uuid4(), name="Ada", phone="+15555550123", address="1 Main St",
        company_id=COMPANY, created_at=datetime.now(UTC),
    )
    tenant_db.add(cust)
    tenant_db.flush()
    job = Job(
        id=uuid.uuid4(), title="Door", customer_id=cust.id,
        lifecycle_stage="scheduled", dispatch_status="assigned",
        billing_status="unbilled", is_return_visit=False,
        company_id=COMPANY, created_at=datetime.now(UTC),
    )
    tenant_db.add(job)
    tenant_db.flush()
    return job


# NOTE: job_id is passed as a UUID rather than the str FastAPI would hand the
# handler. Postgres casts a str transparently; SQLite's Uuid type does not, and
# coercion is not what these tests are about.
def _request():
    return SimpleNamespace(state=SimpleNamespace(tenant={"id": COMPANY}), headers={}, client=None)


def _audit_details(monkeypatch):
    """Capture what actually lands in the audit trail."""
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs.get("details") or {})

    monkeypatch.setattr(dispatch_scheduling, "log_audit_event_sync", lambda **kw: _spy(**kw))
    return captured


def test_send_sms_reports_not_sent_when_unconfigured(monkeypatch):
    """The provider contract the caller has to respect."""
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"):
        monkeypatch.delenv(var, raising=False)

    result = sms_service.send_sms(
        to_phone="+15555550123", body="hi", from_phone="", tenant_id="t1"
    )

    assert result["sent"] is False
    assert result["reason"] == "not configured"


def test_on_my_way_records_not_sent_when_provider_is_unconfigured(
    tenant_db, job_with_phone, monkeypatch
):
    """The bug, executed: an unconfigured provider must produce sms_sent False
    in the audit trail, not True."""
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    details = _audit_details(monkeypatch)

    dispatch_scheduling.on_my_way(
        job_id=job_with_phone.id, payload=None, request=_request(),
        db=tenant_db, user={"sub": "u1"},
    )

    assert details.get("sms_sent") is False, "recorded a delivery that never happened"
    assert details.get("sms_not_sent_reason") == "not configured"


def test_on_my_way_records_sent_when_the_provider_says_so(
    tenant_db, job_with_phone, monkeypatch
):
    """The other direction — the flag must still go True on a real send, or the
    fix has simply hard-coded False."""
    details = _audit_details(monkeypatch)
    monkeypatch.setattr(
        sms_service, "send_sms",
        lambda **kw: {"sent": True, "provider": "twilio", "message_id": "SM1"},
    )

    dispatch_scheduling.on_my_way(
        job_id=job_with_phone.id, payload=None, request=_request(),
        db=tenant_db, user={"sub": "u1"},
    )

    assert details.get("sms_sent") is True
    assert "sms_not_sent_reason" not in details


def test_on_my_way_names_the_reason_when_there_is_no_phone_number(
    tenant_db, monkeypatch
):
    """Every not-sent path must name itself. Recording only the provider's
    reason left this case — and the exception case — writing a blank, which is
    the one a human would actually need to investigate."""
    cust = Customer(
        id=uuid.uuid4(), name="No Phone", phone=None, company_id=COMPANY,
        created_at=datetime.now(UTC),
    )
    tenant_db.add(cust)
    tenant_db.flush()
    job = Job(
        id=uuid.uuid4(), title="Door", customer_id=cust.id,
        lifecycle_stage="scheduled", dispatch_status="assigned",
        billing_status="unbilled", is_return_visit=False,
        company_id=COMPANY, created_at=datetime.now(UTC),
    )
    tenant_db.add(job)
    tenant_db.flush()
    details = _audit_details(monkeypatch)

    dispatch_scheduling.on_my_way(
        job_id=job.id, payload=None, request=_request(),
        db=tenant_db, user={"sub": "u1"},
    )

    assert details.get("sms_sent") is False
    assert details.get("sms_not_sent_reason") == "no_customer_phone"


def test_on_my_way_names_the_reason_when_the_provider_raises(
    tenant_db, job_with_phone, monkeypatch
):
    details = _audit_details(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(sms_service, "send_sms", _boom)

    dispatch_scheduling.on_my_way(
        job_id=job_with_phone.id, payload=None, request=_request(),
        db=tenant_db, user={"sub": "u1"},
    )

    assert details.get("sms_sent") is False
    assert details.get("sms_not_sent_reason") == "exception: RuntimeError"

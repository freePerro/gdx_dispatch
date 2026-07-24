"""Wave F / S11 — normalize_status + upsert_call PII split tests."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.phone_com.models import PhoneComCall
from gdx_dispatch.modules.phone_com.upserts import normalize_status, upsert_call


@pytest.fixture
def tenant_db():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(e)
    sm = sessionmaker(bind=e, expire_on_commit=False)
    return sm()


@pytest.mark.parametrize("raw,expected_status,expected_target", [
    # Voicemail family
    ("type voicemail_received", "voicemail", None),
    ("voicemail_received", "voicemail", None),
    ("voicemail received", "voicemail", None),
    ("voicemail", "voicemail", None),
    # Forwarded with dialed number — the PII case
    ("dial_out +13202325143", "forwarded", "+13202325143"),
    ("dial_out 13202325143", "forwarded", "13202325143"),
    ("DIAL_OUT +18005550199", "forwarded", "+18005550199"),
    ("forwarded to extension", "forwarded", None),
    ("forwarded", "forwarded", None),
    # Answered / completed
    ("answered", "answered", None),
    ("completed", "answered", None),
    # Missed / busy / no_answer
    ("missed", "missed", None),
    ("busy", "missed", None),
    ("no_answer", "missed", None),
    # Canceled / hung
    ("canceled", "canceled", None),
    ("cancelled", "canceled", None),
    ("hung_up", "canceled", None),
    # Empty / None
    ("", (None,), (None,)),
    (None, (None,), (None,)),
    ("   ", (None,), (None,)),
])
def test_normalize_status(raw, expected_status, expected_target):
    s, t = normalize_status(raw)
    if isinstance(expected_status, tuple):
        assert s is None
        assert t is None
    else:
        assert s == expected_status
        assert t == expected_target


def test_unknown_status_preserved_no_target():
    """Unknown shape stays in status (cleaned), target left None — we don't
    guess at PII splits we can't confidently parse."""
    s, t = normalize_status("some_weird_carrier_code")
    assert s == "some weird carrier code"
    assert t is None


def test_upsert_call_writes_split_fields(tenant_db):
    upsert_call(tenant_db, {
        "id": "phc-call-1",
        "direction": "in",
        "caller_id": "+15551234567",
        "called_number": "+18005550199",
        "status": "dial_out +13202325143",
    })
    row = tenant_db.query(PhoneComCall).first()
    assert row.status == "forwarded"
    assert row.final_action_target == "+13202325143"


def test_upsert_call_voicemail_no_target(tenant_db):
    upsert_call(tenant_db, {
        "id": "phc-call-2",
        "direction": "in",
        "caller_id": "+15551234567",
        "called_number": "+18005550199",
        "status": "type voicemail_received",
    })
    row = tenant_db.query(PhoneComCall).first()
    assert row.status == "voicemail"
    assert row.final_action_target is None


def test_upsert_call_falls_back_to_final_action(tenant_db):
    """When `status` not in payload but `final_action` is, normalize that."""
    upsert_call(tenant_db, {
        "id": "phc-call-3",
        "direction": "in",
        "caller_id": "+15551234567",
        "called_number": "+18005550199",
        "final_action": "dial_out +12182525555",
    })
    row = tenant_db.query(PhoneComCall).first()
    assert row.status == "forwarded"
    assert row.final_action_target == "+12182525555"


def test_upsert_call_idempotent_status_does_not_drift(tenant_db):
    """Re-running upsert with same payload must not flip a clean status to
    something else."""
    payload = {
        "id": "phc-call-4",
        "direction": "in",
        "caller_id": "+15551234567",
        "called_number": "+18005550199",
        "status": "dial_out +13202325143",
    }
    upsert_call(tenant_db, payload)
    upsert_call(tenant_db, payload)
    row = tenant_db.query(PhoneComCall).filter(
        PhoneComCall.phone_com_call_id == "phc-call-4",
    ).one()
    assert row.status == "forwarded"
    assert row.final_action_target == "+13202325143"

# ── upsert_message shape tolerance (SMS-inbox fix, 2026-07-23) ──────────


def _ext_shape_payload(**overrides):
    """The extension-endpoint REST shape: to-list, epoch ints, media."""
    payload = {
        "id": 255795124,
        "message_id": "255795124",
        "text": "Doors are ready",
        "conversation_id": "614f7033",
        "type": "sms",
        "from": "+13203041658",
        "media": [],
        "created_at": 1784753860,
        "direction": "in",
        "to": [{
            "number": "+13202706010",
            "sent_at": 1784753859,
            "delivered_at": 1784753861,
            "delivery_status": "delivered",
        }],
        "extension_id": 2674043,
    }
    payload.update(overrides)
    return payload


def test_upsert_message_extension_endpoint_shape(tenant_db):
    from gdx_dispatch.modules.phone_com.upserts import upsert_message

    row = upsert_message(tenant_db, _ext_shape_payload())
    assert row is not None
    assert row.from_number == "+13203041658"
    assert row.to_number == "+13202706010"  # extracted from the to-list
    assert row.thread_key == "+13202706010|+13203041658"
    assert row.body == "Doors are ready"
    assert row.direction == "in"
    assert row.delivery_status == "delivered"  # from the to-entry
    assert row.sent_at is not None  # epoch created_at parsed
    assert row.attachments == []


def test_upsert_message_flat_webhook_shape_still_works(tenant_db):
    """Regression: the legacy flat webhook shape must keep working."""
    from gdx_dispatch.modules.phone_com.upserts import upsert_message

    row = upsert_message(tenant_db, {
        "id": "wh-1", "direction": "out",
        "from": "+18005550199", "to": "+13202959628",
        "text": "on our way", "delivery_status": "sent",
        "created_at": "2026-07-23T10:00:00Z",
    })
    assert row is not None
    assert row.to_number == "+13202959628"
    assert row.thread_key == "+13202959628|+18005550199"
    assert row.delivery_status == "sent"


def test_upsert_message_media_maps_to_attachments(tenant_db):
    """MMS via the extension endpoint carries `media`, not `attachments`."""
    from gdx_dispatch.modules.phone_com.upserts import upsert_message

    media = [{"id": "m1", "url": "https://api.phone.com/media/m1", "type": "image/jpeg"}]
    row = upsert_message(tenant_db, _ext_shape_payload(id=99, message_id="99", media=media))
    assert row.attachments == media


def test_upsert_message_explicit_attachments_beat_media(tenant_db):
    from gdx_dispatch.modules.phone_com.upserts import upsert_message

    row = upsert_message(tenant_db, _ext_shape_payload(
        id=98, message_id="98",
        attachments=["https://x/a.jpg"],
        media=[{"url": "https://x/b.jpg"}],
    ))
    assert row.attachments == ["https://x/a.jpg"]


def test_upsert_message_old_id_heals_send_then_poll_duplicate(tenant_db):
    """A message persisted at send time under Phone.com's UUID id scheme must
    NOT duplicate when the poll returns it under the numeric id — the
    extension-endpoint payload carries the UUID as `old_id`."""
    from gdx_dispatch.modules.phone_com.models import PhoneComMessage
    from gdx_dispatch.modules.phone_com.upserts import upsert_message

    # 1. Send path persisted the outbound row keyed by the POST response id.
    sent = upsert_message(tenant_db, {
        "id": "59bace5b-5fb8-4772-9154-e25d16ffa0d7", "direction": "out",
        "from": "+18005550199", "to": "+13202959628", "text": "on our way",
    })
    assert sent is not None

    # 2. The 10-min poll returns the same message: numeric id, UUID in old_id.
    polled = upsert_message(tenant_db, _ext_shape_payload(
        id=255795200, message_id="255795200",
        old_id="59bace5b-5fb8-4772-9154-e25d16ffa0d7",
        direction="out",
        **{"from": "+18005550199"},
        to=[{"number": "+13202959628", "delivery_status": "delivered"}],
        text="on our way",
    ))

    assert tenant_db.query(PhoneComMessage).count() == 1  # no duplicate
    assert polled.id == sent.id
    assert polled.phone_com_message_id == "255795200"  # key migrated to numeric
    assert polled.delivery_status == "delivered"


def test_upsert_message_dup_pair_prefers_pc_id_row_no_unique_wedge(tenant_db):
    """If BOTH id schemes already exist as separate rows, the upsert must
    update the pc_id-keyed row (no UNIQUE-index collision), not wedge the
    sync by migrating the old_id row onto a taken key."""
    from gdx_dispatch.modules.phone_com.models import PhoneComMessage
    from gdx_dispatch.modules.phone_com.upserts import upsert_message

    upsert_message(tenant_db, {
        "id": "59bace5b-5fb8-4772-9154-e25d16ffa0d7", "direction": "out",
        "from": "+18005550199", "to": "+13202959628", "text": "v1",
    })
    upsert_message(tenant_db, {
        "id": "255795300", "direction": "out",
        "from": "+18005550199", "to": "+13202959628", "text": "v1",
    })
    assert tenant_db.query(PhoneComMessage).count() == 2

    # Poll payload leads with the numeric id and carries the UUID as old_id.
    row = upsert_message(tenant_db, _ext_shape_payload(
        id=255795300, message_id="255795300",
        old_id="59bace5b-5fb8-4772-9154-e25d16ffa0d7",
        direction="out",
        **{"from": "+18005550199"},
        to=[{"number": "+13202959628", "delivery_status": "delivered"}],
        text="v2",
    ))
    assert row.phone_com_message_id == "255795300"
    assert row.body == "v2"
    assert tenant_db.query(PhoneComMessage).count() == 2  # no crash, no merge

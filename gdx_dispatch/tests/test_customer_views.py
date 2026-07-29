"""Tests for gdx_dispatch/core/customer_views.py — "did the customer look?"

Two public, unauthenticated endpoints are what a customer hits after clicking
the link in an estimate or invoice email. Neither logged anything, so the
question was unanswerable.

Because the endpoints are public, three things have to be right: repeated
views must not spam the feed, mail-gateway link scanners must not be reported
as the customer, and nothing here may break the page a customer is trying to
pay on.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.customer_views import (
    CUSTOMER_ACTOR,
    looks_like_a_bot,
    record_customer_view,
    within_scanner_grace_period,
)
from gdx_dispatch.tests.conftest import make_fresh_db

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class _Headers(dict):
    def get(self, key, default=None):  # noqa: D102 - dict-like request headers
        return super().get(key.lower(), default)


class _Request:
    def __init__(self, user_agent=BROWSER_UA):
        self.headers = _Headers({"user-agent": user_agent} if user_agent else {})


@pytest.fixture()
def db():
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    yield session
    session.close()
    engine.dispose()


def _view(db, entity_id, request=None, action="invoice_viewed_by_customer"):
    return record_customer_view(
        db,
        action=action,
        entity_type="invoice",
        entity_id=entity_id,
        tenant_id="t-1",
        request=request if request is not None else _Request(),
    )


def _rows(db, action="invoice_viewed_by_customer"):
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_customer_opening_the_link_is_recorded(db):
    assert _view(db, "inv-1") is True
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0].user_id == CUSTOMER_ACTOR
    assert rows[0].entity_id == "inv-1"


def test_the_user_agent_is_kept_so_a_false_view_is_diagnosable(db):
    _view(db, "inv-1")
    assert "Chrome" in _rows(db)[0].details["user_agent"]
    assert _rows(db)[0].details["via"] == "public_link"


# ---------------------------------------------------------------------------
# De-dupe
# ---------------------------------------------------------------------------


def test_refreshing_the_page_is_one_view_not_eight(db):
    for _ in range(8):
        _view(db, "inv-1")
    assert len(_rows(db)) == 1


def test_a_different_document_is_a_different_view(db):
    _view(db, "inv-1")
    _view(db, "inv-2")
    assert len(_rows(db)) == 2


def test_dedupe_is_per_action_not_global(db):
    _view(db, "inv-1", action="invoice_viewed_by_customer")
    _view(db, "inv-1", action="estimate_viewed_by_customer")
    assert len(_rows(db, "invoice_viewed_by_customer")) == 1
    assert len(_rows(db, "estimate_viewed_by_customer")) == 1


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ua",
    [
        # Corporate mail gateways follow every link in every message. Without
        # this the feed would announce that the customer opened the estimate
        # the instant it was sent.
        "Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1) SafeLinks",
        "Proofpoint-URL-Defense/1.0",
        "Mimecast-Link-Scanner",
        "Mozilla/5.0 (compatible; Googlebot/2.1)",
        "curl/8.4.0",
        "python-requests/2.31.0",
        "HeadlessChrome/126.0",
        "Pingdom.com_bot_version_1.4",
    ],
)
def test_link_scanners_are_not_the_customer(db, ua):
    assert _view(db, "inv-1", request=_Request(ua)) is False
    assert _rows(db) == []


def test_a_request_with_no_user_agent_is_not_a_browser(db):
    assert _view(db, "inv-1", request=_Request(None)) is False
    assert _rows(db) == []


def test_bot_detection_is_case_insensitive():
    assert looks_like_a_bot("SafeLinks/1.0")
    assert looks_like_a_bot("SAFELINKS/1.0")
    assert not looks_like_a_bot(BROWSER_UA)


# ---------------------------------------------------------------------------
# It must never break the page
# ---------------------------------------------------------------------------


def test_a_write_failure_does_not_propagate(db):
    """A customer must still be able to see and pay their invoice if the audit
    write fails."""

    class _ExplodingDb:
        def execute(self, *a, **kw):
            raise RuntimeError("db is down")

        def rollback(self):
            pass

    assert (
        record_customer_view(
            _ExplodingDb(),
            action="invoice_viewed_by_customer",
            entity_type="invoice",
            entity_id="inv-1",
            request=_Request(),
        )
        is False
    )


def test_a_missing_request_object_is_treated_as_a_bot(db):
    # No request means no user agent means not a browser.
    assert (
        record_customer_view(
            db,
            action="invoice_viewed_by_customer",
            entity_type="invoice",
            entity_id="inv-1",
            request=None,
        )
        is False
    )


def test_uuid_entity_ids_are_stringified(db):
    eid = uuid.uuid4()
    _view(db, eid)
    assert _rows(db)[0].entity_id == str(eid)


# ---------------------------------------------------------------------------
# The scanner that pretends to be Chrome
# ---------------------------------------------------------------------------


def test_a_view_seconds_after_send_is_the_gateway_not_the_customer(db):
    """Defender Safe Links and Proofpoint detonate links with a REAL Chrome
    user-agent on purpose — beating cloaking is the design. No UA list can
    catch them. Timing can: nobody opens an estimate two seconds after it
    leaves the outbox."""
    from datetime import UTC, datetime

    just_sent = datetime.now(UTC)
    wrote = record_customer_view(
        db,
        action="estimate_viewed_by_customer",
        entity_type="estimate",
        entity_id="est-1",
        request=_Request(BROWSER_UA),
        sent_at=just_sent,
    )
    assert wrote is False
    assert _rows(db, "estimate_viewed_by_customer") == []


def test_a_view_well_after_send_is_a_real_customer(db):
    from datetime import UTC, datetime, timedelta

    long_ago = datetime.now(UTC) - timedelta(hours=3)
    assert (
        record_customer_view(
            db,
            action="estimate_viewed_by_customer",
            entity_type="estimate",
            entity_id="est-1",
            request=_Request(BROWSER_UA),
            sent_at=long_ago,
        )
        is True
    )


def test_a_naive_sent_at_is_not_a_crash(db):
    from datetime import datetime

    # Some rows carry naive timestamps; comparing them to an aware "now"
    # raises TypeError unless handled.
    assert within_scanner_grace_period(datetime.now()) in (True, False)


def test_an_unsent_document_has_no_grace_period():
    assert within_scanner_grace_period(None) is False


def test_a_real_phone_brand_containing_bot_is_not_a_bot():
    # "CUBOT" is an Android phone brand. A bare "bot" substring silently
    # dropped those customers entirely.
    ua = "Mozilla/5.0 (Linux; Android 13; CUBOT NOTE 30) AppleWebKit/537.36 Chrome/126.0 Mobile Safari/537.36"
    assert looks_like_a_bot(ua) is False


# ---------------------------------------------------------------------------
# It has to RENDER as a customer, which is the whole point
# ---------------------------------------------------------------------------


def test_the_recorded_row_renders_as_a_customer_action(db):
    """The row is useless if the feed calls it an API key.

    The actor is an anonymous public-link visitor, not a CustomerUser row, so
    resolve_actors has to classify the sentinel explicitly — it previously fell
    through to the API-key branch and the customer badge never fired.
    """
    from gdx_dispatch.core.audit_labels import ACTOR_CUSTOMER, decorate_rows

    _view(db, "inv-1")
    row = _rows(db)[0]
    decorated = decorate_rows(
        db,
        [
            {
                "id": str(row.id),
                "user_id": row.user_id,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
            }
        ],
    )
    assert decorated[0]["actor_type"] == ACTOR_CUSTOMER
    assert decorated[0]["user_name"] == "Customer"

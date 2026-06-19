"""Phase 1.5 — push subscription backend tests.

E3 — DB-backed PushSubscription model: upsert/revoke/list semantics.
E4 — send_push helper short-circuits cleanly when pywebpush or VAPID
     keys are missing (the production state today), and reports the
     reason via SendResult so the caller can hand off to the in-app
     fallback path.
E5 — fallback-mode endpoint reads the tenant setting; default is
     'badge_only'.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.push_subscriptions import (
    list_subscriptions_for_user,
    revoke_subscription,
    send_push,
    upsert_subscription,
)
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import PushSubscription


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'push.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(eng, checkfirst=True)
    Session_ = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s = Session_()
    yield s
    s.close()
    eng.dispose()


# ── E3 — upsert / revoke / list ────────────────────────────────────────


def test_e3_upsert_creates_then_updates(db):
    a = upsert_subscription(
        db, user_id="u1", endpoint="https://push.example/a",
        p256dh="key1", auth="auth1",
    )
    db.commit()
    assert a.user_id == "u1" and a.p256dh == "key1"

    # Same endpoint, fresh keys → update in place, no duplicate row.
    b = upsert_subscription(
        db, user_id="u1", endpoint="https://push.example/a",
        p256dh="key2", auth="auth2",
    )
    db.commit()
    assert b.id == a.id
    assert b.p256dh == "key2"
    rows = db.query(PushSubscription).all()
    assert len(rows) == 1


def test_e3_revoke_marks_row_and_excludes_from_list(db):
    upsert_subscription(
        db, user_id="u1", endpoint="https://push.example/x",
        p256dh="k", auth="a",
    )
    db.commit()
    assert revoke_subscription(db, endpoint="https://push.example/x") is True
    db.commit()
    assert list_subscriptions_for_user(db, "u1") == []
    # Revoked row still exists in the table — audit trail.
    row = db.query(PushSubscription).filter_by(endpoint="https://push.example/x").one()
    assert row.revoked_at is not None


def test_e3_revoke_unknown_returns_false(db):
    assert revoke_subscription(db, endpoint="https://push.example/ghost") is False


def test_e3_list_filters_revoked_and_returns_only_active(db):
    upsert_subscription(
        db, user_id="u1", endpoint="https://push.example/1",
        p256dh="k", auth="a",
    )
    upsert_subscription(
        db, user_id="u1", endpoint="https://push.example/2",
        p256dh="k", auth="a",
    )
    upsert_subscription(
        db, user_id="u2", endpoint="https://push.example/3",
        p256dh="k", auth="a",
    )
    db.commit()
    revoke_subscription(db, endpoint="https://push.example/1")
    db.commit()
    rows = list_subscriptions_for_user(db, "u1")
    assert len(rows) == 1
    assert rows[0].endpoint == "https://push.example/2"


def test_e3_re_subscribe_after_revoke_clears_revoked_at(db):
    """Browser permission flip → revoke → user re-grants → endpoint comes
    back. The row must be re-activated, not duplicated."""
    a = upsert_subscription(
        db, user_id="u1", endpoint="https://push.example/q",
        p256dh="k1", auth="a1",
    )
    db.commit()
    revoke_subscription(db, endpoint="https://push.example/q")
    db.commit()
    b = upsert_subscription(
        db, user_id="u1", endpoint="https://push.example/q",
        p256dh="k2", auth="a2",
    )
    db.commit()
    assert b.id == a.id
    assert b.revoked_at is None
    assert b.p256dh == "k2"


# ── E4 — send_push short-circuit reporting ────────────────────────────


def test_e4_no_pywebpush_returns_skipped_no_pywebpush(db, monkeypatch):
    """In our prod state today pywebpush is NOT installed; send_push must
    report skipped_no_pywebpush so the caller can fall back to in-app."""
    from gdx_dispatch.core import push_subscriptions as ps

    monkeypatch.setattr(ps, "_WEBPUSH_AVAILABLE", False)
    upsert_subscription(
        db, user_id="u1", endpoint="https://push.example/p",
        p256dh="k", auth="a",
    )
    db.commit()
    res = send_push(db, user_id="u1", title="t", body="b")
    assert res.skipped_no_pywebpush is True
    assert res.sent == 0


def test_e4_no_vapid_keys_returns_skipped_no_vapid(db, monkeypatch):
    from gdx_dispatch.core import push_subscriptions as ps

    monkeypatch.setattr(ps, "_WEBPUSH_AVAILABLE", True)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    upsert_subscription(
        db, user_id="u1", endpoint="https://push.example/p",
        p256dh="k", auth="a",
    )
    db.commit()
    res = send_push(db, user_id="u1", title="t", body="b")
    assert res.skipped_no_vapid is True


def test_e4_no_subscriptions_returns_skipped_no_subs(db, monkeypatch):
    from gdx_dispatch.core import push_subscriptions as ps

    monkeypatch.setattr(ps, "_WEBPUSH_AVAILABLE", True)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "x")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "y")
    res = send_push(db, user_id="ghost", title="t", body="b")
    assert res.skipped_no_subs is True

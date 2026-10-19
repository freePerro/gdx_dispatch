"""GDXA-1859: background NextAction writers go through one audited helper.

billing_followup, invoice_reminders_auto and outlook's sync health check each
``add(NextAction(...))``-ed or mutated their row in place and committed with
no audit row (invariant #1). ``upsert_system_action`` and
``resolve_system_action`` flush the row and its trail into the caller's
transaction. Everything below runs on a real SQLite session and the real audit
chain; nothing is mocked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.next_action import (
    NextAction,
    resolve_system_action,
    upsert_system_action,
)

TENANT = "11111111-1111-1111-1111-111111111859"
ACTOR = "system:billing_followup"
KEY = {"action_type": "billing_followup", "reference_id": "billing-followup"}


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _audits(db) -> list[AuditLog]:
    return list(
        db.execute(
            select(AuditLog).where(AuditLog.entity_type == "next_action").order_by(AuditLog.created_at)
        ).scalars()
    )


def _upsert(db, **fields):
    return upsert_system_action(db, tenant_id=TENANT, actor=ACTOR, fields=fields, **KEY)


def test_create_writes_row_and_audit_in_one_commit(db):
    row, outcome = _upsert(db, title="Billing follow-up", description="3 drafts", priority="high",
                           action_url="/billing", estimated_value=120.0)
    assert outcome == "created"
    # The helper does not commit; the caller's rollback drops row and trail together.
    db.rollback()
    assert db.query(NextAction).count() == 0
    assert _audits(db) == []

    row, outcome = _upsert(db, title="Billing follow-up", description="3 drafts", priority="high",
                           action_url="/billing", estimated_value=120.0)
    db.commit()
    assert outcome == "created"
    assert row.status == "pending" and row.user_id is None
    (audit,) = _audits(db)
    assert audit.action == "next_action.create"
    assert audit.user_id == ACTOR
    assert audit.entity_id == str(row.id)
    assert audit.details["reference_id"] == "billing-followup"
    assert audit.details["estimated_value"] == 120.0


def test_in_place_update_is_audited_and_unchanged_writes_nothing(db):
    row, _ = _upsert(db, title="T", description="old", estimated_value=1.0)
    db.commit()

    same, outcome = _upsert(db, title="T", description="new", estimated_value=2.0)
    db.commit()
    assert outcome == "updated" and same.id == row.id
    assert db.query(NextAction).count() == 1
    update = _audits(db)[-1]
    assert update.action == "next_action.update"
    assert update.user_id == ACTOR
    assert update.details["changed"] == {
        "description": {"from": "old", "to": "new"},
        "estimated_value": {"from": 1.0, "to": 2.0},
    }

    _, outcome = _upsert(db, title="T", description="new", estimated_value=2.0)
    db.commit()
    assert outcome == "unchanged"
    assert len(_audits(db)) == 2


def test_wake_respects_live_snooze_and_reopens_expired_one(db):
    row, _ = _upsert(db, title="T")
    row.status = "snoozed"
    row.snoozed_until = datetime.now(timezone.utc) + timedelta(days=2)
    db.commit()

    _, outcome = _upsert(db, title="T")
    db.commit()
    assert outcome == "unchanged"
    assert db.get(NextAction, row.id).status == "snoozed"

    row.snoozed_until = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    _, outcome = _upsert(db, title="T")
    db.commit()
    assert outcome == "updated"
    assert db.get(NextAction, row.id).status == "pending"
    assert _audits(db)[-1].details["changed"] == {"status": {"from": "snoozed", "to": "pending"}}

    # wake=False leaves status alone (invoice_reminders_auto's nudge).
    row.status = "snoozed"
    db.commit()
    upsert_system_action(db, tenant_id=TENANT, actor=ACTOR, fields={"title": "T"}, wake=False, **KEY)
    assert db.get(NextAction, row.id).status == "snoozed"


def test_resolve_completes_and_audits_then_next_upsert_creates_fresh(db):
    assert resolve_system_action(db, tenant_id=TENANT, actor=ACTOR, **KEY) == []
    assert _audits(db) == []

    row, _ = _upsert(db, title="T")
    db.commit()
    (done,) = resolve_system_action(db, tenant_id=TENANT, actor=ACTOR, **KEY)
    db.commit()
    assert done.id == row.id and done.status == "completed" and done.completed_at is not None
    audit = _audits(db)[-1]
    assert audit.action == "next_action.complete"
    assert audit.user_id == ACTOR
    assert audit.details["from"] == "pending"

    fresh, outcome = _upsert(db, title="T")
    db.commit()
    assert outcome == "created" and fresh.id != row.id


@pytest.mark.parametrize(
    "actor", ["system", "system:", "", "00000000-0000-0000-0000-000000001859", "system:" + "x" * 58],
)
def test_actor_must_name_the_task(db, actor):
    with pytest.raises(ValueError):
        upsert_system_action(db, tenant_id=TENANT, actor=actor, fields={"title": "T"}, **KEY)
    with pytest.raises(ValueError):
        resolve_system_action(db, tenant_id=TENANT, actor=actor, **KEY)
    assert db.query(NextAction).count() == 0


def test_fields_cannot_carry_status_or_identity(db):
    for bad in ({"status": "completed"}, {"tenant_id": "x"}, {"id": "x"}, {"reference_id": "y"}):
        with pytest.raises(ValueError):
            _upsert(db, title="T", **bad)
    assert db.query(NextAction).count() == 0


def test_decimal_total_matching_the_stored_float_is_unchanged(db):
    # A SQL sum over a Numeric column is a Decimal on Postgres.
    _upsert(db, title="T", estimated_value=Decimal("120.10"))
    db.commit()
    _, outcome = _upsert(db, title="T", estimated_value=Decimal("120.10"))
    assert outcome == "unchanged"
    assert len(_audits(db)) == 1


def test_a_row_under_another_tenant_string_is_left_alone(db):
    # The office queue filters on tenant_id, so refreshing a drifted row would
    # leave the nag invisible. A visible one is created instead.
    drifted = NextAction(tenant_id="gdx", title="old", created_at=datetime.now(timezone.utc), **KEY)
    db.add(drifted)
    db.commit()
    row, outcome = _upsert(db, title="T")
    db.commit()
    assert outcome == "created" and row.tenant_id == TENANT
    assert db.get(NextAction, drifted.id).title == "old"
    assert resolve_system_action(db, tenant_id=TENANT, actor=ACTOR, **KEY) == [row]
    assert db.get(NextAction, drifted.id).status == "pending"


def test_duplicates_refresh_the_newest_and_resolve_completes_all(db):
    now = datetime.now(timezone.utc)
    older = NextAction(tenant_id=TENANT, title="old", created_at=now - timedelta(days=3), **KEY)
    newest = NextAction(tenant_id=TENANT, title="new", created_at=now, **KEY)
    db.add_all([older, newest])
    db.commit()
    row, outcome = _upsert(db, title="T2")
    db.commit()
    assert outcome == "updated" and row.id == newest.id
    assert db.get(NextAction, older.id).title == "old"

    done = resolve_system_action(db, tenant_id=TENANT, actor=ACTOR, **KEY)
    db.commit()
    assert {r.id for r in done} == {older.id, newest.id}
    assert [a.action for a in _audits(db)][-2:] == ["next_action.complete"] * 2
    _, outcome = _upsert(db, title="T2")
    assert outcome == "created"


def test_none_estimated_value_is_refused_before_any_write(db):
    _upsert(db, title="T")
    db.commit()
    with pytest.raises(TypeError):
        _upsert(db, title="T", estimated_value=None)
    assert len(_audits(db)) == 1

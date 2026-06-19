"""HTTP-level tests for /api/forecast/recurring/streams/*.

Covers happy-path CRUD, list filtering, dual-term validation, end-then-
edit blocked, suggestion confirm, deleted-rows excluded, from-transaction
convenience, unlink-hit, and the detect-now trigger.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.forecasting import router as forecasting_router
from gdx_dispatch.modules.forecasting.models import (
    RecurringStream,
    RecurringStreamHit,
)
from gdx_dispatch.modules.quickbooks.banking import QBBankTransaction


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    # Stub out audit logging — the test DB doesn't have audit_log table.
    import gdx_dispatch.modules.forecasting.router as router_mod
    monkeypatch_target = router_mod.log_audit_event_sync
    router_mod.log_audit_event_sync = lambda *a, **kw: None

    app.include_router(forecasting_router.router)
    app.dependency_overrides[forecasting_router.get_db] = _override_db
    app.dependency_overrides[forecasting_router.get_current_user] = lambda: {
        "sub": str(uuid4()),
        "role": "admin",
        "tenant_id": "tenant-test",
    }
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, SessionLocal
    router_mod.log_audit_event_sync = monkeypatch_target
    app.dependency_overrides.clear()
    engine.dispose()


# ─── Manual create + list + get ─────────────────────────────────────────────

def test_create_lists_and_gets_stream(client):
    tc, _ = client
    r = tc.post("/api/forecast/recurring/streams", json={
        "label": "Phone.com",
        "payee_pattern": "PHONE.COM",
        "amount_min": 40.0,
        "amount_max": 50.0,
        "cadence": "monthly",
        "cadence_anchor_day": 9,
        "start_date": "2025-01-09",
    })
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["status"] == "active"
    assert r.json()["source"] == "manual"

    r2 = tc.get("/api/forecast/recurring/streams")
    assert r2.status_code == 200
    assert r2.json()["total"] == 1

    r3 = tc.get(f"/api/forecast/recurring/streams/{sid}")
    assert r3.status_code == 200
    assert r3.json()["label"] == "Phone.com"
    assert r3.json()["hits"] == []


def test_create_rejects_dual_term(client):
    tc, _ = client
    r = tc.post("/api/forecast/recurring/streams", json={
        "label": "X",
        "payee_pattern": "X",
        "amount_min": 1, "amount_max": 2,
        "cadence": "monthly",
        "term_total_occurrences": 12,
        "term_end_date": "2027-01-01",
    })
    assert r.status_code == 400
    assert "both" in r.json()["detail"].lower()


def test_create_rejects_inverted_amount_window(client):
    tc, _ = client
    r = tc.post("/api/forecast/recurring/streams", json={
        "label": "X",
        "payee_pattern": "X",
        "amount_min": 100, "amount_max": 1,
        "cadence": "monthly",
    })
    assert r.status_code == 400


def test_create_rejects_invalid_cadence(client):
    tc, _ = client
    r = tc.post("/api/forecast/recurring/streams", json={
        "label": "X",
        "payee_pattern": "X",
        "amount_min": 1, "amount_max": 2,
        "cadence": "fortnightly",  # not in our enum
    })
    assert r.status_code == 400


# ─── List filtering ─────────────────────────────────────────────────────────

def test_list_filters_by_status(client):
    tc, SessionLocal = client
    db = SessionLocal()
    db.add(RecurringStream(label="A", source="manual", status="active",
                           payee_pattern="A", amount_min=1, amount_max=2, cadence="monthly"))
    db.add(RecurringStream(label="B", source="observed", status="suggested",
                           payee_pattern="B", amount_min=1, amount_max=2, cadence="monthly"))
    db.commit()
    db.close()

    r = tc.get("/api/forecast/recurring/streams?status=suggested")
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["label"] == "B"


def test_list_excludes_soft_deleted(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="Gone", source="manual", status="active",
                        payee_pattern="X", amount_min=1, amount_max=2, cadence="monthly")
    db.add(s)
    db.commit()
    sid = str(s.id)
    db.close()

    assert tc.delete(f"/api/forecast/recurring/streams/{sid}").status_code == 200
    r = tc.get("/api/forecast/recurring/streams")
    assert r.json()["total"] == 0


# ─── Confirm suggested ─────────────────────────────────────────────────────

def test_confirm_suggested_to_active(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="Sub", source="observed", status="suggested",
                        payee_pattern="SUB", amount_min=10, amount_max=20, cadence="monthly")
    db.add(s); db.commit()
    sid = str(s.id)
    db.close()

    r = tc.post(f"/api/forecast/recurring/streams/{sid}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_confirm_rejects_already_active(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="Sub", source="manual", status="active",
                        payee_pattern="SUB", amount_min=10, amount_max=20, cadence="monthly")
    db.add(s); db.commit()
    sid = str(s.id)
    db.close()
    r = tc.post(f"/api/forecast/recurring/streams/{sid}/confirm")
    assert r.status_code == 409


# ─── End (paid_off / cancelled / expired) ──────────────────────────────────

def test_end_paid_off_preserves_hits(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="Loan", source="manual", status="active",
                        payee_pattern="LOAN", amount_min=300, amount_max=400, cadence="monthly",
                        term_total_occurrences=36, occurrences_seen=14)
    db.add(s); db.commit()
    sid = s.id
    db.add(RecurringStreamHit(stream_id=sid, qb_txn_id="t1",
                              txn_date=date(2025, 1, 1), amount=376.56, confirmed=True))
    db.commit()
    db.close()

    r = tc.post(f"/api/forecast/recurring/streams/{sid}/end", json={"reason": "paid_off"})
    assert r.status_code == 200
    assert r.json()["status"] == "paid_off"
    assert r.json()["ended_at"] is not None

    db2 = SessionLocal()
    assert db2.query(RecurringStreamHit).count() == 1, "hits must be preserved on end"
    db2.close()


def test_end_then_edit_blocked(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="X", source="manual", status="active",
                        payee_pattern="X", amount_min=1, amount_max=2, cadence="monthly")
    db.add(s); db.commit()
    sid = str(s.id)
    db.close()

    tc.post(f"/api/forecast/recurring/streams/{sid}/end", json={"reason": "cancelled"})
    r = tc.patch(f"/api/forecast/recurring/streams/{sid}", json={"label": "new"})
    assert r.status_code == 409


def test_end_idempotency_guard(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="X", source="manual", status="active",
                        payee_pattern="X", amount_min=1, amount_max=2, cadence="monthly")
    db.add(s); db.commit()
    sid = str(s.id)
    db.close()
    tc.post(f"/api/forecast/recurring/streams/{sid}/end", json={"reason": "cancelled"})
    r = tc.post(f"/api/forecast/recurring/streams/{sid}/end", json={"reason": "paid_off"})
    assert r.status_code == 409


def test_end_rejects_invalid_reason(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="X", source="manual", status="active",
                        payee_pattern="X", amount_min=1, amount_max=2, cadence="monthly")
    db.add(s); db.commit()
    sid = str(s.id); db.close()
    r = tc.post(f"/api/forecast/recurring/streams/{sid}/end", json={"reason": "abducted_by_aliens"})
    assert r.status_code == 422


# ─── Patch ──────────────────────────────────────────────────────────────────

def test_patch_changes_label_and_term(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="Old", source="manual", status="active",
                        payee_pattern="X", amount_min=1, amount_max=2, cadence="monthly")
    db.add(s); db.commit(); sid = str(s.id); db.close()

    r = tc.patch(f"/api/forecast/recurring/streams/{sid}", json={
        "label": "New",
        "term_total_occurrences": 24,
    })
    assert r.status_code == 200
    assert r.json()["label"] == "New"
    assert r.json()["term_total_occurrences"] == 24


def test_patch_rejects_dual_term_via_existing_plus_incoming(client):
    """If row has term_end_date and incoming patch adds term_total_occurrences,
    the combined shape violates XOR — must reject."""
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="X", source="manual", status="active",
                        payee_pattern="X", amount_min=1, amount_max=2, cadence="monthly",
                        term_end_date=date(2028, 1, 1))
    db.add(s); db.commit(); sid = str(s.id); db.close()
    r = tc.patch(f"/api/forecast/recurring/streams/{sid}", json={"term_total_occurrences": 12})
    assert r.status_code == 400


# ─── From-transaction convenience ──────────────────────────────────────────

def test_create_from_transaction(client):
    tc, SessionLocal = client
    db = SessionLocal()
    db.add(QBBankTransaction(
        qb_txn_id="txn-abc",
        payee="Phone.com",
        amount=44.94,
        txn_date=date(2025, 11, 10),
        txn_type="Cash",
        account_name="Operating Checking",
    ))
    db.commit(); db.close()

    r = tc.post("/api/forecast/recurring/streams/from-transaction", json={
        "qb_txn_id": "txn-abc",
        "cadence": "monthly",
        "term_total_occurrences": 12,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["payee_pattern"] == "PHONE.COM"
    assert body["account_name"] == "Operating Checking"
    assert body["occurrences_seen"] == 1
    assert len(body["hits"]) == 1
    assert body["hits"][0]["qb_txn_id"] == "txn-abc"


def test_create_from_transaction_missing_txn_404(client):
    tc, _ = client
    r = tc.post("/api/forecast/recurring/streams/from-transaction", json={
        "qb_txn_id": "never-existed",
        "cadence": "monthly",
    })
    assert r.status_code == 404


def test_create_from_transaction_zero_amount_rejected(client):
    tc, SessionLocal = client
    db = SessionLocal()
    db.add(QBBankTransaction(qb_txn_id="zero", payee="Test", amount=0.00, txn_date=date(2025, 1, 1)))
    db.commit(); db.close()
    r = tc.post("/api/forecast/recurring/streams/from-transaction", json={
        "qb_txn_id": "zero", "cadence": "monthly",
    })
    assert r.status_code == 400


def test_create_from_transaction_double_click_rejected(client):
    """Two POSTs with the same qb_txn_id must not produce two phantom streams
    with the same attached hit. Second call returns 409."""
    tc, SessionLocal = client
    db = SessionLocal()
    db.add(QBBankTransaction(qb_txn_id="phone-dbl", payee="Phone.com",
                             amount=44.94, txn_date=date(2025, 11, 10)))
    db.commit(); db.close()
    r1 = tc.post("/api/forecast/recurring/streams/from-transaction", json={
        "qb_txn_id": "phone-dbl", "cadence": "monthly",
    })
    assert r1.status_code == 201
    r2 = tc.post("/api/forecast/recurring/streams/from-transaction", json={
        "qb_txn_id": "phone-dbl", "cadence": "monthly",
    })
    assert r2.status_code == 409


def test_audit_kwargs_use_user_id_and_details_not_actor_id_metadata(client):
    """Regression guard: log_audit_event_sync expects user_id + details kwargs.
    Earlier slice-3 draft used actor_id + metadata which the audit impl silently
    drops (kwargs.get('actor_id') is never read on the new-style branch). This
    test pins down that the router writes the correct kwarg names.

    Auditor flagged this as BLOCK-class — SOC2 evidence gap if not fixed.
    """
    import inspect as _inspect

    from gdx_dispatch.modules.forecasting import router as r
    src = _inspect.getsource(r)
    assert "actor_id=" not in src, "router must call log_audit_event_sync with user_id, not actor_id"
    assert "metadata=" not in src, "router must call log_audit_event_sync with details, not metadata"
    # And the new-style kwargs are present at every call site.
    assert src.count("user_id=") >= 9, "expected ≥9 user_id= sites (one per audit call)"
    assert src.count("details=") >= 9, "expected ≥9 details= sites"


# ─── Unlink hit ─────────────────────────────────────────────────────────────

def test_unlink_hit_decrements_occurrences(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s = RecurringStream(label="X", source="observed", status="suggested",
                        payee_pattern="X", amount_min=1, amount_max=2, cadence="monthly",
                        occurrences_seen=2)
    db.add(s); db.commit()
    h = RecurringStreamHit(stream_id=s.id, qb_txn_id="t1",
                           txn_date=date(2025, 1, 1), amount=1.5, confirmed=False)
    db.add(h); db.commit()
    sid = str(s.id); hid = str(h.id); db.close()

    r = tc.post(f"/api/forecast/recurring/streams/{sid}/hits/{hid}/unlink")
    assert r.status_code == 200

    db2 = SessionLocal()
    assert db2.query(RecurringStreamHit).count() == 0
    refreshed = db2.get(RecurringStream, s.id)
    assert int(refreshed.occurrences_seen) == 1
    db2.close()


def test_unlink_hit_from_wrong_stream_404(client):
    tc, SessionLocal = client
    db = SessionLocal()
    s1 = RecurringStream(label="A", source="manual", status="active",
                         payee_pattern="A", amount_min=1, amount_max=2, cadence="monthly")
    s2 = RecurringStream(label="B", source="manual", status="active",
                         payee_pattern="B", amount_min=1, amount_max=2, cadence="monthly")
    db.add_all([s1, s2]); db.commit()
    h = RecurringStreamHit(stream_id=s1.id, qb_txn_id="t",
                           txn_date=date(2025, 1, 1), amount=1.5)
    db.add(h); db.commit()
    sid2 = str(s2.id); hid = str(h.id); db.close()

    r = tc.post(f"/api/forecast/recurring/streams/{sid2}/hits/{hid}/unlink")
    assert r.status_code == 404


# ─── Detect-now ─────────────────────────────────────────────────────────────

def test_detect_now_returns_counts(client):
    tc, SessionLocal = client
    # seed a 12-month subscription
    db = SessionLocal()
    for i in range(12):
        db.add(QBBankTransaction(
            qb_txn_id=f"phone-{i}",
            payee="Phone.com",
            amount=44.94,
            txn_date=date(2025, 1, 9) + timedelta(days=i * 30),
            txn_type="Cash",
        ))
    db.commit(); db.close()

    r = tc.post("/api/forecast/recurring/detect")
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    assert r.json()["hits_added"] == 12

    r2 = tc.get("/api/forecast/recurring/streams?status=suggested")
    assert r2.json()["total"] == 1


# ─── 404 / bad-id paths ────────────────────────────────────────────────────

def test_get_404_on_missing_stream(client):
    tc, _ = client
    r = tc.get(f"/api/forecast/recurring/streams/{uuid4()}")
    assert r.status_code == 404


def test_bad_uuid_400(client):
    tc, _ = client
    r = tc.get("/api/forecast/recurring/streams/not-a-uuid")
    assert r.status_code == 400

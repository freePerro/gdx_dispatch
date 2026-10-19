"""MCP ``invoices.void`` against a real Session — GDXA-1849 (parent GDXA-1847).

The handler used to call ``db.get("Invoice", ...)`` and return ``{"voided": ...}``
without voiding anything; on a real Session the string entity raised
``NoInspectionAvailable``. Its tests were mock-only and passed on a handler that
changed nothing. Here every call goes through ``invoke_tool`` against SQLite
built from the ORM, and the assertions read back the rows that land: the
status, the balance and the ``invoice_voided`` audit row. Only the Celery
enqueue is patched; it is the boundary to the broker.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.core.mcp_tools.invoices_void  # noqa: F401
from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool
from gdx_dispatch.models.tenant_models import Invoice, Payment

TENANT = "tenant-mcp-void"
ADMIN_CAPS = [("write", "invoice"), ("read", "invoice"), ("admin", "invoices.void")]


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid.uuid4)
    capabilities: list[Any] = field(default_factory=list)
    actor_type: str = "human"
    delegated_by_user_id: str | None = None


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def invoice(db):
    inv = Invoice(
        id=uuid.uuid4(), customer_id=uuid.uuid4(), job_id=uuid.uuid4(),
        invoice_number="INV-MCP-VOID", billing_type="standard",
        subtotal=Decimal("400.00"), tax_amount=Decimal("0"), total=Decimal("400.00"),
        balance_due=Decimal("400.00"), status="sent",
        invoice_date=datetime.now(UTC).date(), public_token=uuid.uuid4().hex,
        company_id=TENANT,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _voided_rows(db, invoice_id):
    return db.query(AuditLog).filter(
        AuditLog.action == "invoice_voided", AuditLog.entity_id == str(invoice_id)
    ).all()


async def _void(db, invoice, principal, **extra):
    with patch("gdx_dispatch.core.invoice_void.enqueue_stale_intent_sweep") as sweep:
        r = await invoke_tool(
            "invoices.void", {"invoice_id": str(invoice.id), **extra},
            principal=principal, db=db, approval_ref="tok",
        )
    return r, sweep


def _assert_untouched(db, invoice, status="sent"):
    db.rollback()
    db.expire_all()
    assert db.get(Invoice, invoice.id).status == status
    assert _voided_rows(db, invoice.id) == []


def test_descriptor_red():
    from gdx_dispatch.core.mcp_tools.invoices_void import DESCRIPTOR
    assert DESCRIPTOR.name == "invoices.void"
    assert DESCRIPTOR.blast_radius == "red"
    assert DESCRIPTOR.approval_required is True
    assert "invoice_id" in DESCRIPTOR.input_schema.get("required", [])


def test_tool_registers():
    assert get_tool("invoices.void") is not None


@pytest.mark.asyncio
async def test_the_void_lands_with_its_actor_and_reason(db, invoice):
    p = _P(capabilities=ADMIN_CAPS)
    r, sweep = await _void(db, invoice, p, reason="duplicate bill")

    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    out = r.result["voided"]
    assert (out["invoice_id"], out["previous_status"], out["status"]) == (
        str(invoice.id), "sent", "void",
    )
    assert out["balance_due"] == 0.0
    assert out["reason"] == "duplicate bill"
    sweep.assert_called_once()

    db.expire_all()
    row = db.get(Invoice, invoice.id)
    assert row.status == "void"
    assert Decimal(str(row.balance_due)) == Decimal("0.00")
    audits = _voided_rows(db, invoice.id)
    assert len(audits) == 1, "the void must leave its trail in the database"
    assert audits[0].user_id == str(p.identity_id), "a money mutation names its actor"
    assert audits[0].details["reason"] == "duplicate bill"


@pytest.mark.asyncio
async def test_an_ai_worker_void_is_recorded_against_the_delegating_user(db, invoice):
    """routers/ai.py mints an ai_worker with delegated_by_user_id = the asker."""
    p = _P(capabilities=ADMIN_CAPS, actor_type="ai_worker", delegated_by_user_id="office-user")
    r, _ = await _void(db, invoice, p)

    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert [a.user_id for a in _voided_rows(db, invoice.id)] == ["office-user"]


@pytest.mark.asyncio
async def test_a_bearer_naming_no_person_is_refused(db, invoice):
    """An MCP bearer's identity_id is a UUID5 of its token — it names nobody."""
    p = _P(capabilities=[("*", "*"), ("admin", "*")], actor_type="ai_worker")
    r, sweep = await _void(db, invoice, p)

    assert r.ok is False
    assert r.error_type == "execution_error"
    assert "acting person" in r.audit_row["error_detail"]
    sweep.assert_not_called()
    _assert_untouched(db, invoice)


@pytest.mark.asyncio
async def test_an_invoice_with_a_live_payment_is_refused(db, invoice):
    db.add(Payment(
        invoice_id=invoice.id, amount=Decimal("400.00"), method="check", company_id=TENANT,
    ))
    invoice.status = "paid"
    db.commit()

    r, sweep = await _void(db, invoice, _P(capabilities=ADMIN_CAPS))

    assert r.ok is False
    assert r.error_type == "execution_error"
    assert "recorded payments" in r.audit_row["error_detail"]
    sweep.assert_not_called()
    _assert_untouched(db, invoice, status="paid")


@pytest.mark.asyncio
async def test_an_already_void_invoice_is_refused_not_reported_voided(db, invoice):
    p = _P(capabilities=ADMIN_CAPS)
    first, _ = await _void(db, invoice, p)
    assert first.ok is True, f"{first.error_type}: {first.error_body}"

    again, sweep = await _void(db, invoice, p)

    assert again.ok is False
    assert "already void" in again.audit_row["error_detail"]
    sweep.assert_not_called()
    assert len(_voided_rows(db, invoice.id)) == 1, "a second call writes no second trail"


@pytest.mark.asyncio
async def test_a_missing_invoice_is_refused(db):
    p = _P(capabilities=ADMIN_CAPS)
    with patch("gdx_dispatch.core.invoice_void.enqueue_stale_intent_sweep"):
        r = await invoke_tool(
            "invoices.void", {"invoice_id": str(uuid.uuid4())},
            principal=p, db=db, approval_ref="tok",
        )
    assert r.ok is False
    assert "not found" in r.audit_row["error_detail"]


@pytest.mark.asyncio
async def test_red_without_admin_denied(db, invoice):
    # Has write cap but NOT admin — red gate denies regardless.
    r, _ = await _void(db, invoice, _P(capabilities=[("write", "invoice"), ("read", "invoice")]))
    assert r.ok is False
    assert "capability" in r.error_type.lower()
    _assert_untouched(db, invoice)


@pytest.mark.asyncio
async def test_red_generic_wildcard_no_longer_admin(db, invoice):
    # Security #4: the broad ("*","*") cap that every mcp:invoke token carries
    # must NOT clear the red-tool admin gate — only an explicit admin cap may.
    r, _ = await _void(db, invoice, _P(capabilities=[("*", "*")]))
    assert r.ok is False
    assert "capability" in (r.error_type or "").lower()
    _assert_untouched(db, invoice)


@pytest.mark.asyncio
async def test_red_with_admin_wildcard_voids(db, invoice):
    # A real admin token carries mcp:invoke (→ ("*","*")) AND mcp:admin
    # (→ ("admin","*"), clears the red gate).
    r, _ = await _void(db, invoice, _P(capabilities=[("*", "*"), ("admin", "*")]))
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    db.expire_all()
    assert db.get(Invoice, invoice.id).status == "void"


@pytest.mark.asyncio
@pytest.mark.parametrize("self_approved", [False, True])
async def test_the_unapproved_call_previews_and_voids_nothing(db, invoice, self_approved):
    """invoke_tool runs the handler on the first call to stage the pending
    action. That call must not void, even when the model writes an
    approval_ref into its own arguments."""
    payload = {"invoice_id": str(invoice.id)}
    if self_approved:
        payload["approval_ref"] = "self-made"
    with patch("gdx_dispatch.core.invoice_void.enqueue_stale_intent_sweep") as sweep:
        r = await invoke_tool("invoices.void", payload, principal=_P(capabilities=ADMIN_CAPS), db=db)

    assert r.ok is False
    assert r.error_type == "approval_required"
    assert r.error_body["result"]["preview"]["status"] == "sent"
    sweep.assert_not_called()
    _assert_untouched(db, invoice)

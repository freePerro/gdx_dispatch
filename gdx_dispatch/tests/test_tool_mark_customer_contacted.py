"""Sprint 1.x-S13 — customers.mark_contacted MCP tool contract.

GDXA-1849: the handler set ``last_contacted_at``, which is not a column on
``Customer``, and these tests passed on a MagicMock that accepted it. With no
note the call committed nothing and wrote no trail while returning
``{"ok": True}``. The behaviour tests now run against SQLite built from the
ORM and read back the ``customer_contacted`` audit row and the stored note.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.core.mcp_tools.mark_customer_contacted  # noqa: F401  — register_tool side-effect
from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool
from gdx_dispatch.models.tenant_models import Customer


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


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


def _customer(db, *, notes: str | None = None) -> str:
    row = Customer(id=uuid4(), name="Contact Test", company_id="c1", notes_appended=notes)
    db.add(row)
    db.commit()
    return str(row.id)


def _contacted_rows(db, cid: str):
    return db.query(AuditLog).filter(
        AuditLog.action == "customer_contacted", AuditLog.entity_id == cid
    ).all()


CAPS = [("write", "customer.contact"), ("read", "customer")]


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.mark_customer_contacted import DESCRIPTOR
    assert DESCRIPTOR.name == "customers.mark_contacted"
    assert DESCRIPTOR.blast_radius == "green"
    assert DESCRIPTOR.approval_required is False
    # Cap shape: write on customer.contact (matches the S2 column whitelist).
    caps = [tuple(c) for c in DESCRIPTOR.capabilities_required]
    assert ("write", "customer.contact") in caps
    # Required input field: customer_id.
    schema = DESCRIPTOR.input_schema
    assert "customer_id" in schema.get("required", [])


def test_tool_registers_at_import():
    entry = get_tool("customers.mark_contacted")
    assert entry is not None
    desc, handler = entry
    assert desc.name == "customers.mark_contacted"
    assert callable(handler)


@pytest.mark.asyncio
async def test_marks_customer_contacted_on_success(db):
    cid = _customer(db)
    p = _Principal(capabilities=CAPS)
    r = await invoke_tool("customers.mark_contacted", {"customer_id": cid}, principal=p, db=db)

    assert r.ok is True, f"expected ok=True, got {r.error_type}: {r.error_body}"
    assert r.result["customer_id"] == cid
    assert r.result["note_appended"] is False
    db.expire_all()
    rows = _contacted_rows(db, cid)
    assert len(rows) == 1, "the contact is the audit row; without it nothing was recorded"
    assert rows[0].user_id == str(p.identity_id)


@pytest.mark.asyncio
async def test_appends_note_when_supplied(db):
    cid = _customer(db, notes="Prior note.")
    p = _Principal(capabilities=CAPS)
    r = await invoke_tool(
        "customers.mark_contacted",
        {"customer_id": cid, "note": "Followup scheduled."},
        principal=p, db=db,
    )
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    db.expire_all()
    stored = db.get(Customer, UUID(cid)).notes_appended
    # The previous note content is preserved (append, not replace).
    assert stored == "Prior note.\n\nFollowup scheduled."
    assert _contacted_rows(db, cid)[0].details["note_appended"] is True


@pytest.mark.asyncio
async def test_capability_denied_without_write_cap(db):
    cid = _customer(db)
    p = _Principal(capabilities=[("read", "customer")])  # read-only — no write cap
    r = await invoke_tool("customers.mark_contacted", {"customer_id": cid}, principal=p, db=db)
    assert r.ok is False
    assert r.error_type and "capability" in r.error_type.lower()
    assert _contacted_rows(db, cid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [str(uuid4()), "not-a-uuid"])
async def test_unknown_customer_is_refused_and_records_nothing(db, raw):
    """When the customer doesn't exist, the tool must NOT silently no-op."""
    p = _Principal(capabilities=CAPS)
    r = await invoke_tool("customers.mark_contacted", {"customer_id": raw}, principal=p, db=db)
    assert r.ok is False
    assert r.error_type == "execution_error"
    assert db.query(AuditLog).filter(AuditLog.action == "customer_contacted").count() == 0

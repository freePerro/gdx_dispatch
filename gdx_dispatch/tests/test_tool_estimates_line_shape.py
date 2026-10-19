"""Response shape of every estimate-line dict the estimate MCP tools return.

GDXA-1683 moved the hand-written line dicts in estimates.get,
estimates.update_line and estimates.add_line onto one helper,
``estimate_line_dict``. These tests pin each tool's output — keys, key order and
values — against real SQLite rows, so the extraction (or a later edit to the
helper) cannot change what the assistant sees without going red. Written and
run green against the pre-extraction code first.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import gdx_dispatch.core.mcp_tools.estimates_add_line  # noqa: F401
import gdx_dispatch.core.mcp_tools.estimates_get  # noqa: F401
import gdx_dispatch.core.mcp_tools.estimates_update_line  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

TENANT = "00000000-0000-0000-0000-000000000001"


@dataclass
class _Principal:
    tenant_id: str = TENANT
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _estimate(db, *lines: tuple[str, int, str, int]):
    est = Estimate(
        estimate_number="EST-000042",
        status="draft",
        company_id=TENANT,
        public_token=uuid4().hex,
        total=Decimal("0.00"),
    )
    db.add(est)
    db.flush()
    rows = []
    for desc, qty, price, sort in lines:
        unit = Decimal(price)
        row = EstimateLine(
            estimate_id=est.id,
            description=desc,
            quantity=qty,
            unit_price=unit,
            line_total=(unit * qty).quantize(Decimal("0.01")),
            sort_order=sort,
            company_id=TENANT,
        )
        db.add(row)
        rows.append(row)
        est.total = Decimal(str(est.total)) + row.line_total
    db.commit()
    return est, rows


def _pin(actual: dict, expected: dict) -> None:
    assert actual == expected
    # json.dumps keeps insertion order at every depth, so this also pins the
    # key order the assistant sees in nested dicts like before/after.
    assert json.dumps(actual) == json.dumps(expected), "key order changed"


@pytest.mark.asyncio
async def test_get_line_shape(db):
    est, (a, b) = _estimate(db, ("Spring", 3, "19.99", 2), ("Cable", 1, "12.50", 1))
    r = await invoke_tool(
        "estimates.get",
        {"estimate_id": str(est.id)},
        principal=_Principal(capabilities=[("read", "estimate")]),
        db=db,
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    lines = r.result["estimate"]["lines"]
    assert len(lines) == 2
    _pin(lines[0], {
        "id": str(b.id),
        "description": "Cable",
        "quantity": 1,
        "unit_price": 12.5,
        "line_total": 12.5,
        "sort_order": 1,
    })
    _pin(lines[1], {
        "id": str(a.id),
        "description": "Spring",
        "quantity": 3,
        "unit_price": 19.99,
        "line_total": 59.97,
        "sort_order": 2,
    })
    assert type(lines[1]["quantity"]) is int
    assert type(lines[1]["unit_price"]) is float
    assert type(lines[1]["line_total"]) is float


@pytest.mark.asyncio
async def test_update_line_preview_shape(db):
    _est, (line,) = _estimate(db, ("Spring", 3, "19.99", 1))
    r = await invoke_tool(
        "estimates.update_line",
        {"line_id": str(line.id), "quantity": 4, "unit_price": 20.0},
        principal=_Principal(capabilities=[("write", "estimate")]),
        db=db,
    )
    # A yellow tool without approval_ref is answered by the invoker, not the handler.
    assert r.ok is False and r.error_type == "approval_required"

    from gdx_dispatch.core.mcp_tools.estimates_update_line import handler

    out = await handler(None, db, line_id=str(line.id), quantity=4, unit_price=20.0)
    _pin(out, {
        "line": {
            "preview": True,
            "id": str(line.id),
            "before": {
                "description": "Spring",
                "quantity": 3,
                "unit_price": 19.99,
                "line_total": 59.97,
            },
            "after": {
                "description": "Spring",
                "quantity": 4,
                "unit_price": 20.0,
                "line_total": 80.0,
            },
        }
    })
    db.expire_all()
    assert db.get(EstimateLine, line.id).quantity == 3, "preview must not write"


@pytest.mark.asyncio
async def test_update_line_commit_shape(db):
    est, (line,) = _estimate(db, ("Spring", 3, "19.99", 1))
    r = await invoke_tool(
        "estimates.update_line",
        {"line_id": str(line.id), "description": "Torsion spring", "quantity": 2,
         "approval_ref": "ok"},
        principal=_Principal(capabilities=[("write", "estimate")]),
        db=db,
        approval_ref="ok",
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    _pin(r.result["line"], {
        "preview": False,
        "id": str(line.id),
        "description": "Torsion spring",
        "quantity": 2,
        "unit_price": 19.99,
        "line_total": 39.98,
        "estimate_total": 39.98,
    })
    db.expire_all()
    assert db.get(Estimate, est.id).total == Decimal("39.98")


@pytest.mark.asyncio
async def test_add_line_preview_shape(db):
    est, _ = _estimate(db, ("Spring", 1, "10.00", 1))
    from gdx_dispatch.core.mcp_tools.estimates_add_line import handler

    out = await handler(
        None, db, estimate_id=str(est.id), description=" Opener ", quantity=2,
        unit_price=149.995,
    )
    _pin(out["line"], {
        "preview": True,
        "estimate_id": str(est.id),
        "estimate_number": "EST-000042",
        "description": "Opener",
        "quantity": 2,
        "unit_price": 150.0,
        "line_total": 300.0,
    })


@pytest.mark.asyncio
async def test_add_line_commit_shape(db):
    est, _ = _estimate(db, ("Spring", 1, "10.00", 1))
    r = await invoke_tool(
        "estimates.add_line",
        {"estimate_id": str(est.id), "description": "Opener", "quantity": 2,
         "unit_price": 149.99, "approval_ref": "ok"},
        principal=_Principal(capabilities=[("write", "estimate")]),
        db=db,
        approval_ref="ok",
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    line = r.result["line"]
    new = db.query(EstimateLine).filter(EstimateLine.description == "Opener").one()
    _pin(line, {
        "preview": False,
        "id": str(new.id),
        "estimate_id": str(est.id),
        "description": "Opener",
        "quantity": 2,
        "unit_price": 149.99,
        "line_total": 299.98,
        "sort_order": 2,
        "estimate_total": 309.98,
    })
    assert type(line["quantity"]) is int

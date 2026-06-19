"""catalog MCP tools — descriptor-shape and handler tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.catalog_add_item  # noqa: F401
import gdx_dispatch.core.mcp_tools.catalog_bulk_add_items  # noqa: F401
import gdx_dispatch.core.mcp_tools.catalog_create  # noqa: F401
import gdx_dispatch.core.mcp_tools.catalog_get_item  # noqa: F401
import gdx_dispatch.core.mcp_tools.catalog_list  # noqa: F401
import gdx_dispatch.core.mcp_tools.catalog_update_item  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "00000000-0000-0000-0000-000000000001"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _scalar_result(value):
    s = MagicMock()
    s.scalar_one.return_value = value
    s.scalar_one_or_none.return_value = value
    return s


def _scalars_iter(values):
    s = MagicMock()
    inner = MagicMock()
    inner.__iter__ = lambda self: iter(values)
    s.scalars.return_value = inner
    return s


def test_descriptor_shapes():
    from gdx_dispatch.core.mcp_tools.catalog_add_item import DESCRIPTOR as ADD
    from gdx_dispatch.core.mcp_tools.catalog_bulk_add_items import DESCRIPTOR as BULK
    from gdx_dispatch.core.mcp_tools.catalog_create import DESCRIPTOR as CREATE
    from gdx_dispatch.core.mcp_tools.catalog_get_item import DESCRIPTOR as GET
    from gdx_dispatch.core.mcp_tools.catalog_list import DESCRIPTOR as LIST_
    from gdx_dispatch.core.mcp_tools.catalog_update_item import DESCRIPTOR as UPDATE

    assert CREATE.blast_radius == "green"
    assert ADD.blast_radius == "yellow" and ADD.approval_required is True
    assert UPDATE.blast_radius == "yellow" and UPDATE.approval_required is True
    assert BULK.blast_radius == "yellow" and BULK.approval_required is True
    assert LIST_.blast_radius == "green"
    assert GET.blast_radius == "green"


@pytest.mark.asyncio
async def test_create_dedupes_on_name():
    db = MagicMock()
    existing = SimpleNamespace(id=uuid4(), name="Pricebook", source_system="manual")
    db.execute.return_value = _scalar_result(existing)
    p = _Principal(capabilities=[("write", "catalog")])
    r = await invoke_tool(
        "catalog.create",
        {"name": "Pricebook"},
        principal=p,
        db=db,
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["catalog"]["reused"] is True


@pytest.mark.asyncio
async def test_create_inserts_when_new():
    db = MagicMock()
    db.execute.return_value = _scalar_result(None)
    captured: dict[str, Any] = {}

    def add_side_effect(obj):
        if type(obj).__name__ == "CustomCatalog":
            captured["catalog"] = obj
            obj.id = uuid4()

    db.add.side_effect = add_side_effect
    p = _Principal(capabilities=[("write", "catalog")])
    r = await invoke_tool(
        "catalog.create",
        {"name": "New Book"},
        principal=p,
        db=db,
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["catalog"]["reused"] is False
    assert captured["catalog"].name == "New Book"


@pytest.mark.asyncio
async def test_add_item_preview_then_apply():
    cat_id = uuid4()
    catalog = SimpleNamespace(id=cat_id, name="Pricebook", deleted_at=None)
    db = MagicMock()
    db.get.return_value = catalog
    db.execute.return_value = _scalar_result(None)
    p = _Principal(capabilities=[("write", "catalog")])

    r1 = await invoke_tool(
        "catalog.add_item",
        {"catalog_id": str(cat_id), "name": "Spring", "sku": "SP-100", "price": 49.99},
        principal=p,
        db=db,
    )
    assert r1.ok is False
    assert r1.error_type == "approval_required"

    captured: dict[str, Any] = {}

    def _capture_item(o):
        if type(o).__name__ == "CustomCatalogItem":
            captured["item"] = o
            o.id = uuid4()

    db.add.side_effect = _capture_item

    r2 = await invoke_tool(
        "catalog.add_item",
        {
            "catalog_id": str(cat_id),
            "name": "Spring",
            "sku": "SP-100",
            "price": 49.99,
            "approval_ref": "ok",
        },
        principal=p,
        db=db,
        approval_ref="ok",
    )
    assert r2.ok is True, f"unexpected: {r2.error_type} {r2.error_body}"
    assert r2.result["item"]["preview"] is False
    assert captured["item"].name == "Spring"
    assert float(captured["item"].price) == 49.99


@pytest.mark.asyncio
async def test_add_item_rejects_duplicate_sku():
    cat_id = uuid4()
    catalog = SimpleNamespace(id=cat_id, name="Pricebook", deleted_at=None)
    existing = SimpleNamespace(id=uuid4())
    db = MagicMock()
    db.get.return_value = catalog
    db.execute.return_value = _scalar_result(existing)
    p = _Principal(capabilities=[("write", "catalog")])
    r = await invoke_tool(
        "catalog.add_item",
        {"catalog_id": str(cat_id), "name": "Spring", "sku": "SP-100", "approval_ref": "ok"},
        principal=p,
        db=db,
        approval_ref="ok",
    )
    assert r.ok is True
    assert "already exists" in r.result["error"]


@pytest.mark.asyncio
async def test_update_item_partial_fields():
    iid = uuid4()
    item = SimpleNamespace(
        id=iid,
        name="Old",
        sku="X",
        description="d",
        cost=10,
        price=20,
        category="cat",
        pricing_category="parts",
        active=True,
        deleted_at=None,
    )
    db = MagicMock()
    db.get.return_value = item
    p = _Principal(capabilities=[("write", "catalog")])
    r = await invoke_tool(
        "catalog.update_item",
        {"item_id": str(iid), "price": 25.5, "approval_ref": "ok"},
        principal=p,
        db=db,
        approval_ref="ok",
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert float(item.price) == 25.5
    # other fields unchanged
    assert item.name == "Old"
    assert item.sku == "X"


@pytest.mark.asyncio
async def test_bulk_add_filters_existing_skus():
    cat_id = uuid4()
    catalog = SimpleNamespace(id=cat_id, name="Pricebook", deleted_at=None)
    db = MagicMock()
    db.get.return_value = catalog
    db.execute.return_value = _scalars_iter(["SP-100"])
    p = _Principal(capabilities=[("write", "catalog")])

    r1 = await invoke_tool(
        "catalog.bulk_add_items",
        {
            "catalog_id": str(cat_id),
            "items": [
                {"name": "Spring", "sku": "SP-100", "price": 49.99},
                {"name": "Cable", "sku": "CB-200", "price": 12},
                {"name": "Untagged"},
            ],
        },
        principal=p,
        db=db,
    )
    assert r1.ok is False
    assert r1.error_type == "approval_required"

    inserted: list[Any] = []

    def _capture_bulk(o):
        if type(o).__name__ == "CustomCatalogItem":
            inserted.append(o)
            o.id = uuid4()

    db.add.side_effect = _capture_bulk

    r2 = await invoke_tool(
        "catalog.bulk_add_items",
        {
            "catalog_id": str(cat_id),
            "items": [
                {"name": "Spring", "sku": "SP-100", "price": 49.99},
                {"name": "Cable", "sku": "CB-200", "price": 12},
                {"name": "Untagged"},
            ],
            "approval_ref": "ok",
        },
        principal=p,
        db=db,
        approval_ref="ok",
    )
    assert r2.ok is True, f"unexpected: {r2.error_type} {r2.error_body}"
    assert r2.result["result"]["preview"] is False
    assert r2.result["result"]["inserted"] == 2
    assert r2.result["result"]["skipped_skus"] == ["SP-100"]
    assert len(inserted) == 2


@pytest.mark.asyncio
async def test_bulk_add_rejects_too_many():
    p = _Principal(capabilities=[("write", "catalog")])
    r = await invoke_tool(
        "catalog.bulk_add_items",
        {
            "catalog_id": str(uuid4()),
            "items": [{"name": f"x{i}"} for i in range(501)],
        },
        principal=p,
        db=MagicMock(),
    )
    assert r.ok is False

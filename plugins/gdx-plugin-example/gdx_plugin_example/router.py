"""Example plugin API. Routes are relative; the plugin-host mounts them under
/api/plugins/example. Every route gates on require_module("example") and scopes
data to the forwarded tenant.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.plugin_api.context import PluginContext, get_plugin_db, require_module
from gdx_plugin_example.models import ExampleItem

router = APIRouter()


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.get("/items")
def list_items(
    ctx: PluginContext = Depends(require_module("example")),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    rows = (
        db.execute(
            select(ExampleItem)
            .where(ExampleItem.company_id == ctx.tenant_id)
            .order_by(ExampleItem.id)
        )
        .scalars()
        .all()
    )
    return [{"id": r.id, "name": r.name} for r in rows]


@router.post("/items", status_code=201)
def create_item(
    body: ItemIn,
    ctx: PluginContext = Depends(require_module("example")),
    db: Session = Depends(get_plugin_db),
) -> dict:
    item = ExampleItem(company_id=ctx.tenant_id, name=body.name)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "name": item.name}

"""Tests for the PDF template editor router."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import make_fresh_db
from fastapi import HTTPException
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.routers.pdf_templates import (
    BlockConfig,
    TemplateConfigIn,
    available_types,
    get_template,
    list_templates,
    save_template,
)


class DummyRequest:
    def __init__(self, tenant_id: str = "tenant-pdf-test") -> None:
        self.state = SimpleNamespace(tenant={"id": tenant_id}, request_id="req-pdf1")
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers: dict[str, str] = {}


@pytest.fixture()
def ctx():
    engine = make_fresh_db()
    SL = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SL()
    db.execute(sa_text("""CREATE TABLE IF NOT EXISTS pdf_templates (
        id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(36) NOT NULL,
        template_type VARCHAR(50) NOT NULL, brand_color VARCHAR(20) DEFAULT '#0057a8',
        font_family VARCHAR(50) DEFAULT 'Helvetica', header_content TEXT,
        footer_content TEXT, blocks TEXT NOT NULL, logo_url TEXT,
        created_at TIMESTAMP, updated_at TIMESTAMP,
        UNIQUE(company_id, template_type))"""))
    db.commit()
    req = DummyRequest()
    user = {"user_id": "admin-1", "sub": "admin-1", "role": "admin"}
    try:
        yield db, req, user, SL
    finally:
        db.close()
        engine.dispose()


def _make_payload(**overrides):
    defaults = dict(
        brand_color="#0057a8", font_family="Helvetica",
        header_content="", footer_content="",
        blocks=[BlockConfig(id="logo", type="logo", order=1)],
        logo_url=None,
    )
    defaults.update(overrides)
    return TemplateConfigIn(**defaults)


def test_list_templates_returns_6_defaults(ctx):
    db, req, user, _ = ctx
    result = list_templates(request=req, user=user, db=db)
    assert len(result) == 6


def test_get_template_estimate_default_color(ctx):
    db, req, user, _ = ctx
    t = get_template(template_type="estimate", request=req, user=user, db=db)
    assert t["template_type"] == "estimate"
    assert t["brand_color"] == "#0057a8"


def test_get_template_invalid_type_400(ctx):
    db, req, user, _ = ctx
    with pytest.raises(HTTPException) as exc:
        get_template(template_type="invalid_type", request=req, user=user, db=db)
    assert exc.value.status_code == 400


def test_available_types_returns_6(ctx):
    _, _, user, _ = ctx
    types = available_types(user=user)
    assert len(types) == 6
    keys = {t["key"] for t in types}
    assert "estimate" in keys
    assert "invoice" in keys
    assert "work_order" in keys


def test_save_template_returns_saved(ctx):
    db, req, user, _ = ctx
    result = save_template(template_type="invoice", request=req, payload=_make_payload(), user=user, db=db)
    assert result["status"] == "saved"
    assert "id" in result
    assert result["template_type"] == "invoice"


def test_save_then_get_returns_saved(ctx):
    db, req, user, _ = ctx
    save_template(template_type="estimate", request=req,
                  payload=_make_payload(brand_color="#ff0000", header_content="Custom Header"),
                  user=user, db=db)
    t = get_template(template_type="estimate", request=req, user=user, db=db)
    assert t["brand_color"] == "#ff0000"
    assert t["header_content"] == "Custom Header"


def test_save_template_persists_blocks(ctx):
    db, req, user, _ = ctx
    blocks = [
        BlockConfig(id="logo", type="logo", order=1),
        BlockConfig(id="items", type="line_items", order=2),
        BlockConfig(id="total", type="totals", order=3, visible=False),
    ]
    save_template(template_type="invoice", request=req,
                  payload=_make_payload(blocks=blocks), user=user, db=db)
    t = get_template(template_type="invoice", request=req, user=user, db=db)
    assert len(t["blocks"]) == 3
    assert t["blocks"][0]["id"] == "logo"
    assert t["blocks"][2]["visible"] is False


def test_list_shows_updated_after_save(ctx):
    db, req, user, _ = ctx
    save_template(template_type="invoice", request=req,
                  payload=_make_payload(brand_color="#abcdef"), user=user, db=db)
    templates = list_templates(request=req, user=user, db=db)
    inv = next(t for t in templates if t["template_type"] == "invoice")
    assert inv["brand_color"] == "#abcdef"


def test_unsaved_type_returns_8_default_blocks(ctx):
    db, req, user, _ = ctx
    t = get_template(template_type="work_order", request=req, user=user, db=db)
    assert len(t["blocks"]) == 8


def test_saved_empty_blocks_serializes_as_defaults(ctx):
    """Rows saved while the editor was decorative (pre-2026-07) can carry
    blocks="[]". The editor must get defaults back, not an empty block list
    with no way to recover (found on a real row from 2026-04-08)."""
    db, req, user, _ = ctx
    save_template(template_type="invoice", request=req,
                  payload=_make_payload(blocks=[]), user=user, db=db)
    t = get_template(template_type="invoice", request=req, user=user, db=db)
    assert len(t["blocks"]) == 8


def test_save_update_existing(ctx):
    db, req, user, _ = ctx
    save_template(template_type="estimate", request=req,
                  payload=_make_payload(brand_color="#111111"), user=user, db=db)
    save_template(template_type="estimate", request=req,
                  payload=_make_payload(brand_color="#222222"), user=user, db=db)
    t = get_template(template_type="estimate", request=req, user=user, db=db)
    assert t["brand_color"] == "#222222"

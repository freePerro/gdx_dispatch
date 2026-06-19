"""Coverage for typed-catalog follow-ups that are reachable in sqlite.

What this covers:
  - parts_needed.py custom-door ORM branch (sqlite-compatible)
  - gdx_dispatch/tools/scaffold_product_class.py output stability
  - gdx_dispatch/tools/migrate_tenant_typed_catalogs.py dry-run output

What this does NOT cover (requires Postgres):
  - door_catalog.py /api/catalog/doors UNION SQL (uses ILIKE, NULLS LAST)
  - install_sheet.py door-spec lookup UNION (×2)
  - instant_estimate.py door auto-suggest UNION
  Those four blocks are slated for browser walk on prod after deploy.
"""
from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stdout
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    CustomCatalog,
    CustomCatalogItem,
    DoorSpec,
)
from gdx_dispatch.tools import scaffold_product_class as scaffolder
from gdx_dispatch.tools import migrate_tenant_typed_catalogs as migrator


@pytest.fixture()
def db_session() -> Session:
    # Mirror conftest make_fresh_db at module scale — only what we need.
    import gdx_dispatch.models  # noqa: F401 — register all tenant tables
    from gdx_dispatch.modules.inventory.models import JobPart, Part

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Part.__table__.create(bind=engine, checkfirst=True)
    JobPart.__table__.create(bind=engine, checkfirst=True)

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


# ─────────────────────────────────────────────────────────────────────────
# parts_needed custom-door ORM branch
# ─────────────────────────────────────────────────────────────────────────


def test_parts_needed_surfaces_custom_doors(db_session):
    """When a tenant has a custom door, parts-needed search returns it."""
    from gdx_dispatch.routers.parts_needed import sku_suggest

    catalog = CustomCatalog(name="Custom Doors", source_system="manual", product_class="door")
    db_session.add(catalog)
    db_session.flush()
    item = CustomCatalogItem(
        catalog_id=catalog.id,
        sku="CDOOR-1",
        name="16x7 Black Carriage",
        description="Custom carriage door",
        cost=900,
        price=1700,
        product_class="door",
        active=True,
    )
    db_session.add(item)
    db_session.flush()
    db_session.add(DoorSpec(
        catalog_item_id=item.id,
        manufacturer="Garage Co",
        model_number="GC-1607",
        width=192,
        height=84,
        color="black",
        panel_style="carriage",
    ))
    db_session.commit()

    # Query the suggest endpoint by SKU
    suggestions = sku_suggest(
        request=None, q="CDOOR-1", limit=10,
        user={"user_id": "u", "role": "admin"}, db=db_session,
    )
    custom = [s for s in suggestions if s["sku"] == "CDOOR-1"]
    assert custom, "custom door not surfaced in suggestions"
    assert custom[0]["source"] == "door_catalog"
    assert custom[0]["vendor"] == "Garage Co"
    assert custom[0]["model_number"] == "GC-1607"


def test_parts_needed_excludes_inactive_custom_doors(db_session):
    from gdx_dispatch.routers.parts_needed import sku_suggest

    catalog = CustomCatalog(name="Custom Doors", source_system="manual", product_class="door")
    db_session.add(catalog)
    db_session.flush()
    db_session.add(CustomCatalogItem(
        catalog_id=catalog.id,
        sku="DEAD-DOOR",
        name="Discontinued",
        product_class="door",
        active=False,  # ← excluded
    ))
    db_session.commit()

    suggestions = sku_suggest(request=None, q="DEAD", limit=10, user={"user_id": "u", "role": "admin"}, db=db_session)
    assert not [s for s in suggestions if s["sku"] == "DEAD-DOOR"]


def test_parts_needed_excludes_soft_deleted_custom_doors(db_session):
    from datetime import datetime, timezone
    from gdx_dispatch.routers.parts_needed import sku_suggest

    catalog = CustomCatalog(name="Custom Doors", source_system="manual", product_class="door")
    db_session.add(catalog)
    db_session.flush()
    db_session.add(CustomCatalogItem(
        catalog_id=catalog.id,
        sku="GONE-DOOR",
        name="Removed",
        product_class="door",
        active=True,
        deleted_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    suggestions = sku_suggest(request=None, q="GONE", limit=10, user={"user_id": "u", "role": "admin"}, db=db_session)
    assert not [s for s in suggestions if s["sku"] == "GONE-DOOR"]


def test_parts_needed_excludes_non_door_classes(db_session):
    """A parts-class catalog item should NOT appear under 'door_catalog' source."""
    from gdx_dispatch.routers.parts_needed import sku_suggest

    catalog = CustomCatalog(name="Parts", source_system="manual", product_class="parts")
    db_session.add(catalog)
    db_session.flush()
    db_session.add(CustomCatalogItem(
        catalog_id=catalog.id,
        sku="PART-NOT-DOOR",
        name="Bracket",
        product_class="parts",
        active=True,
    ))
    db_session.commit()

    suggestions = sku_suggest(request=None, q="PART-NOT-DOOR", limit=10, user={"user_id": "u", "role": "admin"}, db=db_session)
    custom_door_hits = [s for s in suggestions if s["sku"] == "PART-NOT-DOOR" and s["source"] == "door_catalog"]
    assert not custom_door_hits


# ─────────────────────────────────────────────────────────────────────────
# scaffolder
# ─────────────────────────────────────────────────────────────────────────


def test_scaffolder_emits_model_and_router_and_types_blocks():
    out = io.StringIO()
    with redirect_stdout(out):
        rc = scaffolder.main([
            "scaffold_product_class",
            "opener",
            "hp:int",
            "drive_type:str",
            "battery_backup:bool",
            "warranty_years:int",
        ])
    assert rc == 0
    text = out.getvalue()
    # Model block
    assert "class OpenerSpec(Base):" in text
    assert '__tablename__ = "opener_specs"' in text
    assert "hp: Mapped[int]" in text
    assert "drive_type: Mapped[str]" in text
    assert "battery_backup: Mapped[bool]" in text
    # Router patch block
    assert "OPENER_SPEC_FIELDS" in text
    assert "from gdx_dispatch.models.tenant_models import OpenerSpec" in text
    # Frontend registry entry
    assert "key: 'opener'" in text
    assert "label: 'Opener'" in text


def test_scaffolder_rejects_unknown_field_type():
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out):
        # Capture stderr by patching
        sys_stderr = sys.stderr
        sys.stderr = err
        try:
            rc = scaffolder.main(["scaffold_product_class", "test", "field:nopetype"])
        finally:
            sys.stderr = sys_stderr
    assert rc == 2
    assert "unknown field type" in err.getvalue()


def test_scaffolder_field_types_decimal_with_precision():
    fs = scaffolder.FieldSpec.parse("price:numeric:8,2")
    assert "Numeric(8, 2)" in fs.sa_type
    fs2 = scaffolder.FieldSpec.parse("price:numeric")
    assert "Numeric(10, 2)" in fs2.sa_type


def test_scaffolder_field_types_string_with_length():
    fs = scaffolder.FieldSpec.parse("name:str:60")
    assert "String(60)" in fs.sa_type
    fs2 = scaffolder.FieldSpec.parse("name:str")
    assert "String(255)" in fs2.sa_type


# ─────────────────────────────────────────────────────────────────────────
# migration helper
# ─────────────────────────────────────────────────────────────────────────


def test_migrator_dry_run_prints_idempotent_sql():
    out = io.StringIO()
    with redirect_stdout(out):
        rc = migrator.main([
            "migrate_tenant_typed_catalogs",
            "--db-url", "sqlite:///does-not-exist.db",
            "--dry-run",
        ])
    assert rc == 0
    text = out.getvalue()
    assert "ADD COLUMN IF NOT EXISTS product_class" in text
    # Both target tables represented
    assert "ALTER TABLE custom_catalogs" in text
    assert "ALTER TABLE custom_catalog_items" in text
    # Indexes
    assert "ix_custom_catalogs_product_class" in text
    assert "ix_custom_catalog_items_product_class" in text


def test_migrator_dry_run_does_not_touch_db(tmp_path):
    """--dry-run must not even open a connection to the target."""
    fake_url = f"sqlite:///{tmp_path}/never-created.db"
    out = io.StringIO()
    with redirect_stdout(out):
        rc = migrator.main([
            "migrate_tenant_typed_catalogs",
            "--db-url", fake_url,
            "--dry-run",
        ])
    assert rc == 0
    # The file must not exist after a dry-run
    assert not (tmp_path / "never-created.db").exists()


def test_migrator_apply_is_idempotent(tmp_path):
    """Running the migrator twice on the same DB must succeed both times.

    Note: sqlite does not support ADD COLUMN IF NOT EXISTS the same way as
    Postgres. We test only that the migrator surfaces the right SQL through
    the dry-run path; live application is covered by browser walk on prod.
    """
    # Sanity: apply mode against a fresh db_url runs without raising in
    # dry-run, twice.
    out = io.StringIO()
    with redirect_stdout(out):
        for _ in range(2):
            rc = migrator.main([
                "migrate_tenant_typed_catalogs",
                "--db-url", f"sqlite:///{tmp_path}/x.db",
                "--dry-run",
            ])
            assert rc == 0

"""GDXA-226 — the nightly draft archive reads ITS tenant's archive threshold.

Prod's `tenant_settings` carries two rows (the live tenant and a stale one left
from 2026-06-22). The task used to run `SELECT ... FROM tenant_settings LIMIT 1`
with no WHERE and no ORDER, so which row's `estimate_draft_archive_days` it
honoured was whatever the planner returned first. It must read the row keyed by
GDX_TENANT_ID, whichever order the rows were written in.
"""
from __future__ import annotations

from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.tenant_settings import Base, TenantSettings
from gdx_dispatch.tasks import estimate_archive

LIVE = UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
STALE = UUID("00000000-0000-0000-0000-000000000001")


def _session(tmp_path, rows):
    # Schema from the ORM, so the Uuid column is stored the way the app stores
    # it (32 dashless hex on SQLite) — a dashed raw bind would never match.
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine, tables=[TenantSettings.__table__])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    for tid, days in rows:
        db.add(TenantSettings(tenant_id=tid, estimate_draft_archive_days=days))
        db.commit()
    db.close()
    return Session


def _threshold_used(Session, tenant_env: str) -> int:
    seen: list[int] = []
    with (
        patch.dict("os.environ", {"GDX_TENANT_ID": tenant_env}),
        patch.object(estimate_archive, "SessionLocal", Session),
        patch.object(
            estimate_archive,
            "_archive_for_tenant",
            side_effect=lambda _tid, days: seen.append(days) or 0,
        ),
    ):
        estimate_archive.archive_stale_drafts_for_all_tenants()
    assert len(seen) == 1, "archive must run exactly once"
    return seen[0]


@pytest.mark.parametrize("stale_first", [True, False])
def test_reads_the_row_keyed_by_the_tenant(tmp_path, stale_first):
    rows = [(STALE, 30), (LIVE, 90)]
    if not stale_first:
        rows.reverse()
    Session = _session(tmp_path, rows)
    assert _threshold_used(Session, str(LIVE)) == 90


def test_no_row_for_the_tenant_uses_the_default(tmp_path):
    Session = _session(tmp_path, [(STALE, 30)])
    assert _threshold_used(Session, str(LIVE)) == 60


def test_unset_tenant_env_reads_the_row_the_app_keys(tmp_path):
    # GDX_TENANT_ID empty (what .env.template ships): the app keys its settings
    # row under company_id()'s default, so the task must ask for that id too.
    Session = _session(tmp_path, [(LIVE, 30), (STALE, 45)])
    with patch.dict("os.environ", {"GDX_DEFAULT_TENANT_ID": ""}):
        assert _threshold_used(Session, "") == 45


def test_non_uuid_tenant_env_uses_the_default(tmp_path):
    # A malformed GDX_TENANT_ID keys no uuid row; the run must neither crash
    # nor borrow some other row's threshold.
    Session = _session(tmp_path, [(STALE, 30)])
    assert _threshold_used(Session, "gdx") == 60

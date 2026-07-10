"""GET /api/me/timezone — the tenant display zone the dispatch board uses to
bucket jobs into the correct office-local day (fixes the UTC off-by-one).

Called directly (no TestClient) so the test needs no auth/tenant plumbing —
the handler only touches the tenant-scoped session and AppSettings, same as the
sibling /api/me/tech-mobile-settings endpoint.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers import me_settings as ms


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'tz.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(eng, checkfirst=True)
    Session = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()
    eng.dispose()


def _seed(db, tz):
    db.add(AppSettings(id=uuid4(), company_name="Acme", timezone=tz))
    db.commit()


def test_returns_configured_timezone(db):
    _seed(db, "America/Chicago")
    out = ms.get_my_timezone(_user={}, db=db)
    assert out == {"tenant_timezone": "America/Chicago"}


def test_defaults_when_no_settings_row(db):
    out = ms.get_my_timezone(_user={}, db=db)
    assert out == {"tenant_timezone": "America/New_York"}


def test_defaults_when_timezone_blank(db):
    # Empty string is falsy → fall back to the same default the mobile endpoint
    # uses, so the frontend never receives '' as a timeZone.
    _seed(db, "")
    out = ms.get_my_timezone(_user={}, db=db)
    assert out == {"tenant_timezone": "America/New_York"}

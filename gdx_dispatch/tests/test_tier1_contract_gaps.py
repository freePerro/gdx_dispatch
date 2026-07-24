"""Tier 1 contract-gap fixes (docs/design/backend-vue-contract-gaps-2026-07-24.md).

Covers the two backend halves of the tier:
  1.3 — PUT /api/commissions/rules/{id} (edit dialog 404'd forever)
  1.4 — POST /api/settings/branding/logo + public serve route
        (SettingsView posted into the void; logo never persisted)

Direct-invocation style, mirroring tests/test_settings.py.
"""
from __future__ import annotations

import io
from collections.abc import Generator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.models.tenant_models import AppSettings, CommissionRule
from gdx_dispatch.routers import branding_public
from gdx_dispatch.routers import settings as settings_router
from gdx_dispatch.routers.commission import RuleIn, update_rule
from gdx_dispatch.routers.settings import BRANDING_LOGO_RE


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (CommissionRule.__table__, AppSettings.__table__, AuditLog.__table__):
        table.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    return tmp_path


def _admin() -> dict[str, str]:
    return {"user_id": "admin-1", "tenant_id": "tenant-test", "role": "admin"}


def _tech() -> dict[str, str]:
    return {"user_id": "tech-1", "tenant_id": "tenant-test", "role": "technician"}


def _request() -> Request:
    request = Request({"type": "http", "headers": []})
    request.state.tenant = {
        "id": "tenant-test",
        "slug": "tenant-test",
        "subscription_tier": "starter",
        "subscription_status": "trialing",
    }
    return request


def _seed_rule(db: Session, role: str = "technician") -> CommissionRule:
    rule = CommissionRule(
        id=uuid4(),
        company_id="tenant-test",
        role=role,
        parts_pct=Decimal("5"),
        labor_pct=Decimal("2"),
        bonus_per_review=Decimal("10"),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


# ── 1.3 PUT /api/commissions/rules/{id} ────────────────────────────────────


def test_update_rule_roundtrip(db_session: Session):
    rule = _seed_rule(db_session)
    out = update_rule(
        str(rule.id),
        _request(),
        RuleIn(role="technician", parts_pct=7.5, labor_pct=3.25, bonus_per_review=25),
        user=_admin(),
        db=db_session,
    )
    assert out["parts_pct"] == 7.5
    assert out["labor_pct"] == 3.25
    assert out["bonus_per_review"] == 25
    db_session.refresh(rule)
    assert rule.parts_pct == Decimal("7.5")
    assert rule.updated_at is not None


def test_update_rule_can_rename_role(db_session: Session):
    rule = _seed_rule(db_session, role="installer")
    out = update_rule(
        str(rule.id),
        _request(),
        RuleIn(role="senior-installer", parts_pct=5, labor_pct=2, bonus_per_review=10),
        user=_admin(),
        db=db_session,
    )
    assert out["role"] == "senior-installer"


def test_update_rule_unknown_id_404(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        update_rule(
            str(uuid4()),
            _request(),
            RuleIn(role="x", parts_pct=1, labor_pct=1, bonus_per_review=0),
            user=_admin(),
            db=db_session,
        )
    assert exc.value.status_code == 404


def test_update_rule_malformed_id_404(db_session: Session):
    with pytest.raises(HTTPException) as exc:
        update_rule(
            "not-a-uuid",
            _request(),
            RuleIn(role="x", parts_pct=1, labor_pct=1, bonus_per_review=0),
            user=_admin(),
            db=db_session,
        )
    assert exc.value.status_code == 404


def test_update_rule_duplicate_role_409(db_session: Session):
    _seed_rule(db_session, role="installer")
    other = _seed_rule(db_session, role="technician")
    with pytest.raises(HTTPException) as exc:
        update_rule(
            str(other.id),
            _request(),
            RuleIn(role="installer", parts_pct=1, labor_pct=1, bonus_per_review=0),
            user=_admin(),
            db=db_session,
        )
    assert exc.value.status_code == 409


# ── 1.4 branding logo upload + public serve ────────────────────────────────


def _png_bytes() -> bytes:
    try:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        # Smallest valid 1x1 PNG — only used when Pillow is absent, in which
        # case _compress_image passes bytes through untouched anyway.
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
            "0000000c49444154789c63f8cfc00000030101"
            "00c9fe92ef0000000049454e44ae426082"
        )


def _upload(name: str = "logo.png", ct: str = "image/png", data: bytes | None = None) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data if data is not None else _png_bytes()),
        filename=name,
        headers=Headers({"content-type": ct}),
    )


def test_upload_logo_persists_and_serves(db_session: Session, upload_dir):
    out = settings_router.upload_branding_logo(
        request=_request(), logo=_upload(), current_user=_admin(), db=db_session
    )
    assert out["logo_url"].startswith("/api/settings/branding/logo/branding-logo-")
    stored = out["logo_url"].rsplit("/", 1)[-1]
    assert BRANDING_LOGO_RE.match(stored)
    assert (upload_dir / stored).is_file()

    # The DB row now points at the serving route
    row = db_session.query(AppSettings).first()
    assert row.logo == out["logo_url"]

    # And the public route serves exactly that file
    resp = branding_public.serve_branding_logo(stored)
    assert str(resp.path) == str(upload_dir / stored)
    assert resp.media_type in ("image/png", "image/jpeg")


def test_upload_logo_replaces_previous_file(db_session: Session, upload_dir):
    first = settings_router.upload_branding_logo(
        request=_request(), logo=_upload(), current_user=_admin(), db=db_session
    )
    first_name = first["logo_url"].rsplit("/", 1)[-1]
    assert (upload_dir / first_name).is_file()

    second = settings_router.upload_branding_logo(
        request=_request(), logo=_upload(), current_user=_admin(), db=db_session
    )
    second_name = second["logo_url"].rsplit("/", 1)[-1]
    assert second_name != first_name
    assert (upload_dir / second_name).is_file()
    assert not (upload_dir / first_name).exists()


def test_upload_logo_requires_admin(db_session: Session, upload_dir):
    with pytest.raises(HTTPException) as exc:
        settings_router.upload_branding_logo(
            request=_request(), logo=_upload(), current_user=_tech(), db=db_session
        )
    assert exc.value.status_code == 403


def test_upload_logo_rejects_non_image(db_session: Session, upload_dir):
    with pytest.raises(HTTPException) as exc:
        settings_router.upload_branding_logo(
            request=_request(),
            logo=_upload(name="malware.pdf", ct="application/pdf", data=b"%PDF-1.4"),
            current_user=_admin(),
            db=db_session,
        )
    assert exc.value.status_code == 415


def test_serve_logo_refuses_non_minted_names(upload_dir):
    # A real file in the flat upload dir that was NOT minted by the logo
    # route must not be addressable through the public endpoint.
    (upload_dir / "customer-contract.pdf").write_bytes(b"%PDF-1.4 secret")
    for name in (
        "customer-contract.pdf",
        "../etc/passwd",
        "branding-logo-shorthex.png",
        f"branding-logo-{uuid4().hex}.svg",
    ):
        with pytest.raises(HTTPException) as exc:
            branding_public.serve_branding_logo(name)
        assert exc.value.status_code == 404


def test_serve_logo_missing_file_404(upload_dir):
    with pytest.raises(HTTPException) as exc:
        branding_public.serve_branding_logo(f"branding-logo-{uuid4().hex}.png")
    assert exc.value.status_code == 404


# ── PDF logo resolution (audit follow-up: WeasyPrint can't read /api/ URLs) ─


def test_resolve_logo_for_pdf_translates_minted_url(upload_dir):
    from gdx_dispatch.core.branding_logo import LOGO_URL_PREFIX, resolve_logo_for_pdf

    name = f"branding-logo-{uuid4().hex}.png"
    (upload_dir / name).write_bytes(_png_bytes())
    resolved = resolve_logo_for_pdf(f"{LOGO_URL_PREFIX}{name}")
    assert resolved.startswith("file://")
    assert resolved.endswith(name)


def test_resolve_logo_for_pdf_missing_file_blank(upload_dir):
    from gdx_dispatch.core.branding_logo import LOGO_URL_PREFIX, resolve_logo_for_pdf

    assert resolve_logo_for_pdf(f"{LOGO_URL_PREFIX}branding-logo-{uuid4().hex}.png") == ""


def test_resolve_logo_for_pdf_passthrough():
    from gdx_dispatch.core.branding_logo import resolve_logo_for_pdf

    assert resolve_logo_for_pdf("https://cdn.example.com/logo.png") == "https://cdn.example.com/logo.png"
    assert resolve_logo_for_pdf("") == ""
    assert resolve_logo_for_pdf(None) == ""


# ── HTTP wiring (audit follow-up: direct-invocation tests can't catch a
#    path typo in the decorator — the bug class being fixed is literally
#    "frontend calls a route that isn't wired") ─────────────────────────────


def _make_wired_app(db: Session):
    from fastapi import FastAPI

    from gdx_dispatch.core.auth import get_current_user as core_gcu
    from gdx_dispatch.core.database import get_db as core_get_db
    from gdx_dispatch.routers import commission as commission_router
    from gdx_dispatch.routers.auth import get_current_user as routers_gcu

    app = FastAPI()

    @app.middleware("http")
    async def _tenant_shim(request, call_next):
        request.state.tenant = {
            "id": "tenant-test",
            "slug": "tenant-test",
            "subscription_tier": "starter",
            "subscription_status": "trialing",
        }
        request.state.current_user = _admin()
        return await call_next(request)

    # Same relative order as app.py: the public branding router before the
    # admin-gated settings router.
    app.include_router(branding_public.router)
    app.include_router(settings_router.router)
    app.include_router(commission_router.router)

    app.dependency_overrides[core_get_db] = lambda: db
    app.dependency_overrides[routers_gcu] = _admin
    app.dependency_overrides[core_gcu] = _admin
    return app


def test_http_put_commission_rule_wired(db_session: Session):
    from fastapi.testclient import TestClient

    rule = _seed_rule(db_session, role="technician")
    client = TestClient(_make_wired_app(db_session))
    # The exact JSON CommissionsView.saveRate sends.
    body = {"role": "technician", "parts_pct": 8, "labor_pct": 4, "bonus_per_review": 15}
    r = client.put(f"/api/commissions/rules/{rule.id}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["parts_pct"] == 8.0
    db_session.refresh(rule)
    assert rule.parts_pct == Decimal("8")


def test_http_logo_upload_and_public_serve(db_session: Session, upload_dir):
    from fastapi.testclient import TestClient

    client = TestClient(_make_wired_app(db_session))
    # Multipart field name must be "logo" — what SettingsView appends.
    r = client.post(
        "/api/settings/branding/logo",
        files={"logo": ("logo.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 200, r.text
    logo_url = r.json()["logo_url"]
    assert logo_url.startswith("/api/settings/branding/logo/branding-logo-")

    served = client.get(logo_url)
    assert served.status_code == 200
    assert served.headers["content-type"] in ("image/png", "image/jpeg")
    assert len(served.content) > 0

    missing = client.get(f"/api/settings/branding/logo/branding-logo-{uuid4().hex}.png")
    assert missing.status_code == 404

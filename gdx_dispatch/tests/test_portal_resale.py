"""Contractor resale quotes — the portal routes (routers/portal_resale.py).

A contractor or wholesale portal customer agrees to the privacy disclaimer,
sets up their brand, and resells one of their visible estimates as a frozen,
marked-up PDF under their own name. Retail customers cannot reach it; another
customer's quote is a 404; our books, invoices and the estimate are never
written; and the audit trail says who did what without carrying their money.
"""
from __future__ import annotations

import io
import json
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models  # noqa: F401  (registers every model on TenantBase)
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import AppSettings, Customer, PdfTemplate
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.modules.reseller import service
from gdx_dispatch.modules.reseller.models import ResaleQuote, ResellerProfile
from gdx_dispatch.routers.portal import PortalPrincipal, get_current_portal_customer
from gdx_dispatch.routers.portal import router as portal_router
from gdx_dispatch.routers.portal_resale import portal_router as resale_router

TENANT = "tenant-test"
_CUST_A = UUID("00000000-0000-0000-0000-0000000000c1")
_CUST_B = UUID("00000000-0000-0000-0000-0000000000c2")
_CUST_RETAIL = UUID("00000000-0000-0000-0000-0000000000c3")
_USER_A = UUID("00000000-0000-0000-0000-0000000000d1")
_USER_B = UUID("00000000-0000-0000-0000-0000000000d2")
_USER_R = UUID("00000000-0000-0000-0000-0000000000d3")

OUR_NAME = "Ourco Overhead Doors"
OUR_ADDRESS = "77 Shop Road"
OUR_TERMS = "OURCO STANDARD TERMS"
OUR_HEADER = "OURCO HEADER BAND"
OUR_FOOTER = "OURCO FOOTER BAND"


def _png(size=(4, 4), color=(200, 20, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (8, 8), (10, 120, 10))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    exif[0x0131] = "GPS-LEAK-SOFTWARE"  # Software
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


class _Ctx:
    def __init__(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        TenantBase.metadata.create_all(self.engine, checkfirst=True)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        s = self.Session()
        s.execute(text(
            "CREATE TABLE IF NOT EXISTS company_module_grants (id TEXT PRIMARY KEY, "
            "company_id TEXT, module_key TEXT, granted_at TEXT, created_at TEXT, expires_at TEXT)"
        ))
        s.execute(
            text(
                "INSERT INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
                "VALUES ('cmg-portal', :tid, 'customer_portal', datetime('now'), datetime('now'))"
            ),
            {"tid": TENANT},
        )
        s.add(Customer(id=_CUST_A, name="Acme Builders", pricing_class="contractor", company_id=TENANT))
        s.add(Customer(id=_CUST_B, name="Bulk Wholesale", pricing_class="wholesale", company_id=TENANT))
        s.add(Customer(id=_CUST_RETAIL, name="Rita Retail", pricing_class="retail", company_id=TENANT))
        s.add(AppSettings(company_name=OUR_NAME, address=OUR_ADDRESS,
                          logo="/api/settings/branding/logo/branding-logo-" + "a" * 32 + ".png"))
        s.add(PdfTemplate(id="tpl-estimate", company_id=TENANT, template_type="estimate",
                          header_content=OUR_HEADER, footer_content=OUR_FOOTER,
                          brand_color="#ff0000", font_family="Georgia", blocks="[]"))
        s.commit()
        self.est_a = self._estimate(s, _CUST_A, "EST-000777", status="sent")
        self.draft_a = self._estimate(s, _CUST_A, "EST-000778", status="draft")
        self.est_b = self._estimate(s, _CUST_B, "EST-000779", status="sent")
        self.est_r = self._estimate(s, _CUST_RETAIL, "EST-000780", status="sent")
        s.close()
        self.principal = PortalPrincipal(user_id=_USER_A, customer_id=_CUST_A, role="customer")

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()

        @app.middleware("http")
        async def inject_tenant(request, call_next):
            request.state.tenant = {"id": TENANT}
            return await call_next(request)

        app.include_router(resale_router)
        app.include_router(portal_router)
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_portal_customer] = lambda: self.principal
        self.client = TestClient(app, raise_server_exceptions=True)

    @staticmethod
    def _estimate(s, customer_id, number, *, status):
        est = Estimate(
            customer_id=customer_id, estimate_number=number, label="New door", total=2600,
            discount=100, status=status, public_token=number, company_id=TENANT,
            notes="OURCO INTERNAL NOTE", description="OURCO SCOPE TEXT",
        )
        s.add(est)
        s.flush()
        s.add_all([
            EstimateLine(estimate_id=est.id, description="16x7 insulated door", quantity=1,
                         unit_price=2450, line_total=2450, sort_order=1, company_id=TENANT),
            EstimateLine(estimate_id=est.id, description="Haul away", quantity=1,
                         unit_price=150, line_total=150, sort_order=2, company_id=TENANT),
        ])
        s.commit()
        return est.id

    def as_(self, user_id, customer_id):
        self.principal = PortalPrincipal(user_id=user_id, customer_id=customer_id, role="customer")

    def agree(self):
        r = self.client.put("/portal/reseller/profile",
                            json={"accept_disclaimer_version": service.DISCLAIMER_VERSION})
        assert r.status_code == 200, r.text
        return r.json()

    def set_up(self, **over):
        self.agree()
        body = {"company_name": "Acme Builders LLC", "phone": "555-0199", "email": "quotes@acme.example",
                "address": "9 Contractor Way", "website": "acme.example", "license_no": "BC-12345",
                "terms_text": "ACME TERMS: 50% down.", "default_markup_pct": 20}
        body.update(over)
        r = self.client.put("/portal/reseller/profile", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def resell(self, estimate_id=None, **over):
        body = {"reference": "ACME-1", "end_customer_name": "Homeowner Hal",
                "end_customer_address": "5 Elm St", "notes": "Install in May."}
        body.update(over)
        return self.client.post(f"/portal/estimates/{estimate_id or self.est_a}/resale", json=body)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    c = _Ctx()
    yield c
    c.engine.dispose()


def _audit_rows(ctx, action=None):
    s = ctx.Session()
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action == action)
    rows = s.execute(q).scalars().all()
    s.close()
    return rows


def _details(row) -> dict:
    return row.details if isinstance(row.details, dict) else json.loads(row.details or "{}")


def _pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)


# ── Eligibility and the disclaimer ───────────────────────────────────────────


def test_a_retail_customer_gets_403_everywhere_and_context_hides_it(ctx):
    ctx.as_(_USER_R, _CUST_RETAIL)
    assert ctx.client.get("/portal/reseller/profile").status_code == 403
    assert ctx.client.put("/portal/reseller/profile",
                          json={"accept_disclaimer_version": service.DISCLAIMER_VERSION}).status_code == 403
    assert ctx.resell(ctx.est_r).status_code == 403
    assert ctx.client.get("/portal/resale-quotes").status_code == 403
    assert ctx.client.get("/portal/context").json()["reseller"] == {
        "eligible": False, "disclaimer_accepted": False, "set_up": False,
    }


def test_context_reports_eligibility_then_setup(ctx):
    assert ctx.client.get("/portal/context").json()["reseller"] == {
        "eligible": True, "disclaimer_accepted": False, "set_up": False,
    }
    ctx.agree()
    assert ctx.client.get("/portal/context").json()["reseller"]["disclaimer_accepted"] is True
    assert ctx.client.get("/portal/context").json()["reseller"]["set_up"] is False
    ctx.set_up()
    assert ctx.client.get("/portal/context").json()["reseller"]["set_up"] is True


def test_without_the_disclaimer_quotes_and_branding_are_refused(ctx):
    profile = ctx.client.get("/portal/reseller/profile").json()
    assert profile["disclaimer"]["accepted"] is False
    assert profile["disclaimer"]["text"] == service.DISCLAIMER_TEXT
    assert ctx.client.put("/portal/reseller/profile", json={"company_name": "X"}).status_code == 403
    assert ctx.resell().status_code == 403
    assert ctx.client.get("/portal/resale-quotes").status_code == 403
    r = ctx.client.post("/portal/reseller/logo", files={"file": ("l.png", _png(), "image/png")})
    assert r.status_code == 403


def test_a_stale_disclaimer_version_is_refused_and_a_reworded_one_needs_agreeing_again(ctx, monkeypatch):
    r = ctx.client.put("/portal/reseller/profile", json={"accept_disclaimer_version": "not-the-version"})
    assert r.status_code == 409
    ctx.set_up()
    assert ctx.resell().status_code == 201
    monkeypatch.setattr(service, "DISCLAIMER_VERSION", "reworded-000001")
    assert ctx.resell().status_code == 403
    assert ctx.client.get("/portal/reseller/profile").json()["disclaimer"]["accepted"] is False


def test_agreeing_is_audited_with_the_version_and_who(ctx):
    ctx.agree()
    ctx.agree()  # idempotent: one acceptance row, not two
    rows = _audit_rows(ctx, "reseller_disclaimer_accepted")
    assert len(rows) == 1
    assert rows[0].user_id == f"portal:{_USER_A}"
    assert _details(rows[0])["version"] == service.DISCLAIMER_VERSION
    s = ctx.Session()
    p = s.execute(select(ResellerProfile)).scalar_one()
    assert p.disclaimer_accepted_by == _USER_A and p.disclaimer_accepted_at is not None
    s.close()


# ── Profile ──────────────────────────────────────────────────────────────────


def test_profile_saves_and_audits_field_names_only(ctx):
    out = ctx.set_up()
    assert out["company_name"] == "Acme Builders LLC" and out["default_markup_pct"] == 20.0
    rows = _audit_rows(ctx, "reseller_profile_updated")
    assert len(rows) == 1
    details = _details(rows[0])
    assert "default_markup_pct" in details["fields"]
    blob = json.dumps(details)
    assert "20" not in blob.replace(str(_CUST_A), "") and "ACME TERMS" not in blob


def test_markup_bounds_are_enforced(ctx):
    ctx.agree()
    assert ctx.client.put("/portal/reseller/profile", json={"default_markup_pct": -1}).status_code == 422
    assert ctx.client.put("/portal/reseller/profile", json={"default_markup_pct": 501}).status_code == 422
    assert ctx.resell(markup_pct=900).status_code == 422


# ── Logo upload ──────────────────────────────────────────────────────────────


def test_logo_is_reencoded_without_exif_and_served_only_to_its_owner(ctx, tmp_path):
    ctx.set_up()
    r = ctx.client.post("/portal/reseller/logo", files={"file": ("l.jpg", _jpeg_with_exif(), "image/jpeg")})
    assert r.status_code == 201, r.text
    stored = list((tmp_path / "reseller").iterdir())
    assert len(stored) == 1 and stored[0].name.startswith("reseller-logo-")
    raw = stored[0].read_bytes()
    assert b"SecretCam" not in raw and b"GPS-LEAK-SOFTWARE" not in raw
    assert not Image.open(io.BytesIO(raw)).getexif()

    got = ctx.client.get("/portal/reseller/logo")
    assert got.status_code == 200 and got.content == raw
    assert _audit_rows(ctx, "reseller_logo_uploaded")

    ctx.as_(_USER_B, _CUST_B)
    assert ctx.client.get("/portal/reseller/logo").status_code == 404


def test_a_replaced_logo_file_is_removed(ctx, tmp_path):
    ctx.set_up()
    ctx.client.post("/portal/reseller/logo", files={"file": ("a.png", _png(), "image/png")})
    ctx.client.post("/portal/reseller/logo", files={"file": ("b.png", _png(color=(1, 2, 3)), "image/png")})
    assert len(list((tmp_path / "reseller").iterdir())) == 1


@pytest.mark.parametrize("name,data,ctype,status", [
    ("evil.png", b"<?php echo 'hi'; ?>  not an image", "image/png", 422),
    ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>x</script></svg>", "image/svg+xml", 415),
    ("empty.png", b"", "image/png", 400),
])
def test_bad_logos_are_refused_and_nothing_is_stored(ctx, tmp_path, name, data, ctype, status):
    ctx.set_up()
    r = ctx.client.post("/portal/reseller/logo", files={"file": (name, data, ctype)})
    assert r.status_code == status, r.text
    assert not (tmp_path / "reseller").exists() or not list((tmp_path / "reseller").iterdir())
    s = ctx.Session()
    assert s.execute(select(ResellerProfile.logo_file)).scalar_one() is None
    s.close()


# ── Resale quotes ────────────────────────────────────────────────────────────


def test_create_freezes_the_markup_and_lists_it_for_tracking(ctx):
    ctx.set_up()
    r = ctx.resell()  # default markup 20%
    assert r.status_code == 201, r.text
    row = r.json()
    # Ours: 2600 - 100 discount = 2500. Theirs: (2940 + 180) - 120 = 3000.
    assert row["base_subtotal"] == 2500.0 and row["resale_subtotal"] == 3000.0
    assert row["markup_amount"] == 500.0 and row["markup_pct"] == 20.0
    assert row["estimate_number"] == "EST-000777"

    listed = ctx.client.get("/portal/resale-quotes").json()
    assert [q["id"] for q in listed] == [row["id"]]

    # Editing our estimate afterwards does not move a quoted number.
    s = ctx.Session()
    est = s.get(Estimate, ctx.est_a)
    est.discount = 0
    s.commit()
    s.close()
    assert ctx.client.get("/portal/resale-quotes").json()[0]["resale_subtotal"] == 3000.0


def test_explicit_markup_overrides_the_default_and_reference_defaults(ctx):
    ctx.set_up()
    row = ctx.resell(markup_pct=0, reference="").json()
    assert row["resale_subtotal"] == row["base_subtotal"] == 2500.0
    assert row["reference"] == "Q-0001"


def test_a_draft_or_another_customers_estimate_cannot_be_resold(ctx):
    ctx.set_up()
    assert ctx.resell(ctx.draft_a).status_code == 404
    assert ctx.resell(ctx.est_b).status_code == 404


def test_customer_b_cannot_read_or_delete_customer_as_quote(ctx):
    ctx.set_up()
    qid = ctx.resell().json()["id"]
    ctx.as_(_USER_B, _CUST_B)
    ctx.set_up(company_name="Bulk Co")
    assert ctx.client.get("/portal/resale-quotes").json() == []
    assert ctx.client.get(f"/portal/resale-quotes/{qid}/pdf").status_code == 404
    assert ctx.client.delete(f"/portal/resale-quotes/{qid}").status_code == 404
    assert ctx.client.get("/portal/reseller/profile").json()["company_name"] == "Bulk Co"


def test_soft_delete_hides_the_quote_and_keeps_the_row(ctx):
    ctx.set_up()
    qid = ctx.resell().json()["id"]
    assert ctx.client.delete(f"/portal/resale-quotes/{qid}").status_code == 200
    assert ctx.client.get("/portal/resale-quotes").json() == []
    assert ctx.client.get(f"/portal/resale-quotes/{qid}/pdf").status_code == 404
    s = ctx.Session()
    assert s.get(ResaleQuote, UUID(qid)).deleted_at is not None
    s.close()
    assert _audit_rows(ctx, "resale_quote_deleted")


def test_audit_rows_name_who_and_what_but_carry_no_money_or_end_customer(ctx):
    ctx.set_up()
    qid = ctx.resell().json()["id"]
    ctx.client.delete(f"/portal/resale-quotes/{qid}")
    for action in ("resale_quote_created", "resale_quote_deleted"):
        (row,) = _audit_rows(ctx, action)
        assert row.user_id == f"portal:{_USER_A}" and row.entity_id == qid
        details = _details(row)
        assert set(details) == {"customer_id", "estimate_id"}
    for row in _audit_rows(ctx):
        blob = json.dumps(_details(row))
        for leak in ("3000", "2500", "500.0", "Homeowner Hal", "5 Elm St", "ACME-1", "Install in May"):
            assert leak not in blob, (row.action, leak)


def _dump_all_tables(engine, skip):
    out = {}
    with engine.connect() as c:
        for table in inspect(c).get_table_names():
            if table in skip:
                continue
            rows = c.execute(text(f'SELECT * FROM "{table}"')).all()  # noqa: S608 — names from the inspector
            out[table] = sorted(map(repr, rows))
    return out


def test_creating_a_quote_writes_nothing_but_the_quote_and_its_audit_row(ctx):
    """Our books untouched: every other table — invoices, payments, the GL,
    the estimate and its lines — reads byte-for-byte the same afterwards."""
    ctx.set_up()
    skip = {"resale_quotes", "audit_logs"}
    before = _dump_all_tables(ctx.engine, skip)
    assert ctx.resell().status_code == 201
    assert _dump_all_tables(ctx.engine, skip) == before


# ── The PDF ──────────────────────────────────────────────────────────────────


def test_the_pdf_carries_their_brand_and_none_of_ours(ctx):
    ctx.set_up()
    ctx.client.post("/portal/reseller/logo", files={"file": ("l.png", _png((40, 20)), "image/png")})
    qid = ctx.resell().json()["id"]
    r = ctx.client.get(f"/portal/resale-quotes/{qid}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'attachment; filename="quote-ACME-1.pdf"'
    assert r.headers["cache-control"] == "private, no-store"
    body = _pdf_text(r.content)

    for theirs in ("Acme Builders LLC", "9 Contractor Way", "555-0199", "quotes@acme.example",
                   "BC-12345", "ACME TERMS", "Quote #: ACME-1", "Homeowner Hal", "5 Elm St",
                   "Install in May.", "16x7 insulated door", "$2,940.00", "$3,000.00"):
        assert theirs in body, theirs
    for ours in (OUR_NAME, OUR_ADDRESS, OUR_HEADER, OUR_FOOTER, OUR_TERMS, "EST-000777",
                 "OURCO INTERNAL NOTE", "OURCO SCOPE TEXT", "branding-logo-", "Tax", "Down", "Estimate"):
        assert ours not in body, ours
    # An embedded image: their logo made it onto the page.
    assert any(page.images for page in PdfReader(io.BytesIO(r.content)).pages)


def test_the_pdf_terms_are_frozen_with_the_quote(ctx):
    ctx.set_up()
    qid = ctx.resell().json()["id"]
    ctx.client.put("/portal/reseller/profile", json={"terms_text": "NEW TERMS LATER"})
    body = _pdf_text(ctx.client.get(f"/portal/resale-quotes/{qid}/pdf").content)
    assert "ACME TERMS" in body and "NEW TERMS LATER" not in body


def test_a_hidden_prices_estimate_prints_totals_only(ctx):
    s = ctx.Session()
    s.get(Estimate, ctx.est_a).hide_line_prices = True
    s.commit()
    s.close()
    ctx.set_up()
    row = ctx.resell().json()
    assert row["hide_line_prices"] is True
    body = _pdf_text(ctx.client.get(f"/portal/resale-quotes/{row['id']}/pdf").content)
    assert "16x7 insulated door" in body and "$3,000.00" in body
    assert "$2,940.00" not in body and "Unit Price" not in body
    s = ctx.Session()
    snap = s.get(ResaleQuote, UUID(row["id"])).lines_snapshot
    s.close()
    assert all("line_total" not in ln for ln in snap["lines"])


def test_markup_is_stored_as_decimal(ctx):
    ctx.set_up()
    ctx.resell(markup_pct="12.5")
    s = ctx.Session()
    q = s.execute(select(ResaleQuote)).scalar_one()
    assert Decimal(str(q.markup_pct)) == Decimal("12.50")
    s.close()


# ── Review fixes (2026-10-08) ────────────────────────────────────────────────


def test_a_non_ascii_reference_still_downloads(ctx):
    ctx.set_up()
    qid = ctx.resell(reference="Ωmega Müller-12").json()["id"]
    r = ctx.client.get(f"/portal/resale-quotes/{qid}/pdf")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="quote-megaMller-12.pdf"'
    assert "Ωmega Müller-12" in _pdf_text(r.content)


def test_a_failed_logo_save_leaves_no_file_behind(ctx, tmp_path, monkeypatch):
    from gdx_dispatch.routers import portal_resale

    ctx.set_up()

    def _boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(portal_resale, "_audit", _boom)
    with pytest.raises(RuntimeError):
        ctx.client.post("/portal/reseller/logo", files={"file": ("l.png", _png(), "image/png")})
    assert not list((tmp_path / "reseller").iterdir())


def test_two_first_saves_at_once_get_a_409_not_a_500(ctx, monkeypatch):
    ctx.agree()  # the other window's save already created the row
    monkeypatch.setattr(service, "get_profile", lambda db, cid: None)  # this one read before it landed
    r = ctx.client.put("/portal/reseller/profile", json={"accept_disclaimer_version": service.DISCLAIMER_VERSION})
    assert r.status_code == 409

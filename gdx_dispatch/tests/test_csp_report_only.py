"""The report-only CSP allows what the pages actually load (GDXA-777).

The policy said `frame-src 'self' blob:` and `script-src 'self'` plus the
Cloudflare beacon, so every Stripe frame, Stripe.js itself and the pay page's
own inline scripts were violations — the day it is enforced, the public pay
page stops taking money. These drive the real middleware and the real pay
page route, and read the header and the rendered scripts.
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.app import SecurityHeadersMiddleware
from gdx_dispatch.core import customer_page_preview
from gdx_dispatch.core.audit import TenantBase, ensure_audit_table
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.payments import public_router
from gdx_dispatch.models.tenant_models import Invoice

REPORT_ONLY = "Content-Security-Policy-Report-Only"


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


@pytest.fixture
def invoice(db):
    inv = Invoice(
        id=uuid.uuid4(), customer_id=uuid.uuid4(), job_id=uuid.uuid4(),
        invoice_number="INV-CSP", billing_type="standard",
        subtotal=Decimal("200.00"), tax_amount=Decimal("0"), total=Decimal("200.00"),
        balance_due=Decimal("200.00"), status="sent",
        invoice_date=datetime.now(UTC).date(), public_token=uuid.uuid4().hex,
        company_id="tenant-csp",
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


@pytest.fixture
def client(db):
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(public_router)

    @app.get("/spa-page")
    def spa_page() -> dict:
        return {}

    app.dependency_overrides[get_db] = lambda: db
    with patch("stripe.PaymentIntent.list", return_value=SimpleNamespace(data=[], has_more=False)), \
            patch("gdx_dispatch.core.payments.record_customer_view"), TestClient(app) as c:
        yield c


def _directives(header: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in header.split(";"):
        name, *sources = part.split()
        out[name] = sources
    return out


def _inline_script_nonces(html: str) -> list[str | None]:
    """The nonce of every <script> without a src, None where it has none."""
    nonces = []
    for attrs in re.findall(r"<script\b([^>]*)>", html):
        if "src=" in attrs:
            continue
        m = re.search(r'nonce="([^"]*)"', attrs)
        nonces.append(m.group(1) if m else None)
    return nonces


def test_the_pay_page_policy_allows_stripe_js(client, invoice):
    r = client.get(f"/pay/{invoice.public_token}")
    assert r.status_code == 200
    assert 'src="https://js.stripe.com/v3/"' in r.text
    d = _directives(r.headers[REPORT_ONLY])
    for origin in ("https://js.stripe.com", "https://*.js.stripe.com"):
        assert origin in d["script-src"]
        assert origin in d["frame-src"]
    assert "https://hooks.stripe.com" in d["frame-src"]
    assert "https://api.stripe.com" in d["connect-src"]
    # What was there before stays.
    assert "blob:" in d["frame-src"]
    assert "https://static.cloudflareinsights.com" in d["script-src"]


def test_every_inline_script_on_the_pay_page_carries_the_policy_nonce(client, invoice):
    r = client.get(f"/pay/{invoice.public_token}")
    nonce = next(
        s[len("'nonce-"):-1] for s in _directives(r.headers[REPORT_ONLY])["script-src"]
        if s.startswith("'nonce-")
    )
    assert len(nonce) >= 16
    nonces = _inline_script_nonces(r.text)
    assert nonces, "the pay page has inline scripts; the parse found none"
    assert nonces == [nonce] * len(nonces)


def test_the_staff_preview_script_carries_the_nonce_too(client, invoice):
    token = customer_page_preview.mint("invoice", invoice.id)
    r = client.get(f"/pay/{invoice.public_token}", params={"preview": token})
    assert r.status_code == 200
    nonce = next(
        s[len("'nonce-"):-1] for s in _directives(r.headers[REPORT_ONLY])["script-src"]
        if s.startswith("'nonce-")
    )
    nonces = _inline_script_nonces(r.text)
    assert len(nonces) == 2, "preview renders the tab script and the page script"
    assert nonces == [nonce, nonce]


def test_the_nonce_is_fresh_per_request(client, invoice):
    first = client.get(f"/pay/{invoice.public_token}").headers[REPORT_ONLY]
    second = client.get(f"/pay/{invoice.public_token}").headers[REPORT_ONLY]
    assert first != second


def test_stripe_is_only_allowed_on_the_pay_page(client):
    r = client.get("/spa-page")
    header = r.headers[REPORT_ONLY]
    assert "stripe.com" not in header
    assert "'nonce-" not in header


def test_the_spa_policy_allows_the_google_maps_loader(client):
    d = _directives(client.get("/spa-page").headers[REPORT_ONLY])
    # MapsView.vue / MobileTodayView.vue inject maps.googleapis.com/maps/api/js.
    assert "https://*.googleapis.com" in d["script-src"]
    assert "https://*.gstatic.com" in d["img-src"]
    assert "https://fonts.gstatic.com" in d["font-src"]
    assert "blob:" in d["worker-src"]
    assert "'unsafe-eval'" not in d["script-src"]
    assert "'unsafe-inline'" not in d["script-src"]


def test_the_policy_stays_report_only(client, invoice):
    for path in (f"/pay/{invoice.public_token}", "/spa-page"):
        r = client.get(path)
        assert REPORT_ONLY in r.headers
        assert "Content-Security-Policy" not in r.headers

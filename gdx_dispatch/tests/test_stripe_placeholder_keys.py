"""A `.env.template` Stripe placeholder is not a configured Stripe (GDXA-920).

The template ships `STRIPE_SECRET_KEY=sk_test_your-secret-key-here` and the
same shape for the publishable key and webhook secret. `stripe_configured()`
was `bool(os.getenv("STRIPE_SECRET_KEY"))`, so a copied template counted as
configured: the stale-intent sweep skipped its `stripe_unconfigured` refusal,
called Stripe with the placeholder, and logged an AuthenticationError
traceback on every run. `test_celery_stripe_env.py` covered only an absent key.

These read the template itself, so a placeholder added there later is covered
without editing this file.
"""
from __future__ import annotations

import ast
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.payments import public_pay_url, stripe_configured, stripe_env

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / ".env.template"


def _template_stripe_entries() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in TEMPLATE.read_text().splitlines():
        m = re.match(r"^\s*(STRIPE_[A-Z0-9_]+)=(.*)$", line)
        if m and m.group(2).strip():
            out[m.group(1)] = m.group(2).strip()
    return out


def test_the_template_still_ships_the_three_stripe_placeholders():
    """The counterfactual for the parametrized test below: if the template
    stopped carrying them, that test would pass on nothing."""
    assert {"STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET"} <= set(
        _template_stripe_entries()
    )


@pytest.mark.parametrize("name,value", sorted(_template_stripe_entries().items()))
def test_every_template_stripe_placeholder_reads_as_unset(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert stripe_env(name) == "", f"{name}={value} from .env.template reads as a real value"


def test_the_template_secret_key_is_not_configured(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", _template_stripe_entries()["STRIPE_SECRET_KEY"])
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://example.test")
    assert stripe_configured() is False
    # No dead pay link goes out on a placeholder key either.
    assert public_pay_url("tok") is None


# Assembled at runtime: a literal real-shaped key trips GitHub push
# protection (GH013) and declines the push (GDXA-925).
_KEY_BODY = "51Habc0DEF" + "ghiJKLmnoPQRstuVWXyz0123456789"


@pytest.mark.parametrize(
    "name,value",
    [
        ("STRIPE_SECRET_KEY", "sk_" + "test_" + _KEY_BODY),
        ("STRIPE_SECRET_KEY", "sk_" + "live_" + _KEY_BODY),
        ("STRIPE_SECRET_KEY", "rk_" + "live_" + _KEY_BODY),
        ("STRIPE_PUBLISHABLE_KEY", "pk_" + "live_" + _KEY_BODY),
        ("STRIPE_WEBHOOK_SECRET", "whsec_" + "0123456789abcdef" + "ABCDEF0123456789abcdef"),
    ],
)
def test_a_real_shaped_key_still_reads_as_set(monkeypatch, name, value):
    """The other direction: the guard must not refuse a working production key."""
    monkeypatch.setenv(name, value)
    assert stripe_env(name) == value


def test_blank_and_whitespace_read_as_unset(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "   ")
    assert stripe_configured() is False
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    assert stripe_configured() is False


def test_no_stripe_env_read_bypasses_the_helper():
    """Every `STRIPE_*` env read in shipped code goes through `stripe_env`.
    A direct `os.getenv("STRIPE_...")` is how the placeholder slipped past."""
    offenders: list[str] = []
    for path in (REPO / "gdx_dispatch").rglob("*.py"):
        if {"tests", "node_modules", "frontend"} & set(path.parts):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.Call) and node.args:
                fn = ast.unparse(node.func)
                if fn in {"os.getenv", "getenv", "os.environ.get", "environ.get"}:
                    target = node.args[0]
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in {"os.environ", "environ"}:
                target = node.slice
            # `stripe_env` itself reads through a variable, so any literal
            # STRIPE_* read is a bypass wherever it sits.
            if (
                isinstance(target, ast.Constant)
                and isinstance(target.value, str)
                and target.value.startswith("STRIPE_")
            ):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {target.value}")
    assert offenders == [], "read STRIPE_* through core.payments.stripe_env: " + ", ".join(offenders)


# ── the webhook fails closed on a placeholder secret ──────────────────────


def test_the_webhook_refuses_a_placeholder_secret(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.stripe_webhook import router

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _template_stripe_entries()["STRIPE_WEBHOOK_SECRET"])
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: None
    r = TestClient(app).post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 503
    assert r.json() == {"detail": "webhook not configured"}


# ── the sweep refuses up front instead of calling Stripe ──────────────────


@pytest.fixture
def db():
    from gdx_dispatch.core.audit import TenantBase, ensure_audit_table

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    yield session
    session.close()
    engine.dispose()


def test_the_sweep_with_the_template_key_never_calls_stripe(db, monkeypatch, caplog):
    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    inv = Invoice(
        id=uuid.uuid4(), customer_id=uuid.uuid4(), job_id=uuid.uuid4(),
        invoice_number="INV-920", billing_type="standard",
        subtotal=Decimal("100.00"), tax_amount=Decimal("0"), total=Decimal("100.00"),
        balance_due=Decimal("100.00"), status="sent",
        invoice_date=datetime.now(UTC).date(), public_token=uuid.uuid4().hex,
        company_id="tenant-920",
    )
    db.add(inv)
    db.commit()

    monkeypatch.setenv("STRIPE_SECRET_KEY", _template_stripe_entries()["STRIPE_SECRET_KEY"])
    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL, \
            patch("stripe.PaymentIntent.list") as stripe_list, \
            patch("gdx_dispatch.core.payments.cancel_open_intents_for_invoice") as sweep:
        SL.return_value.__enter__.return_value = db
        out = sweep_stale_intents(str(inv.id), why="payment_recorded")

    assert out["error"] == "stripe_unconfigured"
    sweep.assert_not_called()
    stripe_list.assert_not_called()
    assert "stale_intent_sweep_failed" not in caplog.text

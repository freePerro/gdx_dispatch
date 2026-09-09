"""#672 — the templates Price column was blank and a typed price was stored as 0.

Two defects behind one rename:

* **Read.** ``_serialize_template`` emits ``default_price`` (the column name);
  only the *agreements* serializer emits ``price``. The Vue read ``price``, so
  the Price column rendered the ``formatCurrency`` placeholder forever and the
  edit dialog opened blank.
* **Write, and worse.** ``TemplateIn`` never declared ``price`` at all. Pydantic
  drops undeclared keys, so ``default_price`` fell back to its ``0`` default and
  a template created through the UI with a typed price of 249 was stored as
  ``0.00`` — measured on the demo database 2026-09-09 during the #455 walk.
  A silent wrong-value write.

The issue filed only the read half and stated "no data loss", reasoning from the
PATCH path (which does accept both keys). The create path had no such compat.

These tests assert the CONTRACT in both directions rather than the source text:
what the serializer emits, and what each request model accepts. A test that only
checked the Vue string would prove somebody typed a name.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from gdx_dispatch.routers.service_agreements import (
    TemplateIn,
    TemplatePatch,
    _serialize_template,
)
from gdx_dispatch.tests.test_service_agreements import client  # noqa: F401  (fixture)

_VUE = (
    Path(__file__).resolve().parents[1]
    / "frontend/src/views/ServiceAgreementsView.vue"
)


class _Template:
    """Stand-in with the ORM's attribute names, not a mock of the serializer."""

    id = "11111111-1111-4111-8111-111111111111"
    company_id = "tenant-a"
    name = "Annual tune-up"
    description = None
    default_duration_months = 12
    default_price = Decimal("249.00")
    services_included = "[]"
    created_at = None
    updated_at = None


def test_the_serializer_emits_the_key_the_column_binds():
    """`default_price` is the contract, and it carries the real number."""
    out = _serialize_template(_Template())
    assert "default_price" in out
    assert out["default_price"] == pytest.approx(249.00)


def test_create_keeps_a_price_sent_under_either_key():
    """The silent-zero regression.

    Pre-fix, `TemplateIn(**{"name": ..., "price": 249})` produced
    `default_price == 0` because `price` was undeclared and dropped. Both keys
    must now survive to the value the row is built from.
    """
    canonical = TemplateIn(name="T", default_price=249)
    assert canonical.resolved_price == pytest.approx(249.0)

    legacy = TemplateIn(name="T", price=249)
    assert legacy.resolved_price == pytest.approx(249.0), (
        "a stale SPA bundle sends `price`; dropping it stores 0 and the user is "
        "never told (#672)"
    )


def test_explicit_default_price_wins_when_both_are_sent():
    """Create and patch must agree on precedence, or the two paths diverge."""
    both = TemplateIn(name="T", default_price=100, price=999)
    assert both.resolved_price == pytest.approx(100.0)
    assert TemplatePatch(default_price=100, price=999).default_price == pytest.approx(100.0)


def test_an_unpriced_create_is_still_zero_not_an_error():
    """`default_price` became Optional to tell 'absent' from 'zero'.

    That must not turn a template created without a price into a validation
    error or a None write — the column is NOT NULL.
    """
    assert TemplateIn(name="T").resolved_price == pytest.approx(0.0)
    assert TemplateIn(name="T", default_price=0).resolved_price == pytest.approx(0.0)


def test_the_vue_no_longer_reads_or_sends_the_phantom_key():
    """Absence-assertion on the template half of the view.

    Presence would only prove authorship; absence is the real property. Scoped
    to the template form and column so the AGREEMENTS side — whose serializer
    genuinely does emit `price` — stays free to use it.
    """
    src = _VUE.read_text()
    assert "templateForm.price" not in src, (
        "the template form must bind default_price; `price` is dropped by "
        "TemplateIn and the typed value is lost (#672)"
    )
    assert "formatCurrency(data.default_price)" in src
    assert 'field="default_price" header="Price"' in src
    # the agreements side is untouched and still correct
    assert "agreementForm.price" in src, (
        "the agreements serializer emits `price` — this fix must not have "
        "renamed that half"
    )


# ── The write path, over HTTP ────────────────────────────────────────────────
# The unit tests above assert `resolved_price` as a property. That is not the
# same as asserting the handler USES it: reverting create_template to
# `payload.default_price or 0` reinstates #672 exactly and leaves every test
# above green (verified by the adversarial review). These drive the real
# endpoint, so the stored value is the assertion.


def test_create_over_http_keeps_a_legacy_price(client: TestClient):  # noqa: F811
    """A stale SPA bundle sends `price`. The row must not be 0.00.

    This is #672's measured symptom: a template created through the UI with a
    typed price of 249 was stored as 0.00 on the demo database.
    """
    r = client.post(
        "/api/service-agreements/templates", json={"name": "Legacy", "price": 249}
    )
    assert r.status_code == 201, r.text
    assert r.json()["default_price"] == pytest.approx(249.0), (
        "create dropped the legacy `price` key and stored the 0 default (#672)"
    )


def test_create_over_http_keeps_the_canonical_default_price(client: TestClient):  # noqa: F811
    """The key the fixed SPA actually sends."""
    r = client.post(
        "/api/service-agreements/templates",
        json={"name": "Canonical", "default_price": 249},
    )
    assert r.status_code == 201, r.text
    assert r.json()["default_price"] == pytest.approx(249.0)


def test_create_over_http_prefers_default_price_when_both_are_sent(client: TestClient):  # noqa: F811
    """Create must not disagree with patch about precedence."""
    r = client.post(
        "/api/service-agreements/templates",
        json={"name": "Both", "default_price": 100, "price": 999},
    )
    assert r.status_code == 201, r.text
    assert r.json()["default_price"] == pytest.approx(100.0)


def test_create_over_http_without_a_price_stores_zero(client: TestClient):  # noqa: F811
    """`default_price` is Optional now; the column is NOT NULL. 0, not None."""
    r = client.post("/api/service-agreements/templates", json={"name": "Free"})
    assert r.status_code == 201, r.text
    assert r.json()["default_price"] == pytest.approx(0.0)

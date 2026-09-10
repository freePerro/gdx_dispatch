"""The invoice money surface demands a permission, and the RIGHT one.

Before this, all seventeen routes below took `Depends(get_current_user)` and
nothing else: any authenticated user — a technician on a phone — could record a
payment, void one, issue a credit memo or process a refund. The 61-key catalog
existed and these routes never consulted it.

Pinning the exact key per route matters as much as having one. Downgrading
`payments.process` to `invoices.write` would silently hand every technician the
payment surface back, because the live technician role carries `invoices.write`
and not `payments.process`. A test that only asserted "some gate exists" would
sleep through that.
"""
from __future__ import annotations

import pytest

from gdx_dispatch.tests.authz_sweep import unpermissioned_mutations

# route -> the key it must demand. Grouped by what the route does to money.
EXPECTED: dict[str, str] = {
    # Create and edit the document.
    "POST /api/invoices": "invoices.write",
    "PATCH /api/invoices/{invoice_id}": "invoices.write",
    "DELETE /api/invoices/{invoice_id}": "invoices.write",
    "POST /api/invoices/{invoice_id}/lines": "invoices.write",
    "PATCH /api/invoices/{invoice_id}/lines/{line_id}": "invoices.write",
    "DELETE /api/invoices/{invoice_id}/lines/{line_id}": "invoices.write",
    "POST /api/invoices/{invoice_id}/finalize": "invoices.write",
    # Put it in front of the customer.
    "POST /api/invoices/{invoice_id}/send": "invoices.send",
    "POST /api/invoices/{invoice_id}/mark-sent": "invoices.send",
    "POST /api/invoices/{invoice_id}/email-preview": "invoices.send",
    "POST /api/invoices/{invoice_id}/send-receipt": "invoices.send",
    "POST /api/invoices/{invoice_id}/pay-link": "invoices.send",
    # Give money back / cancel a receivable.
    "POST /api/invoices/{invoice_id}/refund": "invoices.refund",
    "POST /api/invoices/{invoice_id}/credit-memo": "invoices.refund",
    # Move money against the invoice.
    #
    # /payments is on invoices.write, NOT payments.process: it is the technician
    # field-capture endpoint (queued for offline replay, where any 4xx but 401 is
    # terminal), and payments.process also gates void/apply-credit, which are
    # office-only. The live technician role holds invoices.write.
    "POST /api/invoices/{invoice_id}/payments": "invoices.write",
    "POST /api/invoices/{invoice_id}/payments/{payment_id}/void": "payments.process",
    "POST /api/invoices/{invoice_id}/apply-credit": "payments.process",
}

def _required_keys_by_route() -> dict[str, set[str]]:
    from gdx_dispatch.app import create_app
    from gdx_dispatch.tests.conftest import iter_app_routes

    def keys_of(dependant, depth: int = 0) -> set[str]:
        found: set[str] = set()
        if dependant is None or depth > 8:
            return found
        for sub in getattr(dependant, "dependencies", []) or []:
            call = getattr(sub, "call", None)
            if getattr(call, "__qualname__", "") == "require_permission.<locals>._dependency":
                for cell in call.__closure__ or ():
                    value = cell.cell_contents
                    if isinstance(value, set) and all(isinstance(v, str) for v in value):
                        found |= value
            found |= keys_of(sub, depth + 1)
        return found

    out: dict[str, set[str]] = {}
    app = create_app()
    for path, route in iter_app_routes(app):
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        found = keys_of(dependant)
        for method in getattr(route, "methods", None) or []:
            out.setdefault(f"{method} {path}", found)
    return out


@pytest.fixture(scope="module")
def required() -> dict[str, set[str]]:
    return _required_keys_by_route()


@pytest.mark.parametrize(("route", "key"), sorted(EXPECTED.items()))
def test_route_demands_the_expected_key(required: dict[str, set[str]], route: str, key: str) -> None:
    assert route in required, f"{route} is not registered — did the path change?"
    assert key in required[route], (
        f"{route} must demand {key!r}; it demands {sorted(required[route]) or 'nothing'}"
    )


def test_the_gate_is_not_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive a real request with a technician-shaped permission set.

    Every other test in this file reads the app's *declared* dependencies. That
    proves a gate is wired; it does not prove it refuses anyone — the whole file
    would pass if ``require_permission`` returned None. So make an actual
    request with the live technician key set and assert the refusal, and assert
    a key the technician DOES hold still gets through (otherwise "it 403s" would
    be indistinguishable from "everything 403s").
    """
    from fastapi.testclient import TestClient

    from gdx_dispatch.app import create_app
    from gdx_dispatch.routers.auth.core import get_current_user

    # The live prod technician snapshot, 2026-09-09.
    tech_keys = {
        "jobs.read_own", "jobs.write", "jobs.read_all", "scheduling.read_own",
        "customers.read_own", "customers.read_all", "customers.write",
        "customers.contact_write", "estimates.read_own", "inventory.read",
        "inventory.write", "pricing.labor_matrix.read", "mobile.use",
        "mobile.chat", "invoices.write", "invoices.send",
        "invoices.read_own", "invoices.read_all",
    }
    monkeypatch.setattr(
        "gdx_dispatch.core.modules._load_user_permissions",
        lambda db, request, user: set(tech_keys),
    )

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "tech-1", "id": "tech-1", "role": "technician",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
    }
    client = TestClient(app, raise_server_exceptions=False)
    rid = "11111111-1111-1111-1111-111111111111"

    refused = client.post(f"/api/invoices/{rid}/refund", json={"amount": 1})
    assert refused.status_code == 403, (
        f"a technician reached the refund route: {refused.status_code}"
    )
    assert "invoices.refund" in refused.text, refused.text[:200]

    # Control: a key the technician DOES hold must not be refused, or the
    # assertion above proves nothing about the gate specifically.
    allowed = client.patch(f"/api/invoices/{rid}", json={"notes": "x"})
    assert allowed.status_code != 403, (
        "invoices.write is in the technician set — a 403 here means the test is "
        f"measuring something other than the gate ({allowed.status_code})"
    )


def test_none_of_them_remain_in_the_debt_baseline() -> None:
    still_listed = sorted(set(EXPECTED) & set(unpermissioned_mutations()))
    assert not still_listed, (
        "these are gated now and must be removed from .authz_unpermissioned_baseline:\n  "
        + "\n  ".join(still_listed)
    )

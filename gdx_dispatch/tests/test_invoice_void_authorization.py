"""Who may void an invoice.

`POST /api/invoices/{id}/void` shipped complete and audited with GL S5 and had
**zero UI callers** until 2026-08-23 — no `.vue` file called it, and production
had never written a single `invoice_voided` audit row. "Any authenticated user
may void" was academic while nothing could reach it.

Putting a button on the invoice screen ends that. `/billing/:id` carries no
route permission of its own (unlike `/billing`, which needs `invoices.read_all`,
and `/billing/new`, which needs `invoices.write`), so the screen — and now the
button — is reachable by any role that can log in. A void is terminal: there is
no un-void endpoint anywhere in this repo.

So the route is gated on `invoices.write`, matching `verify_invoice` next door
and `/billing/new`. `accounting`, `admin` and `owner` hold it; `technician`,
`dispatcher` and `sales` do not.

These drive real HTTP with `require_permission` LEFT LIVE — only the tenant
module grant is stubbed, because that is about which modules the tenant bought,
not about who may act. Removing the gate fails them.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.permissions import BUILTIN_ROLES
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.routers.invoices import router as invoices_router

TENANT = "tenant-void-authz"
USER = {"sub": "user-1", "email": "office@example.com", "role": "admin"}

VOID_PATH = "/api/invoices/{invoice_id}/void"


def _module_gate_dependencies(router) -> set:
    """Only the require_module callables — the permission gates stay live."""
    found = set()
    candidates = list(getattr(router, "dependencies", []))
    for route in router.routes:
        candidates.extend(getattr(route, "dependencies", []))
    for dep in candidates:
        if getattr(dep.dependency, "__qualname__", "").startswith("require_module"):
            found.add(dep.dependency)
    return found


@pytest.fixture
def env():
    # A real (empty) schema, so the allowed role's request reaches the handler
    # and comes back 404 rather than 500 on a None session. The 404 is what
    # proves the gate let it through; "not 403" alone would still pass if the
    # route had stopped existing.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    app = FastAPI()
    app.include_router(invoices_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: USER
    for dep in _module_gate_dependencies(invoices_router):
        app.dependency_overrides[dep] = lambda: None

    grants: dict[str, set[str]] = {"keys": set()}

    @app.middleware("http")
    async def _tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        request.state.user = USER
        request.state.user_permissions = set(grants["keys"])
        return await call_next(request)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, grants
    db.close()
    engine.dispose()


def _void(client, invoice_id: str | None = None):
    return client.post(VOID_PATH.format(invoice_id=invoice_id or uuid.uuid4()))


def test_a_technician_cannot_void_an_invoice(env):
    """The field tier must not be able to end a bill.

    `technician` carries `jobs.write` and `inventory.write` but deliberately
    not `invoices.write` — see `core/permissions.py`. Before the gate, the only
    thing standing between a tech and a terminal money mutation was that no
    button existed.
    """
    client, grants = env
    grants["keys"] = set(BUILTIN_ROLES["technician"])
    assert "invoices.write" not in grants["keys"], "fixture assumption"

    assert _void(client).status_code == 403


@pytest.mark.parametrize("role", ["dispatcher", "sales"])
def test_office_roles_without_invoices_write_cannot_void(env, role):
    """Dispatch and sales read invoices; they do not write them."""
    client, grants = env
    grants["keys"] = set(BUILTIN_ROLES[role])
    assert "invoices.read_all" in grants["keys"], "fixture assumption"
    assert "invoices.write" not in grants["keys"], "fixture assumption"

    assert _void(client).status_code == 403


def test_accounting_passes_the_gate_and_reaches_the_handler(env):
    """The counterfactual: the gate must not lock out the role that bills.

    A 404 is the proof — the request got PAST authorization and into the
    handler, which could not find that invoice id. Asserting "not 403" alone
    would pass if the route stopped existing.
    """
    client, grants = env
    grants["keys"] = set(BUILTIN_ROLES["accounting"])
    assert "invoices.write" in grants["keys"], "fixture assumption"

    assert _void(client).status_code == 404


def test_the_void_route_declares_a_permission_gate(env):
    """Names the permission, so a later edit that swaps it for a weaker one
    (or drops the gate while leaving an auth dependency behind) is visible."""
    gates = []
    for route in invoices_router.routes:
        if getattr(route, "path", None) != VOID_PATH:
            continue
        for dep in getattr(route, "dependencies", []):
            qualname = getattr(dep.dependency, "__qualname__", "")
            if qualname.startswith("require_permission"):
                gates.append(dep)
    assert gates, "POST /{invoice_id}/void declares no require_permission gate"

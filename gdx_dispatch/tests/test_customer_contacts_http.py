"""The office contact endpoints, exercised through the HTTP stack.

`test_customer_contacts_office.py` calls the handler functions directly. That
proves the logic and sees NONE of this: route registration and ordering,
dependency injection, the permission gate, response status codes, or Pydantic's
`extra="forbid"`. A handler can be perfect and the endpoint still unreachable —
this repo has shipped a send endpoint no UI ever called and a router with no
authorization at all. So: a real ASGI client over the real router.

Pinned here:
  • the four contact routes are registered and reachable at their real paths
  • `/{customer_id}/contacts` is NOT shadowed by `GET /{customer_id}`
  • create returns 201, delete/patch return 200, unknown customer returns 404
  • unknown fields are rejected (422), not silently dropped
  • a malformed email is rejected before it can become a default recipient
  • the permission gate REJECTS a caller without customers.contact_write

What this file still cannot see (it builds its own app, like 133 others —
see the CI/test debt assessment, item 5): the GL flush guard, the webhook
dispatch hook, the rate limiter, and app.py's four global exception handlers
that decide how an IntegrityError becomes an HTTP status. A shared fixture
built from the real `create_app()` is the fix; `conftest.py` has zero
references to it today. Until that lands, this file keeps the REAL
`require_permission` gate wired (only the tenant module grant is stubbed) so
its authorization test is behavioral rather than a decorator headcount.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, audit_ready_db, ensure_audit_table
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.permissions import WILDCARD
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.routers.customers import router as customers_router

TENANT = "tenant-test"
USER = {"sub": "user-office-1", "email": "office@example.com", "role": "admin"}


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


def _permission_gated(router) -> set[tuple[str, str]]:
    """(path, method) pairs whose route declares a require_permission gate."""
    gated = set()
    for route in router.routes:
        if not hasattr(route, "methods"):
            continue
        for dep in getattr(route, "dependencies", []):
            qualname = getattr(dep.dependency, "__qualname__", "")
            if qualname.startswith("require_permission"):
                for method in route.methods:
                    gated.add((route.path, method))
    return gated


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    app = FastAPI()
    # The router already carries prefix="/api/customers" (customers.py:26) —
    # re-prefixing here silently produced /api/customers/api/customers/... and
    # a wall of 404s. Exactly the class of thing a direct-call test cannot see.
    app.include_router(customers_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: USER

    # Stub ONLY the tenant module grant — that is about which modules this
    # tenant bought, not about who may act. `require_permission` stays REAL, so
    # the authorization test below can actually be failed by removing the gate.
    for dep in _module_gate_dependencies(customers_router):
        app.dependency_overrides[dep] = lambda: None

    # The tenant middleware isn't mounted on this bare app; the handlers read
    # request.state.tenant, so supply it the way the middleware would. The
    # permission set is seeded the same way `require_permission` caches it
    # (`core/modules.py:536`), so tests can change roles without a user table.
    grants = {"keys": {WILDCARD}}

    @app.middleware("http")
    async def _tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        request.state.user = USER
        request.state.user_permissions = set(grants["keys"])
        return await call_next(request)

    uid = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO customers (id, name, email, company_id, created_at, deleted_at)
            VALUES (:id, :n, :e, :c, :ts, NULL)
            """
        ),
        {"id": uid.hex, "n": "Riverbend Lumber", "e": "account@example.com",
         "c": TENANT, "ts": datetime.now(timezone.utc).isoformat()},
    )
    db.commit()

    with TestClient(app) as client:
        yield client, str(uid), db, grants
    db.close()
    engine.dispose()


def _base(cid: str) -> str:
    return f"/api/customers/{cid}/contacts"


# ── reachability ────────────────────────────────────────────────────────────


def test_the_four_contact_routes_are_registered(env):
    _, _, _, _ = env
    paths = {
        (r.path, tuple(sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})))
        for r in customers_router.routes
        if hasattr(r, "methods")
    }
    assert ("/api/customers/{customer_id}/contacts", ("GET",)) in paths
    assert ("/api/customers/{customer_id}/contacts", ("POST",)) in paths
    assert ("/api/customers/{customer_id}/contacts/{contact_id}", ("PATCH",)) in paths
    assert ("/api/customers/{customer_id}/contacts/{contact_id}", ("DELETE",)) in paths


def test_contacts_path_is_not_shadowed_by_the_customer_detail_route(env):
    """`GET /{customer_id}` is registered first. A path param never spans a
    slash, so it must not swallow `/{customer_id}/contacts` — but the file
    carries a documented route-order trap, so this is pinned, not assumed."""
    client, cid, _, _ = env
    r = client.get(_base(cid))
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── status codes ────────────────────────────────────────────────────────────


def test_create_returns_201_and_the_row_is_listed(env):
    client, cid, _, _ = env
    r = client.post(_base(cid), json={"name": "Site A", "label": "job contact"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Site A"
    assert body["is_primary"] is False

    listed = client.get(_base(cid)).json()
    assert [c["name"] for c in listed] == ["Site A"]


def test_patch_and_delete_return_200(env):
    client, cid, _, _ = env
    made = client.post(_base(cid), json={"name": "Site A"}).json()
    assert client.patch(f"{_base(cid)}/{made['id']}", json={"label": "tenant"}).status_code == 200
    assert client.delete(f"{_base(cid)}/{made['id']}").status_code == 200
    assert client.get(_base(cid)).json() == []


def test_unknown_customer_returns_404_on_every_verb(env):
    client, _, _, _ = env
    ghost = str(uuid.uuid4())
    assert client.get(_base(ghost)).status_code == 404
    assert client.post(_base(ghost), json={"name": "Site A"}).status_code == 404
    assert client.patch(f"{_base(ghost)}/{uuid.uuid4()}", json={"label": "x"}).status_code == 404
    assert client.delete(f"{_base(ghost)}/{uuid.uuid4()}").status_code == 404


def test_malformed_customer_id_returns_404_not_500(env):
    client, _, _, _ = env
    assert client.get("/api/customers/not-a-uuid/contacts").status_code == 404
    assert client.post("/api/customers/not-a-uuid/contacts", json={"name": "X"}).status_code == 404


# ── input contract ──────────────────────────────────────────────────────────


def test_unknown_fields_are_rejected_not_silently_dropped(env):
    """extra='forbid'. A client sending `is_primary: true` at create must be
    told no, never have it quietly ignored — that is how a caller comes to
    believe it set the default recipient."""
    client, cid, _, _ = env
    r = client.post(_base(cid), json={"name": "Site A", "is_primary": True})
    assert r.status_code == 422


def test_a_nameless_contact_is_refused(env):
    client, cid, _, _ = env
    assert client.post(_base(cid), json={"name": ""}).status_code == 422
    assert client.post(_base(cid), json={}).status_code == 422


@pytest.mark.parametrize("bad", ["asdf", "no-at-sign.example.com", "two@@example.com", "a@b..c"])
def test_a_malformed_email_never_reaches_the_database(env, bad):
    """The primary contact's email is what automated sends resolve to. Accepting
    'asdf' and letting someone promote that contact routes every automated email
    for the account into nothing, with a green toast and no error anywhere."""
    client, cid, _, _ = env
    assert client.post(_base(cid), json={"name": "Site A", "email": bad}).status_code == 422

    made = client.post(_base(cid), json={"name": "Site B"}).json()
    assert client.patch(f"{_base(cid)}/{made['id']}", json={"email": bad}).status_code == 422


def test_a_good_email_is_accepted(env):
    client, cid, _, _ = env
    r = client.post(_base(cid), json={"name": "Site A", "email": "site.a@example.com"})
    assert r.status_code == 201
    assert r.json()["email"] == "site.a@example.com"


# ── authorization ───────────────────────────────────────────────────────────


def test_each_write_route_declares_the_permission_gate(env):
    """Structural: the decorator is present. Cheap, and catches a delete."""
    _, _, _, _ = env
    gated = _permission_gated(customers_router)
    assert ("/api/customers/{customer_id}/contacts", "POST") in gated
    assert ("/api/customers/{customer_id}/contacts/{contact_id}", "PATCH") in gated
    assert ("/api/customers/{customer_id}/contacts/{contact_id}", "DELETE") in gated


def test_a_caller_without_contact_write_is_refused(env):
    """Behavioral, and the one that matters. The mobile writers gate on
    customers.contact_write (`mobile.py:4098`); an office door to the same
    table without it makes that permission advisory rather than enforced.

    This runs the REAL gate against a permission set that lacks the key —
    the check a suite with a hardcoded admin override cannot make, and the
    reason this repo could ship an unauthenticated payments router and a
    plugin surface with no role authorization without a single test going red."""
    client, cid, _, grants = env
    grants["keys"] = {"customers.read"}          # a technician-shaped set

    assert client.post(_base(cid), json={"name": "Site A"}).status_code == 403
    assert client.patch(f"{_base(cid)}/{uuid.uuid4()}", json={"label": "x"}).status_code == 403
    assert client.delete(f"{_base(cid)}/{uuid.uuid4()}").status_code == 403

    # and the same caller WITH the key gets through — proving the 403s above
    # come from the gate, not from some unrelated failure
    grants["keys"] = {"customers.contact_write"}
    assert client.post(_base(cid), json={"name": "Site A"}).status_code == 201


# ── the audit trail is part of the transaction ──────────────────────────────


def test_a_created_contact_and_its_audit_row_commit_together(env):
    client, cid, db, _ = env
    r = client.post(_base(cid), json={"name": "Site A"})
    assert r.status_code == 201
    actions = [
        row[0] for row in db.execute(text("SELECT action FROM audit_logs")).all()
    ]
    assert "customer_contact_added" in actions


def test_a_failed_audit_leaves_no_contact_behind(env, monkeypatch):
    """The one that can actually fail on the bug it describes.

    The first version of this file asserted only "a 201 produced an audit row",
    which passes just as happily on the broken code. It has to be driven from
    the failure side: make the audit write raise, then read the table from a
    SECOND session on the same engine — a same-session read sees uncommitted
    work and reports success that no other connection can see.

    The subtle part, and the reason this was broken after the first fix:
    `ensure_audit_table` COMMITS on first use per engine (`audit.py:320`). Called
    from inside the handler it hardens the handler's staged INSERT before the
    audit row is written, so `audit_or_rollback` rolls back nothing. The
    endpoints take `Depends(audit_ready_db)` so that initialization happens
    before the handler stages anything.
    """
    import gdx_dispatch.core.audit as audit_mod

    client, cid, db, _ = env

    # Fail the audit the way it actually fails: AFTER ensure_audit_table has
    # run. Replacing _log_audit_event_impl wholesale also removes the
    # ensure_audit_table call inside it — which is the thing that commits — so
    # a naive patch makes the broken code look correct. Ask me how I know.
    def _boom(session, *a, **k):
        audit_mod.ensure_audit_table(session)
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit_mod, "_log_audit_event_impl", _boom)
    # force the guard to re-run (and re-commit) inside the handler, the state a
    # fresh process hits on its first audited write
    import weakref
    monkeypatch.setattr(audit_mod, "_AUDIT_GUARD_INITIALIZED", weakref.WeakSet())

    r = client.post(_base(cid), json={"name": "Ghost"})
    assert r.status_code == 500

    fresh = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)()
    try:
        rows = fresh.execute(
            text("SELECT name FROM customer_contacts WHERE name = 'Ghost'")
        ).all()
    finally:
        fresh.close()
    assert rows == [], (
        f"500 returned but {len(rows)} contact row(s) COMMITTED — "
        "the operator retries and gets duplicates"
    )

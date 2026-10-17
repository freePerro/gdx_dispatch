"""Every customer-authenticated /portal/* route answers 401 over HTTP (GDXA-1079).

The portal tests elsewhere either call handlers directly, which skips the
router's auth dependency, or override ``get_current_portal_customer``, which
replaces it. This file keeps the real dependency and drives each route through
a TestClient with no token, a garbage bearer, an expired customer JWT, customer
claims under a non-customer role, a token signed with another key, a token for
a deactivated customer user and one for a user id that does not exist. Each
must be 401. With a valid token, another customer's invoice or estimate must
answer exactly as an id that does not exist, so a signed-in customer cannot
learn which of someone else's ids are real.

The route list is read from the routers, not written out by hand, and
``test_every_portal_route_is_either_guarded_or_public`` checks it against the
pinned route table (``openapi_routes.txt``). A new /portal route therefore
either carries the guard and is tested here, or is named in ``PUBLIC`` on
purpose.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models  # noqa: F401  (registers every model on TenantBase)
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Invoice
from gdx_dispatch.modules.customer_portal.models import CustomerUser
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers import portal, portal_resale, quote_requests

TENANT = "tenant-test"

# The /portal routes a caller reaches without a customer token, by design:
# they are how a customer gets one.
PUBLIC = {
    ("POST", "/portal/login"),
    ("POST", "/portal/login/password"),
    ("GET", "/portal/verify"),
}

# The customer-facing routers, as app.py includes them.
PORTAL_ROUTERS = (portal.router, quote_requests.portal_router, portal_resale.portal_router)

_ROUTE_TABLE = Path(portal.__file__).resolve().parents[1] / "openapi_routes.txt"


def _depends_on_guard(dependant) -> bool:
    return any(
        d.call is portal.get_current_portal_customer or _depends_on_guard(d)
        for d in dependant.dependencies
    )


def _portal_routes() -> list[tuple[str, str, bool]]:
    """(method, full path, guarded) for every route on the portal routers."""
    out = []
    for router in PORTAL_ROUTERS:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            guarded = _depends_on_guard(route.dependant)
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append((method, route.path, guarded))
    return out


GUARDED = sorted((m, p) for m, p, g in _portal_routes() if g)


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
        customer = Customer(name="Pat Example", email="pat@example.com", company_id=TENANT)
        s.add(customer)
        s.flush()
        user = CustomerUser(customer_id=customer.id, email="pat@example.com", is_active=True)
        invoice = Invoice(
            customer_id=customer.id, invoice_number="INV-1", subtotal=100, tax_amount=0,
            total=100, balance_due=100, status="sent", public_token=uuid4().hex, company_id=TENANT,
        )
        estimate = Estimate(
            customer_id=customer.id, estimate_number="EST-1", label="New door", total=1500,
            status="sent", public_token=uuid4().hex, company_id=TENANT,
        )
        # A portal login that was revoked: the row exists, the account is off.
        revoked = CustomerUser(customer_id=customer.id, email="old@example.com", is_active=False)
        other = Customer(name="Other Person", company_id=TENANT)
        s.add(other)
        s.flush()
        other_invoice = Invoice(
            customer_id=other.id, invoice_number="INV-2", subtotal=200, tax_amount=0,
            total=200, balance_due=200, status="sent", public_token=uuid4().hex, company_id=TENANT,
        )
        other_estimate = Estimate(
            customer_id=other.id, estimate_number="EST-2", label="Other door", total=900,
            status="sent", public_token=uuid4().hex, company_id=TENANT,
        )
        s.add_all([user, revoked, invoice, estimate, other_invoice, other_estimate])
        s.commit()
        self.user_id, self.customer_id = str(user.id), str(user.customer_id)
        self.revoked_user_id = str(revoked.id)
        # Ids of rows that exist and belong to the signed-in customer.
        self.real_ids = {"invoice_id": str(invoice.id), "estimate_id": str(estimate.id)}
        # Ids of rows that exist and belong to someone else.
        self.other_ids = {"invoice_id": str(other_invoice.id), "estimate_id": str(other_estimate.id)}
        s.close()

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

        for router in PORTAL_ROUTERS:
            app.include_router(router)
        # Only the DB is swapped. get_current_portal_customer stays the real one.
        app.dependency_overrides[get_db] = _db
        self.client = TestClient(app, raise_server_exceptions=False)

    def token(self, *, key: str = portal.SIGN_KEY, **over) -> str:
        claims = {
            "sub": self.user_id,
            "role": "customer",
            "customer_id": self.customer_id,
            "typ": "access",
            "aud": portal.PORTAL_AUDIENCE,
            "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
        }
        claims.update(over)
        claims = {k: v for k, v in claims.items() if v is not None}
        return portal.jwt.encode(claims, key, algorithm=portal.ALG)

    def call(self, method: str, path: str, auth: str | None):
        headers = {"Authorization": auth} if auth is not None else {}
        return self.client.request(method, path, headers=headers)


@pytest.fixture(scope="module")
def ctx():
    c = _Ctx()
    yield c
    c.engine.dispose()


def _fill(path: str, ids: dict[str, str] | None = None) -> str:
    ids = ids or {}
    return re.sub(r"\{(\w+)\}", lambda m: ids.get(m.group(1), str(uuid4())), path)


def _bad_auth(ctx: _Ctx) -> dict[str, str | None]:
    past = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())
    return {
        "no token": None,
        "garbage bearer": "Bearer not-a-jwt",
        "expired customer jwt": f"Bearer {ctx.token(exp=past)}",
        "non-customer role": f"Bearer {ctx.token(role='admin')}",
        "non-access typ": f"Bearer {ctx.token(typ='refresh')}",
        "no portal audience": f"Bearer {ctx.token(aud=None)}",
        "wrong signing key": f"Bearer {ctx.token(key='another-secret-key-at-least-32-bytes-long')}",
        "deactivated customer user": f"Bearer {ctx.token(sub=ctx.revoked_user_id)}",
        "unknown customer user": f"Bearer {ctx.token(sub=str(uuid4()))}",
    }


BAD_CASES = (
    "no token",
    "garbage bearer",
    "expired customer jwt",
    "non-customer role",
    "non-access typ",
    "no portal audience",
    "wrong signing key",
    "deactivated customer user",
    "unknown customer user",
)


def test_every_portal_route_is_either_guarded_or_public():
    """The routers' /portal routes are the pinned table's, and only PUBLIC is unguarded."""
    pinned = set()
    for line in _ROUTE_TABLE.read_text().splitlines():
        method, _, path = line.partition(" ")
        if path.startswith("/portal/") or path == "/portal":
            pinned.add((method, path))
    routed = {(m, p) for m, p, _ in _portal_routes()}
    assert routed == pinned, (
        f"portal routes not on PORTAL_ROUTERS: {sorted(pinned - routed)}; "
        f"on the routers but not pinned: {sorted(routed - pinned)}"
    )
    unguarded = {(m, p) for m, p, g in _portal_routes() if not g}
    assert unguarded == PUBLIC, f"unguarded /portal routes not declared public: {sorted(unguarded - PUBLIC)}"
    assert len(GUARDED) == len(pinned) - len(PUBLIC)


@pytest.mark.parametrize("case", BAD_CASES)
@pytest.mark.parametrize(("method", "path"), GUARDED, ids=[f"{m} {p}" for m, p in GUARDED])
def test_guarded_route_answers_401_without_a_valid_customer_token(ctx, method, path, case):
    r = ctx.call(method, _fill(path), _bad_auth(ctx)[case])
    assert r.status_code == 401, f"{case}: {method} {path} -> {r.status_code} {r.text}"


@pytest.mark.parametrize(("method", "path"), GUARDED, ids=[f"{m} {p}" for m, p in GUARDED])
def test_guarded_route_admits_a_valid_customer_token(ctx, method, path):
    """Control: the 401s above come from the token, not from the harness."""
    r = ctx.call(method, _fill(path, ctx.real_ids), f"Bearer {ctx.token()}")
    assert r.status_code != 401, f"{method} {path} -> 401 with a valid token: {r.text}"


_ID_ROUTES = [
    (m, p) for m, p in GUARDED if "{invoice_id}" in p or "{estimate_id}" in p
]


@pytest.mark.parametrize("case", BAD_CASES)
@pytest.mark.parametrize(("method", "path"), _ID_ROUTES, ids=[f"{m} {p}" for m, p in _ID_ROUTES])
def test_unknown_id_is_indistinguishable_from_a_real_one_without_a_valid_token(ctx, method, path, case):
    """Structural pin: the guard answers before the handler reads the id.

    It goes red only if an id-reading dependency is put ahead of the guard.
    """
    auth = _bad_auth(ctx)[case]
    real = ctx.call(method, _fill(path, ctx.real_ids), auth)
    unknown = ctx.call(method, _fill(path), auth)
    assert real.status_code == unknown.status_code == 401
    assert real.content == unknown.content
    assert real.headers.get("www-authenticate") == unknown.headers.get("www-authenticate")


_GET_ID_ROUTES = [(m, p) for m, p in _ID_ROUTES if m == "GET"]


@pytest.mark.parametrize(("method", "path"), _GET_ID_ROUTES, ids=[f"{m} {p}" for m, p in _GET_ID_ROUTES])
def test_another_customers_id_answers_as_an_unknown_id(ctx, method, path):
    auth = f"Bearer {ctx.token()}"
    # The same document id in both calls, so only the invoice/estimate id varies.
    doc = str(uuid4())
    theirs = ctx.call(method, _fill(path, {**ctx.other_ids, "document_id": doc}), auth)
    unknown = ctx.call(method, _fill(path, {"document_id": doc}), auth)
    assert theirs.status_code == unknown.status_code == 404, (theirs.status_code, theirs.text)
    assert theirs.content == unknown.content

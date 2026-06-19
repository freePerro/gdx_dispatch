"""Cross-tenant isolation golden test — SS-5 Slice A scaffold.

Proves that creating a Membership in tenant A does not surface when a query
is scoped to tenant B. Minimal Slice-A proof; Slice B+ deepens with real
service-layer queries, share-path leaks, and FK cascade edge cases.
"""
from __future__ import annotations

from sqlalchemy import select

from gdx_dispatch.models.platform import Membership
from gdx_dispatch.tests.factories import (
    make_capability_set,
    make_identity,
    make_membership,
    make_tenant,
)


def test_membership_is_scoped_to_its_tenant(control_db) -> None:
    """A membership granted in tenant A must not appear in tenant B's query."""
    tenant_a = make_tenant(control_db, slug="ss5-iso-a")
    tenant_b = make_tenant(control_db, slug="ss5-iso-b")
    identity = make_identity(control_db, email="iso-user@example.com")
    capset = make_capability_set(control_db, name="ss5-iso-capset")

    membership = make_membership(
        control_db,
        identity=identity,
        tenant=tenant_a,
        capability_set=capset,
        role="admin",
    )
    # Membership.tenant_id stores the Tenant UUID, not the slug.
    assert membership.tenant_id == tenant_a.id

    # Query scoped to tenant B — must be empty.
    leaked = control_db.execute(
        select(Membership).where(Membership.tenant_id == tenant_b.id)
    ).scalars().all()
    assert leaked == [], "cross-tenant membership leak: tenant B sees tenant A's membership"

    # Query scoped to tenant A — sees exactly the one membership.
    present = control_db.execute(
        select(Membership).where(Membership.tenant_id == tenant_a.id)
    ).scalars().all()
    assert len(present) == 1
    assert present[0].id == membership.id


def test_two_identities_two_tenants_no_cross_membership(control_db) -> None:
    """Two identities in two tenants yield 2 memberships, each scoped correctly."""
    tenant_a = make_tenant(control_db, slug="ss5-iso2-a")
    tenant_b = make_tenant(control_db, slug="ss5-iso2-b")
    id_a = make_identity(control_db, email="user-a@example.com")
    id_b = make_identity(control_db, email="user-b@example.com")
    capset = make_capability_set(control_db, name="ss5-iso2-capset")

    m_a = make_membership(control_db, identity=id_a, tenant=tenant_a, capability_set=capset)
    m_b = make_membership(control_db, identity=id_b, tenant=tenant_b, capability_set=capset)

    a_rows = control_db.execute(
        select(Membership).where(Membership.tenant_id == tenant_a.id)
    ).scalars().all()
    b_rows = control_db.execute(
        select(Membership).where(Membership.tenant_id == tenant_b.id)
    ).scalars().all()

    assert [m.id for m in a_rows] == [m_a.id]
    assert [m.id for m in b_rows] == [m_b.id]
    # Identity-scoped queries never return another identity's memberships.
    assert {m.identity_id for m in a_rows} == {id_a.id}
    assert {m.identity_id for m in b_rows} == {id_b.id}

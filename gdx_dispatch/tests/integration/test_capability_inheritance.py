"""Capability-inheritance scaffolds — SS-5 Slice B.

Exercises two platform-schema relationships that downstream SSes rely on:

  1. ``Capability.parent_capability_id`` self-referential FK — a child
     capability links to its parent. The platform doesn't auto-cascade
     revocation; that's service-layer logic. These tests prove the chain
     shape is correct and queries can walk it.

  2. ``Capability.granted_via_installation_id`` FK — a capability granted
     during an installation points at the installation that granted it, so
     installation uninstall can revoke all its granted capabilities.

Both are model+query-level tests. Revocation cascade behavior lives in
SS-7+ service code.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from gdx_dispatch.models.platform import Capability
from gdx_dispatch.tests.factories import (
    make_capability,
    make_capability_set,
    make_installation,
)


def test_child_capability_references_parent(control_db):
    """A child capability with parent_capability_id forms a walkable chain."""
    capset = make_capability_set(control_db, name="ss5-b-parent-capset")
    parent = make_capability(
        control_db,
        capability_set=capset,
        action="read",
        resource_type="job",
    )
    child = make_capability(
        control_db,
        capability_set=capset,
        action="read",
        resource_type="job.note",
        parent_capability_id=parent.id,
    )

    # Walk parent → child.
    fetched_child = control_db.execute(
        select(Capability).where(Capability.parent_capability_id == parent.id)
    ).scalar_one()
    assert fetched_child.id == child.id

    # Walk child → parent via relationship.
    control_db.refresh(child)
    assert child.parent_capability_id == parent.id

    # Revoking the parent does NOT auto-revoke the child (no DB cascade).
    # Service-layer is responsible for the cascade — this test documents the
    # current schema behavior.
    parent.revoked_at = datetime.now(timezone.utc)
    control_db.flush()
    control_db.refresh(child)
    assert child.revoked_at is None, (
        "schema should not auto-cascade revocation — cascade is service-layer responsibility"
    )


def test_installation_scoped_capability_links_back_to_installation(control_db):
    """A capability granted_via_installation_id must be queryable by install."""
    installation = make_installation(control_db)
    capset = make_capability_set(control_db, name="ss5-b-install-capset")

    granted_cap = make_capability(
        control_db,
        capability_set=capset,
        action="write",
        resource_type="customer",
        granted_via_installation_id=installation.id,
    )
    # Another capability in the same capset but NOT installation-scoped.
    direct_cap = make_capability(
        control_db,
        capability_set=capset,
        action="read",
        resource_type="customer",
    )

    # Query capabilities granted by this specific installation.
    install_caps = control_db.execute(
        select(Capability).where(Capability.granted_via_installation_id == installation.id)
    ).scalars().all()

    assert [c.id for c in install_caps] == [granted_cap.id]
    assert direct_cap.id not in [c.id for c in install_caps]
    assert direct_cap.granted_via_installation_id is None

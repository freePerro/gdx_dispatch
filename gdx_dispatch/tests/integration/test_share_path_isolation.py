"""Share-path isolation integration tests — SS-5 Slice B.

Validates the platform's share model at the query/model layer:
  - A Share is visible only to its declared target tenant (not a third tenant)
  - Revoking the parent SharedResource hides all child shares
  - Revoking a Share hides it while leaving the SharedResource active
  - An expired share is filtered out by standard "active share" queries
  - An installation-scoped share (target_installation_id) resolves by install,
    not tenant

These are model+query tests, not service-layer tests. The service layer
(SS-7+) will own the real authorization dispatch. Slice B's job is to prove
the model shapes right.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from gdx_dispatch.models.platform_extensions import Share, SharedResource
from gdx_dispatch.tests.factories import (
    make_installation,
    make_share,
    make_tenant,
)


def _active_shares(db, target_tenant) -> list[Share]:
    """Mirror of the canonical 'active shares for target tenant' query.
    Accepts a tenant ORM object; uses tenant.id (UUID) for the filter.
    """
    now = datetime.now(timezone.utc)
    return db.execute(
        select(Share)
        .join(SharedResource, Share.shared_resource_id == SharedResource.id)
        .where(
            Share.target_tenant_id == target_tenant.id,
            Share.revoked_at.is_(None),
            SharedResource.revoked_at.is_(None),
            (Share.expires_at.is_(None)) | (Share.expires_at > now),
        )
    ).scalars().all()


def test_share_visible_only_to_target_tenant(control_db, make_dual_tenant_share_setup):
    """A share targeting tenant B must not surface when querying tenant C."""
    setup = make_dual_tenant_share_setup()
    tenant_c = make_tenant(control_db, slug="share-unrelated-c")

    visible_to_b = _active_shares(control_db, setup["target_tenant"])
    visible_to_c = _active_shares(control_db, tenant_c)

    assert [s.id for s in visible_to_b] == [setup["share"].id]
    assert visible_to_c == []


def test_revoking_shared_resource_hides_all_child_shares(control_db, make_dual_tenant_share_setup):
    """Setting revoked_at on SharedResource must remove all its Shares from active queries."""
    setup = make_dual_tenant_share_setup()
    target_tenant = setup["target_tenant"]

    # Before revocation — share is active.
    assert len(_active_shares(control_db, target_tenant)) == 1

    # Owner revokes the underlying shared_resource.
    setup["shared_resource"].revoked_at = datetime.now(timezone.utc)
    control_db.flush()

    # Share row still exists...
    raw_count = control_db.execute(
        select(Share).where(Share.id == setup["share"].id)
    ).scalars().all()
    assert len(raw_count) == 1
    # ...but the active-share query now returns empty.
    assert _active_shares(control_db, target_tenant) == []


def test_revoking_share_hides_it_but_shared_resource_survives(
    control_db, make_dual_tenant_share_setup
):
    """Share-level revocation removes the one share; SharedResource stays for other targets."""
    setup = make_dual_tenant_share_setup()
    target_tenant = setup["target_tenant"]

    # Add a second target tenant with its own share on the same resource.
    second_target = make_tenant(control_db, slug="share-second-target")
    make_share(
        control_db,
        shared_resource=setup["shared_resource"],
        target_tenant=second_target,
        capabilities=["read"],
    )

    # Both targets see one share each.
    assert len(_active_shares(control_db, target_tenant)) == 1
    assert len(_active_shares(control_db, second_target)) == 1

    # Revoke ONLY the first share.
    setup["share"].revoked_at = datetime.now(timezone.utc)
    control_db.flush()

    assert _active_shares(control_db, target_tenant) == []
    # Second target still sees its share.
    assert len(_active_shares(control_db, second_target)) == 1
    # SharedResource is still active.
    assert setup["shared_resource"].revoked_at is None


def test_expired_share_is_filtered_out(control_db, make_dual_tenant_share_setup):
    """A share with expires_at in the past must not appear in active queries."""
    setup = make_dual_tenant_share_setup()
    setup["share"].expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    control_db.flush()

    assert _active_shares(control_db, setup["target_tenant"]) == []

    # Unexpired share (expires_at far future) still shows.
    setup["share"].expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    control_db.flush()
    assert len(_active_shares(control_db, setup["target_tenant"])) == 1


def test_installation_scoped_share_resolves_by_install_not_tenant(
    control_db, make_dual_tenant_share_setup
):
    """A share with target_installation_id only surfaces when queried by install.

    This documents the intended discriminator — target_tenant_id matching does
    NOT find installation-scoped shares; future service code should dispatch
    on both keys.
    """
    setup = make_dual_tenant_share_setup()
    target_tenant = setup["target_tenant"]
    install = make_installation(control_db, tenant=target_tenant)

    install_share = make_share(
        control_db,
        shared_resource=setup["shared_resource"],
        target_tenant=None,
        target_installation=install,
        capabilities=["read"],
    )

    # Querying by tenant only — shouldn't return the install-scoped row.
    by_tenant = _active_shares(control_db, target_tenant)
    # (Tenant-scoped query returns the original from setup, not the install one.)
    assert install_share.id not in [s.id for s in by_tenant]

    # Install-scoped query — returns the install-scoped row.
    by_install = control_db.execute(
        select(Share).where(
            Share.target_installation_id == install.id,
            Share.revoked_at.is_(None),
        )
    ).scalars().all()
    assert [s.id for s in by_install] == [install_share.id]

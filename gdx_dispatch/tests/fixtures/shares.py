"""Dual-tenant share-setup fixture — SS-5 Slice B.

Provides a realistic two-tenant setup with a SharedResource on tenant A and
a Share targeting tenant B, plus the option to extend with additional target
tenants or installation-scoped shares.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from gdx_dispatch.tests.factories import (
    make_share,
    make_shared_resource,
    make_tenant,
)


@pytest.fixture
def make_dual_tenant_share_setup(
    control_db: Session,
) -> Callable[..., dict]:
    """Factory: two tenants + a shared resource owned by A + a share to B.

    Returns a dict with keys:
      - ``owner_tenant`` (Tenant)
      - ``target_tenant`` (Tenant)
      - ``shared_resource`` (SharedResource)
      - ``share`` (Share) — target_tenant_id = target_tenant.slug

    Additional kwargs:
      - ``resource_type`` (default 'job')
      - ``capabilities`` (default ['read'])
      - ``owner_slug_prefix`` (default 'share-owner')
      - ``target_slug_prefix`` (default 'share-target')
    """
    def _make(
        *,
        resource_type: str = "job",
        capabilities: list[str] | None = None,
        owner_slug_prefix: str = "share-owner",
        target_slug_prefix: str = "share-target",
    ) -> dict:
        seq = next(_SEQ)
        owner = make_tenant(control_db, slug=f"{owner_slug_prefix}-{seq}")
        target = make_tenant(control_db, slug=f"{target_slug_prefix}-{seq}")
        sr = make_shared_resource(
            control_db,
            owner_tenant=owner,
            resource_type=resource_type,
        )
        share = make_share(
            control_db,
            shared_resource=sr,
            target_tenant=target,
            capabilities=capabilities or ["read"],
        )
        return {
            "owner_tenant": owner,
            "target_tenant": target,
            "shared_resource": sr,
            "share": share,
        }

    return _make


import itertools as _itertools

_SEQ = _itertools.count(1)

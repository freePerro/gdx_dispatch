"""Ratchet: no NEW mutation route may ship without an authorization check.

The sibling ratchet (``test_authz_route_sweep.py``) asks "is anyone there?".
This one asks "are they allowed?" — the question the sweep structurally could
not answer while authentication and authorization lived in one set, which is
how mutation routes carrying only ``get_current_user`` scored as gated.

``test_the_sweep_can_fail_for_its_own_defect`` is the control. A ratchet that
cannot go red for the thing it claims to catch is decoration, so this builds a
synthetic app with one authenticated-only mutation and one permission-gated
mutation and asserts the sweep separates them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gdx_dispatch.tests.authz_sweep import (
    AUTH_DEPENDENCIES,
    AUTHN_DEPENDENCIES,
    AUTHZ_DEPENDENCIES,
    unpermissioned_mutations,
)

BASELINE_PATH = Path(__file__).resolve().parents[2] / ".authz_unpermissioned_baseline"


def _baseline() -> set[str]:
    lines = BASELINE_PATH.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}


@pytest.fixture(scope="module")
def current() -> set[str]:
    return set(unpermissioned_mutations())


# ── the invariant that protects the EXISTING authentication ratchet ──────


def test_the_split_is_a_partition_not_a_rename() -> None:
    """Pin membership — asserting the union would be a tautology.

    ``AUTH_DEPENDENCIES`` is *defined* as ``AUTHN | AUTHZ``, so asserting they
    union to it can never fail; dropping ``get_current_user`` from AUTHN would
    sail straight past such a check while silently widening what
    ``.authz_ungated_baseline`` means. Pin the members that carry weight
    instead, and pin the size so adding a dependency is a deliberate act of
    choosing which side of the seam it belongs on.
    """
    assert not (AUTHN_DEPENDENCIES & AUTHZ_DEPENDENCIES), (
        "a dependency cannot be both authentication and authorization: "
        f"{sorted(AUTHN_DEPENDENCIES & AUTHZ_DEPENDENCIES)}"
    )
    for name in ("get_current_user", "get_current_portal_customer", "_require_api_key"):
        assert name in AUTHN_DEPENDENCIES, f"{name} must stay authentication"
    for name in ("require_permission.<locals>._dependency", "require_role.<locals>._dependency"):
        assert name in AUTHZ_DEPENDENCIES, f"{name} must stay authorization"
    assert len(AUTH_DEPENDENCIES) == 23, (
        "the auth dependency set changed size. That is fine — but decide which "
        "side of the authn/authz seam the new dependency sits on, then update "
        f"this pin. Currently {len(AUTH_DEPENDENCIES)}: "
        f"{sorted(AUTH_DEPENDENCIES)}"
    )


# ── the ratchet ──────────────────────────────────────────────────────────


def test_no_new_unpermissioned_mutations(current: set[str]) -> None:
    new = sorted(current - _baseline())
    assert not new, (
        "These MUTATION routes authenticate a caller but never check whether "
        f"they are allowed, and are not in {BASELINE_PATH.name}:\n  "
        + "\n  ".join(new)
        + "\n\nFIRST read the handler — the sweep may be wrong. It reads "
        "dependencies plus a text heuristic over the handler body, so a gate "
        "reached through a helper, or an ownership check on the record itself, "
        "is invisible to it.\n\n"
        "If it really is unprotected:\n"
        "  from gdx_dispatch.core.modules import require_permission\n"
        '  dependencies=[Depends(require_permission("invoices.write"))]\n'
        "  # keys are catalogued in gdx_dispatch/core/permissions.py\n\n"
        "⚠ Check who loses access first. The gate 403s every user whose role "
        "snapshot lacks the key; only admin and owner pass via the BUILTIN "
        "union. If the route is legitimately open to any authenticated staff "
        "user, say so in review and add it to the baseline with a reason."
    )


def test_baseline_does_not_silently_grow(current: set[str]) -> None:
    """Report stale lines; fail only if the debt list GREW.

    Deliberately not a hard failure on staleness, for the reason the sibling
    ratchet gives: hardening work must never break the build. There is a
    sharper reason here too — ``app.py`` swallows router import errors and
    substitutes an EMPTY router, so one broken import deletes routes and makes
    their baseline lines look "already fixed". A hard failure saying "delete
    these lines" would have a developer ratify the loss of real debt while the
    routes were merely missing. Print, so a human reads it as a diagnosis.
    """
    stale = sorted(_baseline() - current)
    if stale:
        print(
            f"\n{len(stale)} baseline line(s) no longer appear in the sweep. "
            "Either they were gated (delete the lines — good), or their router "
            "failed to import and app.py swallowed it (fix the import — the "
            "debt is still there):\n  " + "\n  ".join(stale)
        )
    assert len(_baseline()) <= 422, (
        f"the debt list grew to {len(_baseline())} entries. Lines are removed "
        "by gating a route, never added to dodge the ratchet."
    )


# ── the control ──────────────────────────────────────────────────────────


def test_the_sweep_can_fail_for_its_own_defect() -> None:
    """A permission-gated mutation must NOT be flagged; a bare one must be."""
    from fastapi import APIRouter, Depends, FastAPI

    from gdx_dispatch.core.modules import require_permission
    from gdx_dispatch.routers.auth.core import get_current_user

    router = APIRouter()

    @router.post(
        "/probe/authenticated-only",
        dependencies=[Depends(get_current_user)],
    )
    def _authenticated_only() -> dict:
        return {}

    @router.post(
        "/probe/permission-gated",
        dependencies=[
            Depends(get_current_user),
            Depends(require_permission("settings.write")),
        ],
    )
    def _permission_gated() -> dict:
        return {}

    @router.get("/probe/read-only", dependencies=[Depends(get_current_user)])
    def _read_only() -> dict:
        return {}

    app = FastAPI()
    app.include_router(router)
    flagged = set(unpermissioned_mutations(app))

    assert "POST /probe/authenticated-only" in flagged, (
        "the sweep did not flag a mutation with authentication and no "
        "authorization — it cannot fail for the defect it exists to catch"
    )
    assert "POST /probe/permission-gated" not in flagged, (
        "require_permission must count as authorization"
    )
    assert "GET /probe/read-only" not in flagged, (
        "reads are deliberately out of scope; only mutations are swept"
    )

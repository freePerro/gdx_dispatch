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
    STALE_GATED,
    STALE_OTHER,
    STALE_UNREGISTERED,
    _first_registration_dependencies,
    classify_stale,
    unpermissioned_mutations,
)

BASELINE_PATH = Path(__file__).resolve().parents[2] / ".authz_unpermissioned_baseline"

# Pinned EXACTLY, not as a ceiling — see the sibling ratchet's BASELINE_SIZE.
# The freeze was 405 lines under a 422 ceiling and the file has since been
# worked down to 370, so the ceiling was carrying 52 lines of slack.
# 370 → 372 (2026-09-28): the planner Today tab's two own-records routes;
# reason recorded in the baseline file.
BASELINE_SIZE = 372


def _baseline() -> set[str]:
    lines = BASELINE_PATH.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}


@pytest.fixture(scope="module")
def _app():
    """One assembled app for the whole module — `create_app()` is not cheap."""
    from gdx_dispatch.app import create_app

    return create_app()


@pytest.fixture(scope="module")
def current(_app) -> set[str]:
    return set(unpermissioned_mutations(_app))


@pytest.fixture(scope="module")
def table(_app) -> dict[str, set[str]]:
    """The same route table the sweep judged, for classifying stale lines."""
    return _first_registration_dependencies(_app)


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
    # 23 → 24 (2026-09-18): verify_cell_secret, AUTHN side — a shared secret
    # authenticates the phone-side caller; it grants no per-user rights.
    assert len(AUTH_DEPENDENCIES) == 24, (
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


def test_baseline_does_not_silently_grow(
    current: set[str], table: dict[str, set[str]]
) -> None:
    """Report stale lines; fail only if the debt list GREW.

    Deliberately not a hard failure on staleness, for the reason the sibling
    ratchet gives: hardening work must never break the build. There is a
    sharper reason here too — ``app.py`` swallows router import errors and
    substitutes an EMPTY router, so one broken import deletes routes and makes
    their baseline lines look "already fixed". A hard failure saying "delete
    these lines" would have a developer ratify the loss of real debt while the
    routes were merely missing. Print, so a human reads it as a diagnosis.

    The report splits the cases (2026-09-27, GDXA-90) rather than making the
    reader hold the disjunction. Note the third bucket, which this sweep has and
    the sibling structurally cannot: ``unpermissioned_mutations()`` requires an
    AUTHN dependency, so a line also vanishes when the route stops
    authenticating ANYONE. That is a bigger hole, not a paid debt — which is why
    ``STALE_GATED`` is decided by a visible AUTHZ gate and never by the route
    merely still being registered.
    """
    stale = sorted(_baseline() - current)
    if stale:
        buckets = classify_stale(
            stale, table, AUTHZ_DEPENDENCIES, in_body_counts=True
        )
        report = [f"\n{len(stale)} baseline line(s) no longer appear in the sweep."]
        if buckets[STALE_GATED]:
            report.append(
                f"\n{len(buckets[STALE_GATED])} now carry an AUTHORIZATION gate "
                "(a require_permission/require_role dependency, or a role gate "
                "in the handler body) — delete the lines and lower "
                "BASELINE_SIZE to match, the debt is paid:\n  "
                + "\n  ".join(buckets[STALE_GATED])
            )
        if buckets[STALE_UNREGISTERED]:
            report.append(
                f"\n{len(buckets[STALE_UNREGISTERED])} are NOT REGISTERED at all, "
                "which is not the same as gated. Establish which before "
                "pruning: the route was deleted (prune — good), or its router "
                "failed to import and app.py substituted an empty one (fix the "
                "import — the debt is still open):\n  "
                + "\n  ".join(buckets[STALE_UNREGISTERED])
            )
        if buckets[STALE_OTHER]:
            report.append(
                f"\n⚠ {len(buckets[STALE_OTHER])} are registered with NO "
                "authorization gate and STILL left the sweep, which means they "
                "no longer authenticate anyone either. Do NOT prune these — the "
                "route moved from 'authenticated but unauthorized' to fully "
                "OPEN. Check .authz_ungated_baseline and the route's "
                "dependencies:\n  " + "\n  ".join(buckets[STALE_OTHER])
            )
        print("\n".join(report))
    assert len(_baseline()) == BASELINE_SIZE, (
        f"{BASELINE_PATH.name} holds {len(_baseline())} entries, pinned at "
        f"{BASELINE_SIZE}.\n"
        "GREW? Lines are removed by gating a route, never added to dodge the "
        "ratchet. If a mutation is legitimately open to any authenticated staff "
        "user, say so in review, add it with a reason, and raise BASELINE_SIZE "
        "in the same commit.\n"
        "SHRANK? Good — lower BASELINE_SIZE to "
        f"{len(_baseline())} in this commit, so the next addition is caught "
        "instead of absorbed by slack."
    )


# ── the control ──────────────────────────────────────────────────────────


def test_a_mutation_that_lost_its_authentication_is_not_reported_as_paid() -> None:
    """The control for THIS module's stale report (2026-09-27, GDXA-90).

    ``unpermissioned_mutations()`` requires an AUTHN dependency, so a baseline
    line also disappears when a route stops authenticating anyone — a route that
    went from "any logged-in user may" to "anyone at all may". The first draft of
    the split report classified by registration alone and printed "still
    registered and NOW CARRY an authorization gate — the debt is paid" for
    exactly that route, reintroducing the false reassurance this commit exists
    to delete. Pin the distinction.
    """
    from fastapi import APIRouter, Depends, FastAPI

    from gdx_dispatch.core.modules import require_permission
    from gdx_dispatch.routers.auth.core import get_current_user

    router = APIRouter()

    @router.post(
        "/probe/now-authorized",
        dependencies=[
            Depends(get_current_user),
            Depends(require_permission("settings.write")),
        ],
    )
    def _now_authorized() -> dict:
        return {}

    @router.post("/probe/lost-its-auth")
    def _lost_its_auth() -> dict:
        return {}

    app = FastAPI()
    app.include_router(router)
    table = _first_registration_dependencies(app)

    stale = [
        "POST /probe/now-authorized",
        "POST /probe/lost-its-auth",
        "POST /probe/deleted",
    ]
    buckets = classify_stale(stale, table, AUTHZ_DEPENDENCIES, in_body_counts=True)

    assert buckets[STALE_GATED] == ["POST /probe/now-authorized"], (
        f"only the permission-gated probe paid its debt: {buckets}"
    )
    assert buckets[STALE_OTHER] == ["POST /probe/lost-its-auth"], (
        "a mutation that lost its authentication must NOT be reported as "
        f"authorized — it is now fully open: {buckets}"
    )
    assert buckets[STALE_UNREGISTERED] == ["POST /probe/deleted"], (
        f"an unregistered route must not be called gated: {buckets}"
    )
    # Both probes really are absent from the sweep, so all three lines would
    # genuinely have gone stale — the classifier is the only thing telling them
    # apart, which is what makes this a control and not a tautology.
    flagged = set(unpermissioned_mutations(app))
    assert "POST /probe/now-authorized" not in flagged
    assert "POST /probe/lost-its-auth" not in flagged


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

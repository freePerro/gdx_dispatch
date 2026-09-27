"""Ratchet: no NEW route may ship without authentication.

Replaces the hand-maintained approach in ``test_authz_regression.py``, whose
18 hardcoded paths were frozen at the 2026-06-24 sweep. Nothing shipped after
that date was covered — which is how six unauthenticated ``/api/payments/*``
endpoints (real ACH debit, cross-invoice payment replay) stayed open for
months while every test stayed green.

This sweeps the REAL ``create_app()`` route table instead of a list someone
has to remember to update.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gdx_dispatch.tests.authz_sweep import (
    AUTH_DEPENDENCIES,
    STALE_GATED,
    STALE_OTHER,
    STALE_UNREGISTERED,
    _first_registration_dependencies,
    classify_stale,
    ungated_routes,
)

BASELINE_PATH = Path(__file__).resolve().parents[2] / ".authz_ungated_baseline"

# The baseline's length, pinned EXACTLY rather than as a ceiling. A ceiling
# rots: 85 was set on 2026-09-03 and survived every prune after it (#655,
# #782, and six more in GDXA-90), so by 2026-09-27 the "ratchet" was carrying
# 24 lines of slack and could not catch the next 24 additions. Equality means a
# prune that forgets to lower this number is red, once, with a one-line fix —
# and gating a route never reddens it, because gating changes the SWEEP, not
# the file.
BASELINE_SIZE = 61


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
    # Via `ungated_routes()`, never by re-implementing its predicate here: two
    # copies of that filter is how a sweep and its own report start disagreeing
    # about which routes they are judging.
    return set(ungated_routes(_app))


@pytest.fixture(scope="module")
def table(_app) -> dict[str, set[str]]:
    """The same route table the sweep judged, for classifying stale lines."""
    return _first_registration_dependencies(_app)


def test_no_new_unauthenticated_routes(current: set[str]) -> None:
    """Every ungated route must already be in the baseline."""
    new = sorted(current - _baseline())
    assert not new, (
        "These routes are reachable with NO authentication and are not in "
        f"{BASELINE_PATH.name}:\n  "
        + "\n  ".join(new)
        + "\n\nAdd an auth dependency (get_current_user, require_permission, "
        "_current_portal_user, ...). If the route is genuinely public — a "
        "signature-verified webhook, a token-authorized customer link, "
        "/health — say so in review and add it to the baseline with a reason.\n"
        "NOTE: a security scheme declared with auto_error=False does NOT "
        "authenticate; it permits anonymous callers through while making the "
        "signature look guarded. That was the /api/payments bug."
    )


def test_baseline_does_not_silently_grow(
    current: set[str], table: dict[str, set[str]]
) -> None:
    """The baseline is a debt list — it must shrink, never quietly expand.

    Stale entries are fine and are reported, not failed, so hardening work never
    breaks the build.

    The report separates the reasons a line goes stale, because they call for
    opposite reactions. It used to print "now authenticated" for every stale
    line, and on 2026-09-27 six of the seven stale lines were routes that had
    been DELETED (#605, #578) — in a security ratchet's own output that sentence
    invites a reviewer to believe an endpoint is live and guarded when it does
    not exist. The buckets and the caveats on each live in
    ``authz_sweep.classify_stale``, shared with the sibling ratchet.
    """
    stale = sorted(_baseline() - current)
    if stale:
        buckets = classify_stale(stale, table, AUTH_DEPENDENCIES)
        report = [f"\n{len(stale)} baseline line(s) no longer appear in the sweep."]
        if buckets[STALE_GATED]:
            report.append(
                f"\n{len(buckets[STALE_GATED])} are NOW AUTHENTICATED — hardening "
                f"landed, prune them from {BASELINE_PATH.name} and lower "
                "BASELINE_SIZE to match:\n  " + "\n  ".join(buckets[STALE_GATED])
            )
        if buckets[STALE_UNREGISTERED]:
            report.append(
                f"\n{len(buckets[STALE_UNREGISTERED])} are NOT REGISTERED in this "
                "route table. Do NOT read that as 'gated' — establish which "
                "before pruning:\n"
                "  • the route was deleted (prune the line — good), or\n"
                "  • its router failed to import and app.py substituted an "
                "empty one (fix the import — the debt is still open), or\n"
                "  • it is registered conditionally and this environment does "
                "not meet the condition (keep the line — GET /{full_path:path} "
                "needs frontend/dist, which no test environment builds):\n  "
                + "\n  ".join(buckets[STALE_UNREGISTERED])
            )
        if buckets[STALE_OTHER]:
            report.append(
                f"\n{len(buckets[STALE_OTHER])} are registered with NO auth "
                "dependency and still absent from the sweep. That should be "
                "impossible here — such a route is by definition ungated, so it "
                "cannot be stale. Read it as a defect in ungated_routes(), not "
                "as hardening:\n  " + "\n  ".join(buckets[STALE_OTHER])
            )
        print("\n".join(report))
    # 91 → 93 (2026-08-13): POST /api/proposals/{token}/accept + /decline —
    # the public estimate approval page. Group 2, token IS the credential
    # (64-char Estimate.public_token, sent_at-gated, uniform 404, row-locked);
    # same authorization model the /api/payments/* endpoints pin below.
    # 93 → 94 (2026-08-18): POST /api/proposals/{token}/deposit/pay — mints
    # the deposit invoice at pay-click (accept no longer auto-mints; only an
    # online payment counts). Same Group-2 token model as accept/decline;
    # accepted-only (409), row-locked, idempotent, amount server-derived
    # from the tenant deposit percent — never caller input.
    # 94 → 85 (2026-09-03): the SaaS-residue purge deleted nine of these
    # routes outright (/superadmin, the six /legacy/* tenant-UI handlers,
    # the /integrations Jinja page, and the dismissed-recommendation POST
    # whose router was the dead half of a shadowed pair). Pruned, not fixed.
    # 85 → BASELINE_SIZE = 61 (2026-09-27, GDXA-90), and from a ceiling to an
    # equality — see the constant for why a ceiling could not hold. The pin is
    # on the FILE's line count, not the sweep result, so it stays deterministic
    # across environments even though GET /{full_path:path} only registers when
    # frontend/dist exists.
    assert len(_baseline()) == BASELINE_SIZE, (
        f"{BASELINE_PATH.name} holds {len(_baseline())} entries, pinned at "
        f"{BASELINE_SIZE}.\n"
        "GREW? It is a debt list to work down, not a place to record new "
        "exceptions. If a new public-by-design route really belongs here, say "
        "so in review, add it with a reason, and raise BASELINE_SIZE in the "
        "same commit.\n"
        "SHRANK? Good — hardening or a deletion landed. Lower BASELINE_SIZE to "
        f"{len(_baseline())} in this commit, so the next addition is caught "
        "instead of absorbed by slack."
    )


def test_classify_stale_can_fail_for_its_own_defect() -> None:
    """The control for the split report — on the classifier, not on its inputs.

    An earlier version of this test asserted only that the route-table helper
    returns what it says. That could not fail for the defect: inverting the
    report's own ``in table`` test left the module 6/6 green while it printed
    "NOW AUTHENTICATED" for ``GET /{full_path:path}`` — the one line this commit
    pinned as must-not-prune. So drive ``classify_stale`` directly, with one
    probe route per bucket, and assert the buckets.

    ``STALE_GATED`` must require a VISIBLE gate, not mere registration. That is
    the whole point: in the sibling authorization sweep a line also disappears
    when the route loses its authentication, and calling that "the debt is paid"
    reintroduces exactly the false reassurance being removed here.
    """
    from fastapi import APIRouter, Depends, FastAPI

    from gdx_dispatch.routers.auth.core import get_current_user

    router = APIRouter()

    @router.get("/probe/gated", dependencies=[Depends(get_current_user)])
    def _gated() -> dict:
        return {}

    @router.get("/probe/wide-open")
    def _wide_open() -> dict:
        return {}

    app = FastAPI()
    app.include_router(router)
    table = _first_registration_dependencies(app)

    stale = ["GET /probe/gated", "GET /probe/wide-open", "GET /probe/deleted"]
    buckets = classify_stale(stale, table, AUTH_DEPENDENCIES)

    assert buckets[STALE_GATED] == ["GET /probe/gated"], (
        "a registered route carrying get_current_user is the only one of the "
        f"three that was hardened: {buckets}"
    )
    assert buckets[STALE_UNREGISTERED] == ["GET /probe/deleted"], (
        f"a route absent from the table must not be called gated: {buckets}"
    )
    assert buckets[STALE_OTHER] == ["GET /probe/wide-open"], (
        "a registered route with NO auth dependency must NOT land in "
        f"STALE_GATED — registration is not a gate: {buckets}"
    )
    # And the sweep agrees the wide-open probe is ungated, so the two halves of
    # the report are measuring different things.
    assert "GET /probe/wide-open" in set(ungated_routes(app))
    assert "GET /probe/gated" not in set(ungated_routes(app))


def test_the_spa_catch_all_stays_in_the_baseline() -> None:
    """GDXA-90's near-miss, pinned.

    ``GET /{full_path:path}`` is declared inside ``if _frontend_dist.exists():``
    (``app.py:1930``, route at ``:1940``), and no test environment builds the
    frontend — CI's
    ``test`` job never runs ``npm run build``, that is the separate ``frontend``
    job. So the line reads as stale in every sweep run while the route is live
    in every deployed container, and the 2026-09-27 prune came within one line
    of deleting it on that evidence.
    """
    assert "GET /{full_path:path}" in _baseline(), (
        "GET /{full_path:path} was pruned from .authz_ungated_baseline. It "
        "looks stale because frontend/dist is absent in test environments, not "
        "because the route is gone — app.py registers the SPA catch-all only "
        "when that directory exists, so pruning it reddens "
        "test_no_new_unauthenticated_routes for anyone running the suite "
        "against a tree with a built frontend. Put the line back."
    )


def test_payment_endpoints_are_token_authorized_not_open(current: set[str]) -> None:
    """The /api/payments/* endpoints have no auth dependency BY DESIGN — the
    anonymous customer proves which invoice they may touch with its token.

    That design is only safe because authorization is enforced inside the
    handlers, so pin the guarantee here: the invoice must come from the token,
    and the request must not be able to choose the amount or the payment
    method. The behavioural proof lives in test_payments.py; this asserts the
    endpoints still exist in the shape this reasoning assumes.
    """
    from gdx_dispatch.core.payments import (
        _amount_cents,
        _idempotency_key,
        _resolve_public_invoice,
    )

    assert callable(_resolve_public_invoice)
    assert callable(_amount_cents)
    assert callable(_idempotency_key)

    # The two unauthenticated payment-method endpoints were deleted; if they
    # come back they must not come back ungated.
    assert "GET /api/payments/methods" not in current
    assert "DELETE /api/payments/methods/{pm_id}" not in current


def test_the_tenant_wide_payment_list_has_no_ungated_twin():
    """M17. `core/payments.py` carried an UNAUTHENTICATED tenant-wide
    `GET /api/payments` returning the whole AR book. It was unreachable only
    because ui_compat's authenticated handler for the same path registers
    first (app.py:1670 vs :1756) and FastAPI is first-match-wins.

    That safety was accidental. `app.py` substitutes an EMPTY router when the
    ui_compat import fails, and with that branch taken the twin became live —
    proven 2026-08-23 against a real container: HTTP 200, 200 payment rows,
    invoice numbers and amounts, no credentials sent. Afterwards, the same
    simulation returns 404.

    `authz_sweep` deliberately ignores shadowed duplicates, so it could never
    have caught this. Asserted on the ROUTER rather than the assembled app for
    exactly that reason: the app hides the twin, the router cannot.
    """
    from gdx_dispatch.core.payments import router as core_payments_router

    # The router stores the FULL path including its own `/api/payments`
    # prefix — NOT the "" the decorator was written with. An earlier version
    # of this test looked for "" and therefore passed with the twin restored:
    # a guard that could not fail, caught by counterfactually re-adding the
    # route it exists to forbid.
    listing = [
        r for r in core_payments_router.routes
        if str(getattr(r, "path", "")).rstrip("/") == "/api/payments"
        and "GET" in (getattr(r, "methods", None) or set())
    ]
    assert not listing, (
        "core/payments.py has re-grown a tenant-wide GET listing. It is "
        "shadowed by ui_compat's authenticated handler ONLY while that import "
        "succeeds — app.py falls back to an empty router when it does not, and "
        "then this one serves the whole payment book to anonymous callers."
    )

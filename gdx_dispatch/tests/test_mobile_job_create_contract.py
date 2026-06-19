"""Regression brake — pin the role + route contract that lets a tech
create a job-with-customer-and-parts from the mobile dialog.

Doug 2026-05-10: "tech need to be able to make a new job, and add a new
customer and parts while doing it." The frontend dialog
(`MobileJobNewDialog.vue`) assumes:

- A technician's builtin permission set contains `jobs.write` (so /api/jobs
  POST passes the user's role/permission check) and `inventory.write` (so
  /api/jobs/{id}/parts-needed POST passes its `require_permission` gate).
- `POST /api/customers` has NO `require_permission(...)` dependency at the
  router level — the dialog calls it from a tech context, and a tightening
  to e.g. `customers.write` would silently break the new-customer toggle
  (technicians have only `customers.read_own` per `BUILTIN_ROLES`).
- `POST /api/jobs` likewise has no `require_permission` dep at the router
  level — authentication + module access only.

These are the **structural** invariants the dialog rides on. If a future
hardening pass changes any of them, this test fires and forces the change
to be made deliberately (either widen the technician role, or update the
mobile dialog to match the new gates).

This is intentionally a static-source test — no DB, no app spin-up.
Spinning up the full app for one assertion is overkill; the assertion
we want is "the route's source text doesn't contain require_permission",
which is exactly what a string scan gives us reliably and fast.
"""
from __future__ import annotations

import re
from pathlib import Path

from gdx_dispatch.core.permissions import BUILTIN_ROLES

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_technician_has_jobs_write_and_inventory_write() -> None:
    perms = set(BUILTIN_ROLES["technician"])
    assert "jobs.write" in perms, (
        "Technicians lost jobs.write — the mobile-job-create dialog will "
        "403 for techs. Either restore jobs.write on the technician role "
        "or remove the dialog button for techs."
    )
    assert "inventory.write" in perms, (
        "Technicians lost inventory.write — adding parts to a job from "
        "the mobile dialog will 403. Either restore inventory.write or "
        "hide the Parts section in MobileJobNewDialog (canAddParts gate "
        "would still need updating)."
    )


def _post_root_decorator(source: str) -> str:
    """Return the @router.post('') decorator text + its dependencies kwarg."""
    # Match `@router.post("", ...)` up to the next def, capturing args
    # (multi-line possible). The empty path means "router prefix only".
    m = re.search(
        r"@router\.post\(\s*\"\"(?P<args>[^)]*)\)\s*\n",
        source,
        re.DOTALL,
    )
    assert m, "Could not locate @router.post('') in source"
    return m.group(0)


def test_jobs_create_has_no_router_permission_gate() -> None:
    """POST /api/jobs (route path "" with prefix /api/jobs) must not carry
    a `require_permission(...)` router-level dependency. The mobile dialog
    relies on this being a get_current_user-only endpoint."""
    source = _read("gdx_dispatch/routers/jobs.py")
    dec = _post_root_decorator(source)
    assert "require_permission" not in dec, (
        "POST /api/jobs grew a require_permission dep. The mobile-job-"
        "create dialog will start 403'ing for any role missing the new "
        "permission. Either widen the technician role to include it, or "
        "update MobileJobNewDialog.vue to gate visibility on the new "
        "permission. Decorator text:\n" + dec
    )


def test_customers_create_has_no_router_permission_gate() -> None:
    """POST /api/customers must not carry require_permission. The mobile
    dialog's "Create new customer" toggle assumes a tech (with only
    `customers.read_own`) can POST it."""
    source = _read("gdx_dispatch/routers/customers.py")
    dec = _post_root_decorator(source)
    assert "require_permission" not in dec, (
        "POST /api/customers grew a require_permission dep. The mobile "
        "dialog's new-customer toggle will 403 for techs. Either widen "
        "the technician role to include the new permission, or change "
        "the dialog to skip create-new-customer for techs and surface a "
        "prefilled office handoff instead. Decorator text:\n" + dec
    )


def test_router_modules_match_dialog_assumption() -> None:
    """The dialog promises only "auth + module access" for the create
    endpoints. The require_permission absence tests above cover the
    permission half; this test pins the module half — if a future change
    swaps `require_module("jobs")` for `require_module("dispatch")`, the
    tech (who has the `jobs` module by default) would hit a 403 they can
    no longer hear from the contract tests above."""
    jobs_src = _read("gdx_dispatch/routers/jobs.py")
    customers_src = _read("gdx_dispatch/routers/customers.py")

    # Match `APIRouter(prefix="/api/jobs", ..., dependencies=[Depends(require_module("jobs"))])`
    # — bracket-counting since the args span multiple kwargs.
    def _router_init(source: str, prefix: str) -> str:
        anchor = f'APIRouter(prefix="{prefix}"'
        idx = source.find(anchor)
        assert idx >= 0, f"APIRouter init for prefix={prefix!r} not found"
        paren = source.index("(", idx)
        depth = 0
        for i in range(paren, len(source)):
            ch = source[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return source[idx : i + 1]
        raise AssertionError(f"Unterminated APIRouter init for prefix={prefix!r}")

    jobs_init = _router_init(jobs_src, "/api/jobs")
    assert 'require_module("jobs")' in jobs_init, (
        "/api/jobs router lost its require_module(\"jobs\") gate. The "
        "mobile dialog assumes any tech with module access can post; "
        "if this changed, MobileJobNewDialog needs to gate on the new "
        "module too. APIRouter init:\n" + jobs_init
    )

    customers_init = _router_init(customers_src, "/api/customers")
    assert 'require_module("customers")' in customers_init, (
        "/api/customers router lost its require_module(\"customers\") "
        "gate. Update MobileJobNewDialog accordingly. APIRouter init:\n"
        + customers_init
    )


def test_parts_needed_post_keeps_inventory_write_gate() -> None:
    """The Parts section is gated client-side on inventory.write — verify
    the backend still enforces the same. If the backend gate changes, the
    client-side gate must change with it (otherwise office users see the
    section and 403 on submit, OR techs lose the Parts section despite
    being able to submit)."""
    source = _read("gdx_dispatch/routers/parts_needed.py")
    # Multi-line decorator: walk forward from the opening paren counting
    # bracket nesting until the matching close. Regex with greedy
    # alternation can't reliably handle this when args contain a list.
    needle = '@router.post('
    start = source.find(needle + '\n    "/jobs/{job_id}/parts-needed"')
    if start < 0:
        # Single-line variant fallback.
        start = source.find('@router.post("/jobs/{job_id}/parts-needed"')
    assert start >= 0, "Could not locate @router.post('/jobs/{job_id}/parts-needed') in source"
    paren_open = source.index('(', start)
    depth = 0
    end = paren_open
    for i in range(paren_open, len(source)):
        ch = source[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    decorator = source[start:end]
    assert 'require_permission("inventory.write")' in decorator, (
        "The Parts add-route changed its permission gate. Update "
        "MobileJobNewDialog.vue's `canAddParts` computed to match. "
        "Decorator text:\n" + decorator
    )

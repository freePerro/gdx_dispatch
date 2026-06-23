"""Route-shadow regression gate.

Two FastAPI handlers on the same `(method, path)` pair silently collide —
whichever was `include_router`'d first wins; the other is dead code that
flips behavior the next time include order changes. The Settings → Modules
empty-panel bug (S100, fix 0aa0056f) was one such pair: `branding_public.py`
shadowed `settings.py` and returned a thin shape that broke the admin tab.

This test fails CI when a NEW shadow is introduced over the recorded
baseline at `gdx_dispatch/tools/route_shadow_baseline.txt`. The baseline starts
at 55 (the count when the gate landed) and is driven to 0 by sprint
`ai-queue/plans/sprint_three_plane_cleanup.md` S2/S3.

To re-baseline after intentionally resolving shadows:
    .venv/bin/python gdx_dispatch/tools/route_shadow_scan.py --write-baseline
"""
from __future__ import annotations

from gdx_dispatch.tools.route_shadow_scan import collect_shadows, load_baseline


def test_no_net_new_route_shadows() -> None:
    """No net-new (method, path) shadow pairs vs baseline."""
    current = set(collect_shadows().keys())
    baseline = load_baseline()
    net_new = sorted(current - baseline)
    assert not net_new, (
        f"{len(net_new)} net-new route shadow(s) introduced. Each pair has "
        "two handlers registered against the same (method, path) — only one "
        "wins by include order, the other is dead code that will silently "
        "flip behavior. Either delete the duplicate, or if intentional run "
        "`python gdx_dispatch/tools/route_shadow_scan.py --write-baseline`. "
        f"New shadows: {net_new}"
    )


def test_settings_modules_returns_rich_shape() -> None:
    """`/api/settings/modules` must return tier-grouped shape, not the thin one.

    Direct regression for the S100 bug: SettingsView.modulesByTier filters
    by `item.tier === tier`, so a missing `tier` field renders the panel
    empty. Asserts the canonical winning handler still returns the rich
    shape regardless of include-order changes elsewhere.
    """
    from gdx_dispatch.tools.route_shadow_scan import collect_shadows

    # The route may be shadowed (acceptable while baseline > 0), but whichever
    # handler wins must produce the rich shape. We reach the winning handler
    # via the live FastAPI app's route table — same path FastAPI dispatches.
    import os
    os.environ.setdefault(
        "JWT_SECRET",
        "test-jwt-secret-at-least-32-bytes-long-for-hs256-sha256-safety",
    )
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    target = None
    for full_path, route in iter_app_routes(app):
        if full_path == "/api/settings/modules" and \
                "GET" in (getattr(route, "methods", None) or set()):
            target = route
            break

    assert target is not None, "/api/settings/modules GET not registered"

    # Inspect the winning handler's source — it must read MODULES from
    # gdx_dispatch.core.modules (the rich source) and emit `tier` / `name` / `locked`.
    # Static analysis is enough; we don't need to invoke with a fake DB.
    import inspect
    src = inspect.getsource(target.endpoint)
    for required_field in ('"tier"', '"name"', '"locked"', '"upgrade_required"'):
        assert required_field in src, (
            f"Winning handler for GET /api/settings/modules ({target.endpoint.__module__}:"
            f"{target.endpoint.__qualname__}) does not emit {required_field}. "
            "SettingsView.modulesByTier needs the rich shape — see S100."
        )

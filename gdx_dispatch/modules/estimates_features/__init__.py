"""Per-tenant Estimates feature toggles (2026-04-30).

Single toggle today:
  - estimates_allow_line_margin_override
        When false, the per-line margin override input is hidden in the
        UI and rejected by the API. Default true (existing behavior).

A new card lives in Admin Settings → Estimates Features. Future
estimate-level toggles (whole-estimate margin override, etc.) land
here without changing routes.
"""
from gdx_dispatch.modules.estimates_features.service import (  # noqa: F401
    EstimatesFeatures,
    get_features,
    require_line_margin_override_allowed,
)

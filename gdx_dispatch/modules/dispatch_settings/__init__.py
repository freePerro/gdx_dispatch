"""Per-tenant Dispatch settings (2026-05-01).

Three toggles control behavior when a job is scheduled without a tech:
  - dispatch_warn_save_no_tech — soft gate (UI confirm dialog on save)
  - dispatch_block_save_no_tech — hard gate (server returns 422)
  - dispatch_show_unassigned_lane — surface a "Scheduled — Not Assigned"
        lane on the Dispatch board, alongside holding areas.

All default false so existing tenants see no behavior change.
"""
from gdx_dispatch.modules.dispatch_settings.service import (  # noqa: F401
    DispatchSettings,
    get_settings,
    require_tech_for_scheduled_job,
)

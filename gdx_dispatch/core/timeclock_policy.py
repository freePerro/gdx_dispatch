"""Open-shift thresholds and the auto-close note — a leaf module on purpose.

The celery sweep (`tasks/timeclock_sweep.py`) is imported by
`core/celery_app.py` at load time, and `celery_app` is reached from deep
inside model loading (`models` → `core.integrations` → `core.webhooks`). If
the sweep takes these names from `routers/timeclock.py` or from
`core/timesheet_hours.py`, whichever of those loads first is still
half-initialised when the sweep asks for them, the import fails, and
`core/webhooks/__init__.py` swallows that ImportError by setting
`emit_webhook` to None (GDXA-1888). So nothing here may import from
`gdx_dispatch`: `core/timesheet_hours.py` and `routers/timeclock.py`
re-export these names for their existing callers.
"""
from __future__ import annotations

# MH-7b — open-shift guard thresholds, shared by /status, clock-in auto-close,
# the celery sweep, the export and the emailed timesheet. Floats (hours).
WARNING_AFTER_HOURS = 8.0
MAX_SHIFT_HOURS = 16.0
IMPLAUSIBLE_SHIFT_MINUTES = int(MAX_SHIFT_HOURS * 60)

AUTO_CLOSE_NOTE = "Auto-closed — end time unknown, needs office review"

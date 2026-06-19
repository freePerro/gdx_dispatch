"""Per-tenant Job workflow flags (UX audit F-8 / 2026-04-29).

Default behavior on `Start Job` is hard-coded in `gdx_dispatch.routers.jobs.start_job`:
stamp `started_at` + auto-assign current user. The optional behaviors —
schedule lock, customer-timeline arrival event, arrival SMS, and
require-fields-on-complete — are tenant-toggleable.

This module owns the GET/PATCH endpoints + Pydantic shapes. The flags
themselves live as columns on `tenant_settings` (control-plane), seeded
by alembic migration 040.

Per Doug 2026-04-29: `b but the rest should be available as options to
be turned on and off per tenant`."""

"""Payroll module — UX audit F-82 / 2026-04-29.

Per Doug 2026-04-29: "there is true cost and estimated cost and time
tells that with the more data you collect. Lets make it part of the
payroll module? Payroll will be external. to start but then there can
be options for the tenant to choose from."

Two cost layers:

  1. **True cost** — comes from `payroll_entries`. One row per
     pay-period per tech, with hours_paid + gross_pay. Effective
     hourly rate = gross_pay / hours_paid. Job-costing prefers this
     when an entry covers the labor date.

  2. **Estimated cost** — falls back to Technician.hourly_rate when
     no payroll_entry covers the date range. Used until the tenant
     has been recording payroll long enough to have real data.

Sources:
  - 'manual'      — typed in the Payroll admin
  - 'csv_import'  — file upload (planned, not wired today)
  - 'gusto'       — Gusto API adapter (planned)
  - 'qbo_payroll' — QBO Payroll adapter (planned)
"""
from gdx_dispatch.modules.payroll.service import effective_labor_cost  # noqa: F401

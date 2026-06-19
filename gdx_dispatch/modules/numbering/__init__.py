"""Centralized record numbering — today: jobs, future: estimates/invoices.

Every business document needs a human-readable number. Different tenants
want different shapes ("JOB-001", "JOB-2026-001", "BM-2026-001",
"GDX-12345"). The platform owns the locking + counter; the format
template comes from TenantSettings.

Format template tokens:
  {seq}            raw integer ("1", "42")
  {seq:NNN}        zero-padded ("001", "0042")
  {year}           4-digit current year ("2026")
  {yy}             2-digit current year ("26")
  {month}          2-digit current month ("04")
  {customer_initials}   best-effort initials from passed customer name

Reserved for later: {site}, {region}, {tech_initials}.

The counter resets on Jan 1 only when the format includes {year} or {yy}
— the control-plane row tracks `job_number_year_seen` so a tenant who
moved to a non-year format mid-stream doesn't accidentally re-number.

Per Doug 2026-04-29 (UX audit F-11): "I like b but can we make it so the
tenant can choose how they are? or what sequential number to start with?"
"""
from gdx_dispatch.modules.numbering.service import (  # noqa: F401
    apply_template,
    next_job_number,
    preview,
)

# NOTE: do NOT auto-import the router here. The router pulls in
# gdx_dispatch.routers.auth, which fails fast outside an app context (no JWT
# key). app.py imports the router via its explicit path, same shape
# as gdx_dispatch.modules.tax.

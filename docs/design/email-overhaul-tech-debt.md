# Tech debt log — email overhaul build (feat/email-overhaul)

Debt found (not created) while building docs/design/email-readability-and-delivery-plan.md.
Each entry: what it is, where, why it wasn't fixed in this branch.

| # | Found | Where | Debt | Why deferred |
|---|-------|-------|------|--------------|
| 1 | 2026-08-18 | routers/communications.py | Outbound email/thread store is a process-local in-memory dict (`_EMAILS_BY_TENANT`) wiped on restart; endpoints look persistent but aren't. The new `outbound_emails` table supersedes it for transactional mail, but the communications screens still read the dict. | Migrating those screens to the new table is a UI project of its own; out of scope here. |
| 2 | 2026-08-18 | routers/supplier_invite.py | Docstring claims "supplier gets email with link"; no email is ever sent. | Needs a product decision on whether supplier invites should email at all. |

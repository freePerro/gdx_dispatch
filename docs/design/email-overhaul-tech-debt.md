# Tech debt log — email overhaul build (feat/email-overhaul)

Debt found (not created) while building docs/design/email-readability-and-delivery-plan.md.
Each entry: what it is, where, why it wasn't fixed in this branch.

| # | Found | Where | Debt | Why deferred |
|---|-------|-------|------|--------------|
| 1 | 2026-08-18 | routers/communications.py | Outbound email/thread store is a process-local in-memory dict (`_EMAILS_BY_TENANT`) wiped on restart; endpoints look persistent but aren't. The new `outbound_emails` table supersedes it for transactional mail, but the communications screens still read the dict. | Migrating those screens to the new table is a UI project of its own; out of scope here. |
| 2 | 2026-08-18 | routers/supplier_invite.py | Docstring claims "supplier gets email with link"; no email is ever sent. | Needs a product decision on whether supplier invites should email at all. |
| 3 | 2026-08-18 | routers/communications.py + core/email.py | The communications email path injects the legacy SES-style `core/email.py` sender and stores messages in the in-memory dict — separate from the transactional pipeline and the outbound_emails log. | Migrating the communications screens onto the audited pipeline is the same UI project as row 1. |
| 4 | 2026-08-18 | modules/workflows/engine.py | send_sms / create_followup_task / emit_webhook / update_job_field actions now honestly report `not_implemented` (previously "logged"). Executors still to build. | Out of email-overhaul scope; the send_email executor structure makes each mechanical. |
| 5 | 2026-08-18 | routers/supplier_invite.py | Invite mints a link but sends no email; docstring now says so. A real invite email through the pipeline is unbuilt. | Needs a product decision (who sends supplier mail, which template). |

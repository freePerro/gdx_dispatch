# Customer email overhaul — readability, missing features, silent non-delivery

Status: **MERGED #348 · RELEASED v1.68.0 · DEPLOYED to prod** — all six
phases built, with migrations 066-068 live. Verified on main 2026-08-21:
`core/email_layout.py` (P1), the single server-rendered send pipeline with a
`body_text` override (P2, `estimates.py:1583`), migration 067 +
`automation_emails_enabled` (P4a), migration 066 `outbound_emails` +
`routers/outbound_emails.py` + the Email Log screen (P5.1), `Estimate.sent_via`
(P5.4), `validate_email` wired at `customers.py:61/95` (P5.6), Reply-To at
`email_sender.py:86` (P5.7), `decrypt_secret` on `password_enc` (P5.8), and
migration 068 + `tasks/plugin_email_outbox.py` (P6).
**Phase 4b's third bullet is BLOCKED, not pending — and its premise is wrong.**
`gdx_dispatch/core/email.py` is **not dead code**. Re-verified 2026-08-21:
`routers/communications.py:15` imports it (`from gdx_dispatch.core import email
as email_service`) and injects it via `get_email_sender` into two mounted,
live endpoints — `POST /api/email/send` (:370) and `POST /api/communications/send`
(:597). `core/email.py::send_email` is a real SMTP/SES sender, not a stub.
`tests/test_email_sms.py` imports it too. Deleting it breaks both endpoints.
The finding at §6 below ("zero live references") was wrong when written.
Deletion is blocked behind migrating the communications screens off the legacy
sender and the in-memory `_EMAILS_BY_TENANT` dict — rows 1 and 3 of
[email-overhaul-tech-debt.md](email-overhaul-tech-debt.md), both deferred there
as "a UI project of its own". Phase 4b's other two bullets (supplier_invite
docstring, `/settings/email/test` returning Not-implemented) are done.

Locked decisions (Doug, 2026-08-18):

- Automation emails should be implemented for real, as an **option that can
  be turned on or off** — not stripped from the UI. See Phase 4a.
- **Estimate sends should honor the tenant-editable email template** (the
  `estimate_email_subject_template` / `estimate_email_body_template` columns
  that today only feed the composer preview). The template supplies the copy;
  the Phase 1 shell supplies layout/branding around it. See Phase 2.
- **Everything must be auditable** — plugin email (and the pipeline broadly)
  is approved on the condition that "we can figure out what went wrong."
  Acceptance test for the whole plan: for any email a customer did or didn't
  receive, the office can answer WHO/WHAT triggered it (user, bulk, reminder
  task, workflow rule, plugin key, n8n), WHAT it said (rendered body), WHO it
  went to (resolved recipient + why), and WHAT happened (provider, outcome,
  bounce) — from the app, without reading container logs. See Phase 5.1.

## The reported problem

Doug: emails are hard to read (not just the fonts), features visible in the
app don't show up in the email, and some backend data never makes it into the
message. Investigation confirms all three, plus a fourth: two email surfaces
silently send nothing at all.

## Findings

### A. Why the emails are hard to read

Every outbound email hand-rolls its own HTML — there is **no shared layout**,
and the two "designed" templates (`build_estimate_email_html` /
`build_invoice_email_html` in
[core/email_sender.py](../../gdx_dispatch/core/email_sender.py) L93/L159)
copy-paste the same wrapper with the same defects:

1. **Times New Roman line items in Outlook.** `font-family:Arial` is set only
   on the outer `<div>`. Outlook desktop (Word renderer) does not inherit
   font-family into `<td>` cells, so the entire line-item table — the heart of
   the email — renders in Times New Roman. Every send from GDX goes out via
   Outlook Graph and is reviewed in Outlook Sent Items, so this is exactly the
   "fonts are hard to read" symptom.
2. **Layout collapses in Outlook.** `max-width:600px;margin:0 auto` on a div
   is ignored by the Word renderer → the email sprawls full-width. Email-safe
   layout requires a table-based shell.
3. **The CTA button disappears in Outlook.** "View & Accept Estimate" /
   "View & Pay Invoice" is a padded `<a>` with `border-radius`. Outlook
   ignores anchor padding and radius → the button degrades to a small plain
   text link. This is the "cannot see other features" symptom: the single
   most important affordance is nearly invisible in the most-used client.
4. **Dark mode breaks everywhere.** No surface sets `background-color` on the
   text container; all hardcode dark text (#333 / #0f172a / #1e293b) assuming
   a white client background. Gmail app / Outlook dark mode invert or darken
   the background → low-contrast or invisible text.
5. **Three different blues, three font stacks.** #0057a8 (estimate/invoice),
   #2563eb (portal magic link), #3b82f6 (password reset); Arial vs system-ui
   vs bare sans-serif; the password-reset email even uses `rem` units, which
   mail clients handle unreliably.
6. **Some bodies have no styling at all.** The dunning/reminder email is
   literally `"<p>" + body.replace("\n", "<br>") + "</p>"`
   ([routers/invoice_reminders.py:445](../../gdx_dispatch/routers/invoice_reminders.py)).
   The mobile payment receipt is three bare `<p>` tags
   ([routers/mobile_invoicing.py:1288](../../gdx_dispatch/routers/mobile_invoicing.py)).
7. No plain-text MIME alternative on any send (deliverability + accessibility).

### B. Features that exist in the app but not in the email

- **No tenant branding.** `AppSettings` carries logo, phone, address, email,
  primary/secondary colors — the PDF generator uses all of them
  ([routers/pdf.py:60](../../gdx_dispatch/routers/pdf.py) `_branding_payload`);
  the emails use none. Header is text-only, footer is
  "{company} — Sent via GDX Platform" with no way to call the shop.
- **The reminder (dunning) email has no pay link.** `public_pay_url` is never
  called in the reminder path — the collections email gives the customer no
  way to pay. Template context is only
  invoice_number/customer_name/amount_due/days_overdue/due_date.
- **Planner digest CTA is a dead link** — relative href `/mobile/planner`
  ([tasks/planner_digest.py:~187](../../gdx_dispatch/tasks/planner_digest.py)),
  dead in every mail client.
- **Password reset carries zero tenant branding** — hardcoded "DispatchApp"
  ([routers/auth/core.py:691](../../gdx_dispatch/routers/auth/core.py)).

### C. Backend data that never shows up in the body

- **Tiered proposals are butchered.** `send_estimate`
  ([routers/estimates.py:1598](../../gdx_dispatch/routers/estimates.py))
  serializes flat `EstimateLine` rows and never checks `proposal_mode`. A
  good/better/best proposal emails as one flat item list — on mobile-built
  proposals the lines are *all three tiers' items mixed together* — under a
  single total that matches no tier. The tier presentation exists only on the
  public page and PDF.
- **"Valid for 30 days" is wrong.** Hardcoded copy in the estimate template;
  the real expiry is `valid_until` = sent_at + tenant `estimate_expiry_days`
  (default **60**). Worse, `_apply_send_expiry` runs *after* the body is
  built, so the real date isn't even available at compose time today.
- **No HTML escaping anywhere except the planner digest.** Customer names,
  company name, notes, and line descriptions are raw f-string-interpolated. A
  description like `panel <16ft>` silently disappears; `&` renders wrong; and
  it's an injection vector into customer-facing mail.
- **Multi-line descriptions collapse.** Newlines in line descriptions and
  notes are not converted to `<br>` (only estimate `description` gets that
  treatment) — a carefully formatted multi-line description arrives as one
  run-on line.
- **Inconsistent money formatting.** Line items go through `format_money`;
  subtotal/tax/total/balance use raw `${x:.2f}` — no thousands separators on
  the biggest numbers on the page.
- **Mobile receipt omits the remaining balance and company name** — comment
  in code admits "A dedicated receipt template can land later."
- **Business customers are greeted by their company name** (Doug,
  2026-08-18: "Hi Acme Lumber (a business account)" instead of the person's name). Every
  send path resolves the greeting from `Customer.name` and the recipient
  from `Customer.email`. The `customer_contacts` table (name/email/phone/
  label per person at a customer) exists precisely for this and **no email
  path reads it** — its own model docstring flags wiring lookups to contacts
  as the known follow-up. There is also no way to choose WHICH person at
  the account receives the estimate.

### D. Sends that silently don't happen (the sharpest "does not show up")

1. **Mobile payment receipt is SMTP-only.** It calls
   `core.email_sender.send_email` directly
   ([routers/mobile_invoicing.py:1296](../../gdx_dispatch/routers/mobile_invoicing.py))
   — the only tenant-facing send that bypasses `send_transactional_email`, so
   it never tries Outlook Graph. The S110 docstring records that GDX has no
   SMTP row in `email_settings`; if that's still true on prod, **every mobile
   payment receipt has silently not been delivered**. Verify on prod, then fix.
2. **Workflow/automation "send_email" actions send nothing.**
   `modules/workflows/engine.py` lists `send_email` in SUPPORTED_ACTIONS but
   `execute_rule` only logs `"result": "logged"`;
   `routers/automations.py` catalogs `send_email`/`send_welcome_email`
   with no execution path. Anything configured there looks alive and is not.
3. **Supplier invite claims an email that is never sent**
   ([routers/supplier_invite.py](../../gdx_dispatch/routers/supplier_invite.py)
   docstring says "supplier gets email with link"; the router sends nothing).
4. **Admin "test email" is a stub** —
   [routers/admin_settings.py:103](../../gdx_dispatch/routers/admin_settings.py)
   returns `{"ok": True, "message": "Test email queued"}` without sending.
5. **Tenant estimate email templates are compose-only.** The tenant-editable
   `estimate_email_subject_template`/`estimate_email_body_template` columns
   feed only the composer preview; `send_estimate` ignores them and uses the
   hardcoded builder. Only the reminder template is genuinely tenant-editable
   end-to-end.
6. ~~Dead code: [core/email.py](../../gdx_dispatch/core/email.py) (SES sender,
   zero live references).~~ **WRONG — corrected 2026-08-21.** It has live
   references: `routers/communications.py` injects it into `POST /api/email/send`
   and `POST /api/communications/send`. Struck rather than deleted because the
   mistake is the useful part — "zero live references" was asserted from a grep
   that missed the `from gdx_dispatch.core import email as email_service` form,
   and it survived into two later status lines before anyone tried the deletion.

### E. Delivery-lifecycle gaps (second-pass audit, 2026-08-18)

1. **The mobile receipt fakes success.** Beyond being SMTP-only (D1), it
   discards `send_email`'s return value and responds with a hardcoded
   `{"sent": True}` ([routers/mobile_invoicing.py:1285-1320](../../gdx_dispatch/routers/mobile_invoicing.py)),
   and stamps neither `sent_at` nor `sent_via`. The tech is told the customer
   got a receipt no matter what happened.
2. **No record anywhere of what a customer was sent.** SMTP sends set only
   Subject/From/To (no CC/BCC/archive); Graph saves to the sending rep's
   personal Sent Items only. `routers/communications.py` stores emails in an
   **in-memory dict wiped on every restart** — no DB table. No transactional
   send writes a communication row, and audit rows carry `{status, provider}`
   but never subject or body. If a customer disputes "that's not what you
   sent me," there is no answer.
3. **A bounced dunning email can UN-send a delivered invoice.** Bounce
   detection rung 2 matches on failed-recipient == customer email; reminder
   subjects never match rung 1 (no `" from "`), so an NDR for a *reminder*
   can fall through to rung 2 and clear `sent_at`/`sent_via` on an invoice
   that was delivered fine ([modules/outlook/bounce_detect.py:273-325](../../gdx_dispatch/modules/outlook/bounce_detect.py)).
   Receipts and magic links have zero bounce coverage; SMTP-only tenants
   have zero bounce detection at all.
4. **Send-receipt overwrites invoice delivery history.** The receipt path
   delegates to `send_invoice`, which re-stamps `sent_at` on the PAID
   invoice — the original send date is lost, and `sent_via="email"` can't
   distinguish invoice-send from receipt-send.
5. **Composer attachments have no size guard.** `/compose` returns the PDF
   base64 unchecked and the UI shows a locked "auto-attached" checkbox
   ([routers/invoices.py:1783](../../gdx_dispatch/routers/invoices.py),
   InvoiceDetailView.vue:607) — Graph rejects the *entire message* past
   ~4MB, so an oversized PDF means the customer gets nothing while the UI
   promised an attachment. The one-click paths have the 2.5MB guard; the
   composer and [modules/outlook/send_router.py:180](../../gdx_dispatch/modules/outlook/send_router.py)
   do not.
6. **Silent PDF degradation is invisible.** When the one-click guard skips
   an oversized PDF, `send_estimate`'s response has no `pdf_attached` field
   at all, and the invoice response's `pdf_attached` is consumed by no
   frontend file — a write-only flag.
7. **Estimate rows can't record delivery channel.** Estimate has `sent_at`
   but **no `sent_via` column**; mark-sent buries the channel in the audit
   blob. Violates the project rule (stamp BOTH) and forecloses an "Unsent
   estimates" view. Reminders similarly stamp only their own table.
8. **No server-side double-send guard on any endpoint.** Status checks block
   void/finalized, but an already-sent invoice re-sends freely; the only
   locks are frontend button flags. Two tabs or a retried request = two
   emails.
9. **No recipient validation.** `core/validation.py` has `validate_email` —
   dead code; customer email is a bare string. A typo'd address is caught
   only by NDR detection, only on Outlook tenants.
10. **No Reply-To on SMTP** — replies go to the configured from-address
    (often a relay), while Graph sends thread to the rep's own mailbox.
    Reply behavior silently differs by provider.
11. **`password_enc` is base64, not encryption**
    ([routers/email_settings.py:77](../../gdx_dispatch/routers/email_settings.py)) —
    plaintext SMTP credentials at rest under an encrypting name. SOC2-relevant.
12. Minor: subject lines use the bare serial while bodies/PDF names use the
    `str(id)[:8]` fallback — an empty-string serial breaks bounce rung-1
    matching and renders "Estimate # from Acme".

### F. The composer is the real estimate email — and it's plain text (KEY REFRAME)

**`POST /api/estimates/{id}/send` has no UI caller.** Nothing in
`frontend/src` hits it — so the branded `build_estimate_email_html` (and all
of section A/C's template critique) is dead code from the UI's perspective.
**Every estimate email actually sent from the app** goes through the composer
(`EstimateView.vue` → `GET /email-compose` → `POST /api/outlook/send`), which:

- wraps the tenant's plain-text template in a `<pre>` tag client-side — no
  logo, no line items, no totals table, no button. *This is the email Doug
  finds hard to read.*
- appends "Review & approve online: {url}" as **plain text and never
  linkifies it** — Outlook does not auto-link text inside an HTML body, so
  the approval link is unclickable. (`InvoiceDetailView.vue` linkifies its
  pay link; `EstimateView.vue` — same-shaped code — does not.) *This is
  "you cannot see other features."*
- drops the `customer_id`/`job_id` the compose endpoint returns, so the sent
  mail gets no job tag and no customer linkage on Outlook sync.
- has **no attachment size guard** — a photo-heavy estimate PDF 422s at the
  send router's per-attachment cap, surfaced as a generic "Outlook rejected
  the send."
- stamps sent via `POST /mark-sent` with a **hardcoded** audit channel
  `"manual"` — Outlook sends, mailto fallback, and true out-of-band sends
  are indistinguishable.

Invoices are split-brained: bulk send from Billing / mobile uses the branded
HTML `/send` path; sends from InvoiceDetailView's composer are `<pre>` plain
text. Same customer can get both identities in one week.

Template-system details that matter for the fix:

- Estimate templates (`{{placeholder}}`, plain text) are tenant-editable in
  Settings → Feature Settings; context keys: customer_name, job_title,
  estimate_number, estimate_label, company_name, total, estimate_link.
  **The help text omits `estimate_link` and `estimate_label`**, and the
  Settings preview text drifts from the actual default copy.
- **Unresolved placeholders ship to the customer verbatim** (`{{customer}}`
  typo → literal text); no validation on save, no preview.
- Invoice/receipt subject+body templates are **hardcoded consts** in
  routers/invoices.py — no tenant columns at all.

## Plan

### Phase 1 — shared email shell + branding (fixes readability everywhere)

New module `core/email_layout.py`:

- **Table-based 600px shell** (the email-safe classic): outer table with
  explicit `background-color:#ffffff` on the content cell, header band, body
  cell, footer. `font-family`, `font-size:15-16px`, `line-height` declared
  **on every `<td>`**, not just the wrapper.
- **Bulletproof CTA button**: padded `<td>` with background color + anchor,
  so Outlook shows a real button (VML fallback optional — the td approach is
  good enough and simpler).
- **Dark-mode survivable**: explicit backgrounds, off-white/off-black colors
  (avoid pure #fff/#000 which trigger the harshest inversions),
  `<meta name="color-scheme" content="light">` hint.
- **Tenant branding from `AppSettings`**: logo in the header (fall back to
  company-name text), `primary_color` for the header/CTA (fall back to
  current blue), footer with phone / address / email — same
  `_branding_payload` data the PDFs already use.
- **Helpers**: `esc()` (html.escape) applied to every interpolated value,
  `nl2br()` for descriptions/notes, and all money through `format_money`.
- Optional but cheap: derive a plain-text alternative part in
  `send_email` (SMTP multipart) — Graph accepts html-only fine.

Migrate `build_estimate_email_html` and `build_invoice_email_html` onto the
shell. Tests pinning current HTML strings will need updating:
test_estimates, test_tier9_documents, test_billing_office_paths,
test_critical_contract_fixes, test_transactional_email_attachments.

### Phase 2 — ONE send pipeline: composer and one-click deliver the same branded email

This is the heart of the fix, reframed by finding F. Today the composer
(the path Doug actually uses) sends unstyled `<pre>` text and the branded
builder is UI-dead. The fix is a single server-side render+send:

- **End state: exactly ONE builder** (Doug asked 2026-08-18). The current
  `build_estimate_email_html` is rewritten INTO the single renderer (its
  hardcoded "Dear {customer_name}" greeting deleted — copy comes from the
  tenant template + recipient resolver); the client-side `<pre>` builder in
  EstimateView/InvoiceDetailView is deleted entirely. The composer is an
  editor for the message portion only — line items/tiers, totals, CTA, and
  branding are always server-rendered and identical on every path. The
  composer should preview the final assembled email so what-you-see is
  what the customer gets.
- **New/extended send endpoint accepting a `body_text` override.**
  `POST /estimates/{id}/send` (and the invoice equivalent) takes optional
  `{body_text, subject, extra_attachment_ids}`. The composer keeps its
  editable plain-text Textarea, but on Send posts HERE instead of building
  `<pre>` HTML client-side and calling `/api/outlook/send`. The server
  renders: shell (Phase 1 branding) + escaped/nl2br'd copy + structured
  content (line table or tier summary, totals, **real CTA button**) +
  PDF attachment with the existing size guard. One-click send is the same
  endpoint with no override.
- **Honor the tenant email template at send time (locked).** The default
  copy for both composer prefill and one-click send comes from
  `estimate_email_subject_template` / `estimate_email_body_template`
  through `_render_template` — what the composer previews is what every
  path delivers. Missing/blank template → default copy.
- This collapses composer defects for free: clickable CTA replaces the
  unclickable plain-text link, consistent escaping, `customer_id`/`job_id`
  tagging server-side, size-guarded attachments, honest `mark-sent`
  semantics (the server send stamps; `mark-sent` remains only for true
  out-of-band delivery, with a real channel field).
- Invoice split-brain ends the same way: InvoiceDetailView's composer posts
  to the invoice send endpoint with its edited copy.
- **Recipient = a person, not just the account — via ONE shared resolver.**
  The company-name greeting appears at **seven sites**, not just estimates:
  estimate composer template, estimate branded builder ("Dear {name}"),
  invoice one-click/bulk/send-receipt builder, invoice composer template,
  mobile invoice fallback body, mobile payment receipt ("Hi {cust.name}"),
  and the reminder/dunning template — plus every SMTP/Graph To-name header
  ("Acme Lumber (a business account) <bob@…>"), and Phase 4a automation emails would
  inherit it too. So the fix is a single helper, not a per-screen patch:
  `resolve_recipient(customer, db, contact_id=None) -> (email, to_name,
  greeting_name)` in core, called by EVERY send path.
  - Composer paths: the compose endpoint lists the account email plus every
    live `CustomerContact` (name + label + email); the composer shows a
    recipient picker. Chosen contact → greeting placeholder
    (`customer_name`) and To-name resolve to the person — "Hi Bob," not
    "Hi Acme Lumber (a business account),". Add a `contact_name` placeholder alongside.
  - Automated/no-human paths (one-click, bulk, reminders, receipts,
    automations): **`is_primary` flag on CustomerContact** (promoted from
    optional to required — without it these paths can never greet the
    person). Resolver default: primary contact if set, else account
    email + account name (residential accounts: the name IS the person, so
    nothing regresses).
  - Deliberately unchanged: PDFs' bill-to, portal pages, and the public
    proposal "prepared for" line keep the ACCOUNT name — documents address
    the business; only greetings and To-names address a person. Portal
    magic link and password reset have no greeting (unaffected).
- **Template polish**: document `estimate_link`/`estimate_label` in the
  Settings help text, fix the preview/default drift, warn on unresolved
  `{{placeholders}}` at save time. Add tenant columns for invoice + receipt
  subject/body templates (currently hardcoded consts) so all three
  document types are editable the same way.
- **Honor `hide_line_prices`** (found during build 2026-08-18): the
  "total-only" override (estimate tri-state / tenant default) is applied
  by the PDF but NOT the email — an estimate set to hide per-line prices
  still emails them. The builder renders description+qty columns only
  when the effective flag is on.
- **Tier-aware estimate email**: when `proposal_mode`, do NOT dump lines.
  Render a compact tier summary (name + price per `ProposalTier`) and make
  the CTA ("View options & choose online") the star. Non-proposal estimates
  keep the line table (escaped, nl2br'd).
- **Real expiry**: compute/stamp `valid_until` (or the would-be value)
  *before* composing the body; print the actual date; delete "valid for
  30 days".
- Thousands separators on all totals; settlement rows already correct.
- If the deposit-ask branch lands its accept-page changes, add one sentence
  to the estimate email stating the deposit expectation (e.g. "A 50% deposit
  is requested on acceptance") — copy only, no new logic. Coordinate with
  [deposit-ask-online-pay-plan.md](deposit-ask-online-pay-plan.md).

### Phase 3 — the broken/naked surfaces

Priority order:

1. **Mobile payment receipt** (confirmed fake success): it discards the send
   result and returns hardcoded `{"sent": True}`, stamps nothing, and is
   SMTP-only. Route through `send_transactional_email` (gets Outlook Graph),
   return the real outcome, stamp a receipt-send record (WITHOUT overwriting
   the invoice's original `sent_at` — see Phase 5), rebuild body on the
   shared shell with remaining balance + company identity. First: check prod
   `email_settings` to confirm/deny that receipts have been black-holed, and
   tell Doug which customers were affected if so.
   **Prod check 2026-08-18: CONFIRMED — `email_settings` has 0 rows**, so
   the receipt path can never have delivered; current log window (since
   the v1.64.2 deploy) shows 0 attempts, so recent customer impact ~none.
2. **Reminder/dunning email**: wrap the tenant-template text in the shared
   shell and add `{pay_link}` to the template context + default template so
   the collections email can actually collect. (Dunning is OFF per Doug —
   still fix the body for manual reminder sends.)
3. **Planner digest**: absolute URL from the configured base; keep its
   escaping.
4. **Portal magic link + password reset**: onto the shared shell (password
   reset stays platform-branded but gets the same rendering fixes).
5. Mobile invoice fallback body (import-failure path): make it use the shell
   too, or delete the fallback — the import cannot realistically fail now.

### Phase 4a — automation emails for real, behind a toggle (locked: option, on/off)

Wire the workflow engine's `send_email` action
([modules/workflows/engine.py:41-45](../../gdx_dispatch/modules/workflows/engine.py))
to `send_transactional_email`, on the Phase 1 shell. Design points:

- **Global toggle `automation_emails_enabled`, default OFF.** This default is
  load-bearing: every action has been a no-op forever, so any `is_active`
  rule configured in the past with a `send_email` action would start
  emailing real customers the moment the deploy lands. OFF-by-default makes
  turning it on a deliberate act. Per-rule control already exists
  (`WorkflowRule.is_active`); the toggle is the tenant-wide kill switch on
  top.
- **Honest run records.** With the toggle off, the action result becomes
  `"skipped_disabled"` (today's `"logged"` reads like success). With it on,
  record `"sent"` / the `skip_reason` from `send_transactional_email`.
- **Designated sender.** Background sends pass `user_id=None` (same as the
  auto-reminder beat task), which skips the Outlook Graph path — on a tenant
  with no SMTP row (GDX), that means silent non-delivery even with the
  toggle ON. Add a tenant setting choosing which Outlook-connected user
  automations send as (e.g. the office account); fall back to SMTP; if
  neither, surface `no_email_provider_connected` in the run record.
- **Recipient/context.** Resolve the customer email from the trigger
  context's entity (job/invoice/estimate/customer); subject/body from the
  action params through the same `{{placeholder}}` context the rule
  conditions see, escaped. No recipient → recorded skip, not an error.
- Other action types (`send_sms`, `create_followup_task`, `emit_webhook`,
  `update_job_field`) stay log-only for now; the executor structure should
  make adding them mechanical later.

### Phase 4b — honesty cleanup (small, independent)

- Fix supplier_invite docstring or implement the send.
- Make admin_settings `/settings/email/test` actually send (or return 501).
- ~~Delete dead `core/email.py`.~~ **Not actionable — it is not dead** (see
  § Findings 6). Blocked on migrating the communications screens; tracked in
  email-overhaul-tech-debt.md rows 1 and 3.

### Phase 5 — delivery lifecycle hardening (from the section E audit)

In rough priority order; each item is small and independent:

1. **Persist every send ATTEMPT, not just successes** (locked: everything
   auditable). One `outbound_emails` table written by
   `send_transactional_email` itself so no caller can forget:
   - **initiator**: enum + ref — user click (user_id), bulk send, reminder
     task, workflow rule (rule_id), plugin (plugin key + delivery_id), n8n;
   - **content**: subject + rendered HTML as delivered (the exact bytes);
   - **recipient**: resolved address + how it was resolved (account email /
     contact id / override);
   - **outcome**: provider, sent/failed + skip_reason, attempt count,
     timestamps; bounce events (Phase 5.2) link back to this row and stamp
     `bounced_at` on it.
   Rows are append-only, never deleted. Phase 6's outbox references these
   rows for its lifecycle (queued → sent/failed), so a plugin send is
   traceable end to end. A simple "Outbound email log" screen (filter by
   customer / entity / initiator / outcome) makes it usable without SQL —
   this kills the in-memory communications dict and the "no answer to a
   customer dispute" problem in one move.
2. **Bounce-detection fixes**: give reminders/receipts a rung-1-matchable
   subject convention (or an `X-GDX-Entity` header / subject serial), and
   stop rung 2 from clearing `sent_at` on an invoice when the bounced
   message was a reminder, not the invoice itself.
3. **Receipt sends must not overwrite invoice delivery history** — separate
   `receipt_sent_at` (or the Phase 5.1 record) instead of re-stamping
   `sent_at` via `send_invoice`.
4. **Add `sent_via` to Estimate** + accept a channel on estimate mark-sent
   (mirror the invoice `MarkSentIn`), so out-of-band vs emailed is real data.
5. **Server-side double-send guard**: reject (or confirm-flag) a `/send`
   when `sent_at` is within the last N seconds; cheap idempotency.
6. **Wire `validate_email`** (currently dead code) into customer
   create/update and as a pre-send check with a clean skip_reason.
7. **Reply-To on SMTP sends** (tenant setting; Graph path already threads to
   the rep's mailbox).
8. **Encrypt `email_settings.password_enc` for real** (it's plain base64) —
   fold into the SOC2 track.
9. Subject/body serial fallback alignment (use the same fallback in
   subjects; keeps bounce rung 1 working).

### Phase 6 — plugins get full email access (Doug, 2026-08-18: "we need full access for plugins")

**Today plugins cannot send email at all.** Model B (ADR-013) runs plugins in
the plugin-host container, which by design has **no internet egress** — a
plugin importing `core.email_sender` or a Graph client fails at the network
layer, silently. Nothing in `plugin_api` offers email. Full data access,
zero email capability.

The architecture already tells us the right mechanism — plugins talk to core
through the **shared database**, so email follows the same path:

- **Email outbox table** in the shared DB. `plugin_api` grows a
  `send_email(...)` helper that inserts a row: recipient (raw address OR
  `customer_id`/`contact_id` for the Phase 2 resolver), subject, body,
  entity refs, optional attachments (same size cap). The core app — which
  has egress, Outlook tokens, and SMTP creds — drains the outbox through
  `send_transactional_email`. At-least-once with a delivery id, mirroring
  the event platform's semantics.
- **Full access means both modes**: `body_text` → rendered inside the
  branded shell (logo, line items context, CTA — same as every core email),
  or `body_html` → sent raw for plugins that want total control of the
  markup. No artificial capability ceiling.
- **Plugins inherit the whole pipeline free**: provider routing, the
  designated-sender identity (Phase 4a's setting), the Phase 5 sent-mail
  archive (plugin sends are logged like every other send), and bounce
  coverage.
- **Consent, not restriction**: add `"email"` to `KNOWN_PERMISSIONS`
  (ADR-014 pattern — browser/events/schedules/services already work this
  way) with an owner-consent risk line ("sends email to your customers as
  your company"). Declared once at install; no runtime gating after that.
  Explicitly: the Phase 4a `automation_emails_enabled` toggle governs the
  core workflow engine ONLY — it does not gate plugin sends.
- **Emit `email.*` plugin events** — `email.sent`, `email.send_failed`,
  `email.bounced` — into the existing event fan-out (plugin handlers + the
  n8n webhook path), so automations can react to delivery outcomes. This
  also gives n8n flows a first-class way to send branded email FROM the
  business (n8n → outbox via a plugin service or core endpoint) instead of
  bolting on their own SMTP node.

## Verification

- Unit: escaping, nl2br, tier summary rendering, expiry date presence,
  branding fallbacks (no logo / no color configured).
- The real gate: **send each template to Doug's own inbox** and view in
  Outlook desktop (light + dark), Gmail web, and the phone client —
  jsdom/pytest cannot prove email rendering, same lesson as the
  structural-assertion blind spot. Walk: estimate **via the composer** (the
  real path — flat + tiered), invoice via composer AND bulk-send, PAID
  receipt flavor, reminder, magic link, mobile receipt. Verify the CTA
  button is clickable in Outlook specifically.

## Open questions for Doug

1. Confirm priority: receipt non-delivery check first, then Phase 1 shell?
2. ~~Automations/workflow email actions — implement or strip?~~ **Locked
   2026-08-18: implement as an on/off option** (Phase 4a).
3. ~~Should the estimate send honor the tenant-editable template?~~
   **Locked 2026-08-18: yes** — wire it into `send_estimate` (Phase 2).
4. Any interest in a "reply-to" distinct from the sending Outlook user?
   (Not required — replies already thread to the sender via Graph.)
5. Phase 4a designated sender: which account should automation emails send
   as? (Needs an Outlook-connected user, or SMTP configured.)

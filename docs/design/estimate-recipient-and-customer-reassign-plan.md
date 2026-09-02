# Estimate: type a different email address, and change the customer

**Status:** **RELEASED v1.115.0** (2026-09-02) — A = #590, B + C = #592 (#591
was auto-closed when #590's base branch was deleted on merge; same commit,
rebased). Prod and demo both rolled to 1.115.0 and verified. Doug's "ask if
this is a new contact" shipped in #590. **F1-F10 below remain unbuilt** and are
filed separately. Owed: an interactive reassignment walk on prod — deliberately
not done, because it mutates a real customer's estimate.

Scoped to Doug's two asks plus the one safety consequence of the second. Every
line reference was verified against `origin/main` @ `1b028d7` (2026-09-01) —
printed from git, not read from the worktree, which was 29 commits behind.

**Date:** 2026-09-01 (rev 5 — trimmed. Revs 1-4 accreted seven more defects
found while verifying; they are real, and are now filed separately below rather
than bundled. Two earlier revisions also carried wrong claims — see § Why you
can trust the line numbers.)

**Trigger:** Doug — "Sometimes we need to manually add an email address to an
estimate and/or change who the customer is for the estimate. It needs to have
an audit trail."

---

## What already exists (do not rebuild)

- **The recipient override works server-side, end to end.** `SendEstimateIn.to_email`
  (`gdx_dispatch/routers/estimates.py` L1979) → `_prepare_estimate_email` L1766 →
  `override_recipient` (`gdx_dispatch/core/email_recipients.py` L119) → send L2091.
  Covered by pytest. Only the browser never sends the field.
- **Ask 1's audit trail already exists and needs no code.** Every send attempt
  writes an `outbound_emails` row — `to_email`, `recipient_source='override'`,
  the exact HTML, the outcome — from inside `send_transactional_email`, so no
  caller can forget. Readable at `/email-log`.
- **Reassignment works server-side.** `EstimatePatchIn.customer_id` L501,
  validated against live + undeleted customers L1081. No UI calls it.
- **The estimate page already has an activity panel** (`EstimateStatusContext.vue`,
  mounted `EstimateView.vue` L44), fed by `GET /api/estimates/{id}/activity`
  (L975). Shipped in #548, **RELEASED v1.113.0** — see
  `estimate-rejection-visibility-plan.md`. Ask 2's trail renders here.
- **The frontend already knows the customer's name** — `selectedCustomer`
  (`EstimateView.vue` L1875) resolves it from the customer list the page loads.
  The header's blank customer line is a binding bug, not a missing API.

---

## A — Type a different email address

**Defect.** `previewComposer` posts `to_email` (`EstimateView.vue` L3093).
`sendComposer` (L3104) does not (payload L3114-3119). Type an address → Preview
renders to it → Send → server gets `null` → `customer_has_no_email` (L2112) →
a red toast over the address the operator is looking at.

Second half: the free text box exists only as the `v-else` of "are there stored
recipients" (`Select` L996 / `InputText` L1010). If the customer has one wrong
address, there is no way to type a different one — the case that actually
happens (EST-000085 was one letter off).

**Fix.**
1. Add `to_email` to the `sendComposer` payload — one line, the same expression
   `previewComposer` already uses.
2. A **"Send to a different address"** link beside the recipient `Select` that
   swaps in the free input, seeded with the current pick, labelled *"one-time —
   the customer record is not changed"*, with a *Save to customer* link onto the
   existing audited contact-add endpoint (`gdx_dispatch/routers/customers.py`
   L2006). Also add it as a third button on the shipped bounce banner, which
   today offers only *Fix customer email* (rewrites the account address) and
   *Re-send* (same bad address) — new `@send-to-other` emit on
   `EstimateStatusContext.vue`.

**Known scope limit — do not ship it silently inert.** The override is consulted
only inside the `estimate.customer_id` branch; `estimate_has_no_customer` (L2116)
short-circuits first. An estimate with no customer at all cannot send to any
typed address. That state is reachable — `duplicate_estimate` starts the copy
with `customer_id=NULL` when the source customer was soft-deleted or merged
(L2926-2928). Either handle it or disable the affordance with a visible reason.

**Audit trail:** none needed. `outbound_emails` already records it.

**Size:** ~1 line + ~15 lines of UI.

---

## B — Change the customer, with a trail

**Defect.** The customer `Select` is disabled the moment the estimate exists
(`EstimateView.vue` L239). The server accepts the change but nothing calls it.

**And the generic PATCH cannot carry the trail.** `patch_estimate` audits with
`details={}` (L1112) — the row says *"user X ran patch_estimate"*, not which
field, not from whom to whom. It also takes no `request`, so `tenant_id=''`, no
IP, no request id. Worse, #548's whitelist comment is explicit — *"patch_estimate
/ line edits are 83% of an estimate's rows on prod and say nothing a person needs
here"* — so `patch_estimate` is **deliberately excluded** from `_ACTIVITY_LABELS`.
A reassignment routed through it would be invisible in the panel that just
shipped. It needs its own action name.

**Fix.**
1. `POST /api/estimates/{id}/reassign-customer` — `{customer_id, reason}`,
   reason required (same rule as decline). Takes `request: Request`. Audits
   **before** the commit with `audit_or_rollback` (`gdx_dispatch/core/audit.py`
   L528) — an untraced reassignment is worse than a failed one — as
   `estimate_customer_reassigned`, details `{from_customer_id, from_customer_name,
   to_customer_id, to_customer_name, reason, previous_status, token_rotated}`.
   Modeled on `update_mobile_job_customer` (`gdx_dispatch/routers/mobile.py` L3932),
   the repo's existing "correct who this record belongs to" endpoint.
2. Remove `customer_id` from `EstimatePatchIn` so there is one door. **Verified
   safe:** the autosave PATCH (L2759) omits it, L2921 is inside a POST, no test
   PATCHes it, and none of the five estimate MCP tools PATCH at all.
3. **Register the action in three places** — miss any and the row is mute:
   `_ACTIVITY_LABELS` (L907, the panel's label),
   `gdx_dispatch/frontend/src/constants/activityLabels.js` (dashboard),
   and `detailLine()` (`EstimateStatusContext.vue` L128-135, a per-action
   whitelist — without a case the "Acme → Baker" text never renders).
4. Frontend: a **"Change customer"** action in the header beside the customer
   name — searchable picker, required reason, consequences stated. Bind the
   header's blank name to `selectedCustomer?.name` while you are there (no
   backend change).
5. Block when `job_id IS NOT NULL` or status is `accepted`/`declined` — a
   converted estimate's customer is settled by the job. 409 naming the job.

**Size:** ~40 lines backend, ~50 lines frontend, 3 one-line registrations.

---

## C — Rotate the public link when the estimate was already sent

Not a separate feature. Without it, B leaks: reassign an estimate that was
already emailed and the old customer's link keeps working, now rendering the
**new** customer's name, jobsite address and pricing.

**Key on `sent_at`, never on `status`.** `_get_public_estimate_or_404`
(`gdx_dispatch/modules/proposals/router.py` L160-164) filters `deleted_at IS NULL`
**and `sent_at IS NOT NULL`**; there is no status term, and `GET /proposals/{token}`
(L403) adds none. A status-keyed rule looks safe and is not: `reopen_estimate`
(L2829-2833) sets `status="draft"` and **never touches `sent_at`**, and it accepts
`rejected` (L2822) — the bounced state this whole feature serves. So
bounce → reopen → reassign would leave the link live under a rule that says
"it's a draft, nothing to rotate."

Bounded honestly: the old recipient can read it but cannot accept it — the public
accept gates on `sent`/`rejected` (L456). Disclosure, not fraud.

**Fix.** When `sent_at IS NOT NULL`, mint a fresh `public_token`; record
`token_rotated`. Say it in the dialog: *"the link already emailed to {old
customer} will stop working"* — so the operator knows to re-send.

**Do it as one shared helper.** Five sites already mint an `Estimate.public_token`
(`routers/estimates.py` L801 and L2960, `routers/mobile_quoting.py` L449,
`modules/proposals/service.py` L75, `core/mcp_tools/estimates_create_draft.py`
L113) and there is no `rotate_public_token` today.

**Nothing else breaks.** Swept every `public_token` / `/proposals/` reader: the
URL is computed at send (`_public_proposal_url` L1476) and persisted only inside
`outbound_emails.html_body`, which rotation is meant to invalidate. The re-send
detector matches the literal `"/proposals/"`, never the token
(`gdx_dispatch/modules/outlook/resend_detect.py` L94, L115). `estimate_nurture.py`
touches neither.

**Size:** ~5 lines + the helper.

---

## Packaging

Two PRs, merged in order. **PR 1 = A** (frontend only, no server change, ships
alone). **PR 2 = B + C** (C is a safety requirement of B and must not ship
separately).

## Decisions

1. **"Change customer" lives in the estimate detail header.** *(Doug, 2026-09-01)*
2. **Rotate whenever `sent_at IS NOT NULL`, regardless of status.** Doug approved
   "rotate when a customer could already be holding the link" against a
   *status*-keyed description that was wrong; the intent is unchanged, the
   predicate is corrected. **Re-confirm.**
3. **A typed address is offered for saving, never saved automatically.** *(Doug)*
4. **Reason required on reassignment.** *(Doug)*

## Found while verifying — filed separately, NOT bundled

Real, evidenced, and none of them Doug's ask. One issue each:

| # | Finding |
|---|---|
| F1 | `InvoiceDetailView.vue` has A's exact bug — preview sends `to_email` (L1837), send does not (L1858). Server already honors it (`gdx_dispatch/routers/invoices.py` L2342, L2601). |
| F2 | `estimates.write` / `estimates.send` are defined (`gdx_dispatch/core/permissions.py` L49-50) and **enforced nowhere**; the router has only `require_module` + `get_current_user` (L41). Any technician can reassign or send. `.authz_ungated_baseline` cannot fail for this — it tracks only routes with no auth at all. |
| F3 | Reassignment emits no domain event, though `estimate.sent` (L2133) and accept/decline (L269-281) both ship `customer_id` to n8n consumers. |
| F4 | Lines added *after* a reassignment price against the new customer (`_resolve_customer_for_engine` L300-315) while existing lines stay frozen. One estimate, two pricing bases. |
| F5 | Sweep the shape this feature creates: *a customer change that leaves cross-entity provenance pointing at the other customer.* The repo already has this post-mortem, mirrored, at `InvoiceCreateView.vue` L629-632. Surface: every table holding both an `estimate_id`/`job_id` and a `customer_id`. |
| F6 | Four more `details={}` audit blocks in `estimates.py` (L1266, L1413, L1458, L3144). |
| F7 | `GET /api/estimates/{id}` still omits `customer_name` (`_serialize_estimate` L171) though the list endpoint enriches it (L688-753). Worked around here client-side; the API gap stands. |
| F8 | The customer picker loads `/api/customers?per_page=500` — above 500 customers, the one you want may not be in the list. |
| F9 | **Two BULK paths move estimates between customers with none of C's protections.** `POST /api/customers/merge` (`gdx_dispatch/routers/customers.py` L1691) and `POST /{parent_id}/absorb-subcustomers` (L1345) both rewrite every `customer_id` column found by `_discover_customer_fk_tables` (L1610-1618, "any column named customer_id"), which includes `estimates.customer_id`. Neither rotates `public_token`, takes a reason, or writes an action the estimate activity panel renders. C's security argument applies verbatim, N rows at a time. Absorb has run on prod. Found by the code-diff audit — this is Pattern C, which rev 5 declared and did not execute. |
| F10 | **A rotated token leaves no "needs re-send" state.** `status` and `sent_at` are untouched, so the list still reads "Sent" while the link is dead; the only notice is a toast. Nothing surfaces it a day later. |

## Why you can trust the line numbers

Two earlier revisions of this doc shipped wrong claims, both from trusting a
summary instead of the code: rev 1 called the rejection-visibility work unbuilt
(it is RELEASED v1.113.0), and rev 3 cited `deposits/service.py` L214 as a
"requires accepted" gate — that line is `if estimate.customer_id is None`. So
every `L<n>` here was extracted mechanically and printed from `origin/main`.

The one claim that mattered and survived re-derivation: **money cannot reach
this feature.** `create_deposit_invoice` has no status gate, but all five of its
callers do (`routers/estimates.py` L2545/L2573/L2672,
`modules/proposals/router.py` L594, `routers/portal.py` L1351,
`routers/mobile_quoting.py` L697). An estimate with a deposit is `accepted`;
`reopen_estimate` refuses `accepted`; and the bounce detector only flips rows
already `sent`. B blocks on `accepted` anyway.

## What the code-diff audit caught (fixed in the branches)

- **`audit_or_rollback` was not yet atomic here.** `ensure_audit_table` COMMITS
  on first use per engine and runs lazily from inside the audit call, so the
  `customer_id` write and the rotated token were hardened *before* the audit row
  existed — leaving nothing to roll back, which is the endpoint's whole promise.
  Hoisted to the top of the handler, matching `create_customer_contact`
  (`gdx_dispatch/routers/customers.py`). NOT `Depends(audit_ready_db)`: that
  resolves its own session and bypasses the suite's `get_db` override.
- **The permission gate could not fail.** The shared fixture authenticates as
  `admin`, and `require_permission`'s admin escape hatch meant every test would
  have passed with the dependency deleted. Added a test using a role without
  `estimates.write`; verified it returns 200 (red) with the gate removed.
- **The dialog lied about a blank jobsite.** A NULL `jobsite_address` means
  "the customer's address", so reassigning an estimate with no explicit jobsite
  silently moves the work to the NEW customer's address. The dialog said "the
  jobsite address stays exactly as typed"; it now says what actually happens.

Known limit, stated rather than papered over: A's counterfactual proves the
shared payload builder's tests go red when the builder drops `to_email`. The
original defect was an omission at the *call site*, and EstimateView has no
mount harness (it needs the pricing engine, catalogs and a route). The
structural mitigation is that both payloads are now built by one function
rather than two hand-written literals — not a test that would catch someone
re-inlining them.

## Verification owed

- Branch off current `main`.
- **The reopen walk** — bounce → `rejected` → reopen → reassign → confirm the old
  `/proposals/{token}` no longer renders. The case a status-keyed rule ships broken.
- Browser, light + dark, desktop + mobile: type an address on a customer with no
  email → sends; type a different address on a customer that has one → sends
  there and the customer record is unchanged; duplicated estimate with a NULL
  customer → honest, not inert.
- The reassignment shows in the shipped activity panel with a label, a named
  actor and the "Acme → Baker" line — proves all three registrations.
- **After deploy, hit `GET /api/estimates`.** `gdx_dispatch/app.py` L72-75 wraps
  the estimates import in `try/except` and falls back to an **empty** router:
  a bad import boots a healthy app where every estimate endpoint 404s, recorded
  only in a log line. PR 2 adds two imports to that file.
- Full pytest matrix + vitest + lint ratchet + `test_doc_link_scan.py`.

## Constraints

- **Doc-link ratchet (#588):** measured on this revision — **35 backticked path occurrences, 26 distinct, 0 dead.** Re-measure *after* the last edit; this doc
  has twice carried a stale count forward.
- **Status line ships with the code (#589):** a PR implementing part of this
  updates line 3 in the same PR.

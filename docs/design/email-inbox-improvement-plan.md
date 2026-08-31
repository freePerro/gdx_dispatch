# Email Inbox — Improvement & Feature Plan

**Status:** **PARTIALLY BUILT** — Phases 0, 1 and 2 shipped (see the Build
status table below). **D6 DECIDED 2026-08-31 — removed** (was: still open, re-verified
2026-08-21: the auto-email automations — `dispatch_trigger` — had zero
production callers anywhere on main and needed a revive-or-delete
call (open decision 6).

## Build status (2026-07-27)

| Phase | State |
|---|---|
| **Phase 0** (0.1–0.5 + D7) | **SHIPPED** v1.19.0; delta-blank sync bug fixed v1.21.0 |
| **Phase 1** (1.1–1.5) | **BUILT** this branch — 1.2 shipped with D7 |
| **Phase 2** (P2.1–P2.6) | **BUILT** this branch |
| **D6 auto-email automations** | still dead — `dispatch_trigger` has no production caller. Needs Doug's revive-or-delete call (open decision 6). | **[DECIDED 2026-08-31: removed — `dispatch_trigger`, its tests and the Outlook Auto-Email templates tab are deleted; Event Rules (`modules/workflows`) is the one event-email path. See unimplemented-endpoints-decision-list.md § 2026-08-31.]**

Deviations from the plan as written, and why:

- **1.4 forward** goes through Graph's native `POST /me/messages/{id}/forward`
  instead of a re-composed send. The plan's MVP was text-only with attachments
  as a follow-up; the native action carries the original attachments for free
  and avoids downloading + re-uploading every blob through our worker. It is
  **owner-only** — that endpoint resolves the id against the caller's own
  mailbox and would send under the owner's name (consistent with open
  decision 7).
- **1.3 threading** is a server-resolved conversation strip in the detail pane
  (`GET /messages/{id}/thread`, every sibling re-checked through
  `filter_visible`) rather than client-side grouping of the loaded page. A
  grouped list would have counted "3 messages" meaning "3 of the ones that
  happen to be on screen".
- **1.1 search** is local-column only (subject / sender / preview) and the UI
  says so. Bodies are never persisted (0.1 live-fetches them), so a local body
  search would silently miss text that is plainly in the email. Graph
  `$search` remains the follow-up.
- **P2.4 AI draft** is its own endpoint under the email module gate rather
  than a call into `/api/ai/communication/draft` — that route requires the
  *communications* module and a `customer_id`, and most inbound mail is not
  customer-linked yet.
- **P2.3 save-to-job** writes a `Document` row (the surface the job page
  already reads), not a new `OutlookAttachment`+R2 path. Idempotent on content
  hash.
- **P2.2** ships create-task + attach-to-job/customer. "Create estimate from
  email" is NOT built — an estimate needs a customer *and* line items, so it
  is a builder-prefill feature, not an inbox button.
- **Latent bug fixed in passing:** the reply path threaded through
  `/me/messages/{parent_graph_id}/reply` using the VIEWER's token; on a shared
  mailbox a non-owner's reply 404'd at Graph and 502'd the whole send. It now
  falls back to a plain `sendMail` (loses RFC threading, delivers the reply).

### Audit round 3 (2026-07-27) — findings folded in

Three adversarial reviews (security / correctness / UX) ran against the first
cut. What they caught, and what changed:

1. **Search cursor was a content oracle.** `?q=` filtered raw rows but
   `next_offset` counted them *including hidden ones*, so `?q=<guess>&limit=1`
   confirmed words inside mail the viewer may not open. Search now filters
   first and paginates the VISIBLE list; every number in the response derives
   only from readable rows.
2. **`save-to-job` had no job authorization.** Seeing one email let anyone
   write a file onto any job in the tenant. Now office roles, or
   `job_belongs_to_user` for a tech.
3. **`save-to-job` published to the customer portal.** `portal_documents`
   lists by `customer_id`, so the filename + email subject would have reached
   the customer. `customer_id` is now left NULL — the user asked for a *job*.
4. **`save-to-job` poisoned vendor-invoice dedup.** `Document.content_hash` is
   the vendor pipelines' tenant-wide key (`find_existing_document` is unscoped
   and hard-raises), so filing an emailed statement onto a job would have
   blocked importing that statement forever. Idempotency now keys on
   (job, name, size); no hash is written.
5. **`save-to-job` orphaned files.** A >255-char attachment name wrote bytes,
   then failed the INSERT — an orphan per retry. Names are truncated first and
   a failed commit unlinks the file.
6. **Drafts returned all-null ids.** `graph_client._request` returns an
   `httpx.Response`, not JSON. The mocked test couldn't see it.
7. **Personal messages leaked sideways.** `create-task` copied a
   personal message's subject into a tenant-readable PlannerTask, and
   `ai-draft` shipped it to the AI provider. Both now refuse with a 409.
8. **The unread badge counted Junk and Deleted Items**, and a failed poll
   zeroed the count so the recovery poll announced week-old mail as new.
9. **Dead buttons.** Forward is owner-only server-side and Link is
   office-roles-only; both now render only for who can use them.
10. **The job marker showed a raw UUID to customers** and only stamped on some
    paths. It now prefers the printed job number (the tagger resolves both
    forms) and stamps on new mail only, never replies — deterministic
    regardless of who sends.
11. **Job search ignored `job_number`** — the one identifier every screen
    prints. Added to `routers/jobs.py`.
12. **"Saved to the job" showed nothing on the job.** The Photos tab filters on
    an `entity_type` field `DocumentOut` doesn't carry; the job's Email tab now
    lists files attached to the job.

### Audit round 4 (2026-07-27) — the one that mattered most

A fourth reviewer, given only the diff, found that **the round-3 security fix
had deleted the search itself**. Extracting the visibility-first `_search_page`
helper moved the branch above `query.filter(_search_predicate(search))`, and
nothing re-applied it: `?q=anything` returned the newest 500 messages,
unfiltered. The UI looked right, which is why nobody noticed.

The test that was supposed to prove search worked asserted
`tdb.query.return_value.filter.called` — true on *every* request, because
`_load_tech_emails` filters a User query on the same shared mock. It passed
with the feature gone. It now asserts the predicate is built from the term and
that the exact predicate object reaches `.filter()`.

Also fixed in that round:

- `create_draft` wrapped the whole send in a bare `except` that returned
  `ok=True` — a connection error told the user their draft was safely in
  Outlook. Only the response *parse* degrades now.
- The marker's idempotency check scanned for a bare token, so a job numbered
  "14" was treated as already-marked by any subject containing "14". It looks
  for the bracketed marker.
- The `job_number` lookup used `.first()` on a non-unique, tenant-editable
  column — newest-wins ordering makes it deterministic.

**Accepted, not fixed:** the marker stamps on new mail only, never on replies.
Graph derives a reply's subject from its parent, and stamping only on the
non-owner fallback path would make the customer-visible subject depend on who
clicked Send. Replies to a customer we already know are linked by address
matching anyway; the marker exists for the case where we don't know them.

**Status: DRAFT v2 (audit round 1 folded in) — 2026-07-10**
**Scope: the Outlook / Microsoft Graph inbox (`modules/outlook/`, `InboxView.vue`, `MobileInboxView.vue`)**

> **Audit round 1 verdict (2026-07-10):** the diagnosis (D1–D7) is accurate — every
> "dead/broken" claim was verified against source; nothing flagged dead is secretly
> wired. The flaw was in a **fix**: the headline P0 live-fetch (0.1 body / 0.3
> attachments) was architected as if one user owns the mailbox. It is a **shared**
> mailbox — `visibility.py` deliberately shows one connected account's mail to *other*
> users (techs, second office staff). Graph auth is per-connecting-user
> (`with_outlook_client` keys off `user_id`, `token_refresh.py:164-167`), so a live-fetch
> keyed to the *viewer* returns "reconnect / preview forever" for everyone who isn't the
> account owner — and verifying only on Doug's own connected account is the exact thing
> that hides it. **Fix folded in below: all live-fetch resolves the OWNER via
> `msg.account_id → OutlookAccount.user_id` (the map `visibility.py` already builds), and
> P0 acceptance now requires testing as a non-owner.** Two mechanism corrections from the
> audit are also folded into D5 and 0.5.

> Verdict from the code read: the **sync plumbing is strong** (per-folder delta with
> 410-recovery, webhook + fallback poller, self-healing subscriptions, tenant-plane
> isolation, server-side visibility on every read). The **last mile is thin** — the
> parts a user touches are missing or broken, and three substantial features are fully
> written but never wired into the running system. This plan fixes what's broken first,
> then adds the features that make the inbox worth opening for a garage-door
> field-service shop.

## Problem

Today the "inbox" can list message headers and send a new/reply mail from desktop, but:
you cannot read a full email (only the ~255-char preview), you cannot open an
attachment, mobile compose/reply is silently rejected, and the customer/job email tabs
are always empty because the tagging engine that would populate them is never called.
The good bones are being wasted on a read-only preview pane.

## What already exists (reuse, don't rebuild)

| Piece | Where | State |
|---|---|---|
| Per-folder delta sync + 410 full-resync | `modules/outlook/tasks.py:245` `_sync_one_folder` | **Live** |
| Webhook receiver → enqueue sync | `modules/outlook/webhook_router.py:60` | **Live** |
| Fallback poller (every 30 min) + subscription self-heal (every 6h) | `tasks.py:622`, `tasks.py:537`; `core/scheduler.py:45-55` | **Live** |
| Token refresh + 401 retry context mgr | `modules/outlook/token_refresh.py` `with_outlook_client` | **Live** |
| Graph client: get_message (**full body**), list/download attachment, move/copy, mark-read, folder CRUD | `modules/outlook/graph_client.py:189, 368, 372` | **Live but partly unused** |
| Send / reply (cc, bcc, attachments supported) | `modules/outlook/send_router.py:131` `POST /api/outlook/send` | **Live (desktop only)** |
| Folder rail: tree, color, pin, rename, create, delete, empty, mark-all-read | `folders_router.py`, `InboxView.vue` | **Live** |
| Read views + **by-customer / by-job** email endpoints | `views_router.py:146, 175, 199` | **Live, but by-customer/by-job return nothing — see D3** |
| Tagging engine (auto_match email→customer, job_thread subject regex, AI stub) | `modules/outlook/tagger.py` `tag_message` / `manual_tag` | **Dead code — never called outside tests (D3)** |
| Auto-email triggers (invoice.created / job.completed / estimate.sent) | `modules/outlook/automations.py` `dispatch_trigger` | **Dead code — no production caller (D6)** | **[DECIDED 2026-08-31: removed — `dispatch_trigger`, its tests and the Outlook Auto-Email templates tab are deleted; Event Rules (`modules/workflows`) is the one event-email path. See unimplemented-endpoints-decision-list.md § 2026-08-31.]**
| Initial backfill task + `backfill_days` setting | `tasks.py:417`; `OutlookSettings.backfill_days` | **Dead code — no caller (D5)** |
| Visibility chokepoint | `modules/outlook/visibility.py` `filter_visible` / `can_view` | **Live** |
| MCP email tools (list/read/draft/move) for the AI agent | `core/mcp_tools/email_*.py` | Live; `email.draft` makes a **local-only** draft (`graph_message_id="local-draft-…"`, not pushed to Graph) |
| Planner "needs-action" task surface (mobile bottom nav) | `PlannerTask`, `routers/planner.py` | Live — reuse target for "email → action" (see D-P2) |
| Transactional email (Graph-first, SMTP fallback) | `core/transactional_email.py` | Live |

## What's broken or dead (verified against source — these motivate P0)

- **D1 — You cannot read a full email.** The detail endpoint returns only stored
  columns, and the full body is never persisted (the R2 write is a documented no-op,
  `tasks.py:150-157`). Desktop renders `<pre>{{ detail.body_preview }}</pre>`
  (`InboxView.vue:664`); mobile's `detailBodyHtml` prefers `body_html || body ||
  body_preview` (`MobileInboxView.vue:150-160`) but the API returns neither of the
  first two, so both always show the escaped ~255-char preview. `graph_client.get_message()`
  (`graph_client.py:189`) already fetches `body.content` — the detail endpoint just
  never calls it.
- **D2 — Mobile compose & reply always 422.** `MobileInboxView` posts `{ to: "<string>",
  body, in_reply_to }` (`MobileInboxView.vue:240, 278`), but `SendMailIn`
  (`send_router.py:44-55`) is `extra="forbid"`, requires `to: list[EmailStr]`, and
  requires `body_html` (not `body`). Every mobile send/reply is rejected. Desktop is
  correct.
- **D3 — Auto-tagging never runs → customer/job email tabs are always empty.**
  `tag_message()` / `manual_tag()` (`tagger.py:172, 217`) are called only from tests.
  `_persist_messages` never tags. So `linked_customer_id` / `linked_job_id` are never
  set, and `GET /messages/by-customer/{id}` and `.../by-job/{id}` (`views_router.py:175,
  199`) return nothing. The single most valuable feature is built and disconnected.
- **D4 — Attachments can't be opened.** `_persist_messages` sets `has_attachments` but
  never creates `OutlookAttachment` rows, and no router exposes the client's
  `list_attachments`/`download_attachment`. The UI shows "📎 Has attachments" as inert
  text.
- **D5 — Connecting a mailbox pulls no history on connect.** The OAuth callback creates
  a subscription (`outlook_oauth.py:379`) but enqueues **no** initial sync or backfill.
  And the fallback poller **explicitly skips accounts with a healthy subscription**
  (`tasks.py:648-650` `if healthy: continue`) — which a freshly connected account has,
  because the callback just created one. So the mailbox stays empty until the *first
  webhook* fires (i.e. until new mail arrives); it is **not** "eventually filled by the
  poller" (audit round 1 correction). When a sync finally does run it's an *unbounded*
  delta walk — the initial delta pages the whole folder and `MAX_MESSAGES_PER_RUN=500`
  is defined-but-never-applied (`tasks.py:44`), so `OutlookSettings.backfill_days`
  (default 90) is never honored either. `backfill_outlook_mailbox` — the task written to
  do exactly this, date-bounded — has zero callers. (Poller cadence: the scheduler runs
  it every 30 min via `crontab(minute="*/30")`; the `tasks.py` docstring says 15 min and
  is stale.)
- **D6 — Auto-email automations are dead too.** `dispatch_trigger` (`automations.py`) **[DECIDED 2026-08-31: removed — `dispatch_trigger`, its tests and the Outlook Auto-Email templates tab are deleted; Event Rules (`modules/workflows`) is the one event-email path. See unimplemented-endpoints-decision-list.md § 2026-08-31.]**
  has no production caller — invoice/job/estimate emails are never sent. *(Outbound
  side; adjacent to the inbox. Flagged for awareness; not on the P0 critical path.)*
- **D7 — Pagination + visibility ordering bug.** `list_messages` applies
  `offset/limit` in SQL and *then* `filter_visible` in Python (`views_router.py:161-172`).
  A page can silently return fewer than `limit` rows, and there's no "page 2" — the UI
  hardcodes `limit=200` (desktop) / `100` (mobile) with no load-more. On a busy mailbox
  this drops mail off the bottom with no way to reach it.

## Design

Three phases. **P0 makes it a working inbox** (fix the four things a user hits in the
first 30 seconds + the two sync-correctness gaps). **P1 makes it feel like a mail
client.** **P2 makes it worth having inside GDX specifically.**

### Phase 0 — Make it actually work

**0.1 Render the full body (D1).**
Live-fetch on detail open rather than finishing the deferred R2 pipeline (smaller, no
migration, always fresh):
- `GET /api/outlook/messages/{id}` gains an opt-in `?with_body=1` (or a sibling
  `/messages/{id}/body`) that, after `can_view`, opens `with_outlook_client` **for the
  mailbox owner** and calls `gc.get_message(row.graph_message_id)`, returning `body_html`
  + `body_content_type`.
- **Owner resolution (audit round 1 — load-bearing):** this is a **shared** mailbox.
  `with_outlook_client(control_db, tenant_db, user_id, tenant_id)` keys tokens off
  `user_id` (`token_refresh.py:164-167`) and raises `OutlookReconnectRequired` if that
  user has none. The viewer is frequently **not** the account owner (the whole point of
  `visibility.py`). So resolve the owner from the message:
  `msg.account_id → OutlookAccount.user_id` (the same `account_owner` map
  `visibility.py` already builds), and pass the **owner's** `user_id` — never the
  viewer's. Keying to the viewer would give every tech/second-office-user "reconnect,"
  never a body. `views_router` does not currently import `with_outlook_client`; add it.
  *(Note: the existing write actions — mark-read, move — use `_account_for_user(uid)` and
  are therefore already owner-only; a non-owner can read a shared message today but
  can't mark/move it. See Open decision 7 for whether P0 also normalizes those.)*
- **Security gotcha (must-do):** Outlook HTML carries scripts, remote tracking pixels,
  and `on*` handlers. Do **not** drop it into `v-html` raw. Sanitize server-side
  (bleach) or render in a `sandboxed` iframe with remote images off by default + a
  "show images" toggle. Desktop's current `<pre>` is ugly but *safe*; switching to HTML
  without sanitizing trades a UX bug for an XSS/tracking hole.
- Fallback: if the message is gone from Graph (moved/deleted) or the account needs
  reconnect, fall back to `body_preview` with a note. Keep the preview column as the
  offline/list snippet.
- *Deferred alternative (not now):* finish the R2 body persistence the schema already
  anticipates (`body_r2_key`) — needed only if we later want offline read or full-text
  local search (see 1.1).

**0.2 Fix mobile send/reply (D2).**
`MobileInboxView.vue`: send `to: [addr]` (array), `body_html` (rename from `body`), and
send `cc` as an **array too** (mobile currently sends it as a bare string, which also
trips `extra="forbid"` — audit round 1 catch), omitting it when empty. Drop any stray
field so the payload passes `extra="forbid"`. Reuse desktop's `splitAddrs` + the
`\n`→`<br>` shim. Add a vitest that asserts the outgoing payload shape matches
`SendMailIn` exactly. ~20-line fix; unblocks all mobile sending.

**0.3 Attachments: list + download (D4).**
- Two endpoints under `/api/outlook`, both behind `require_module("email")` +
  `can_view`:
  - `GET /messages/{id}/attachments` → lazy `gc.list_attachments(graph_message_id)`
    (id, name, contentType, size, isInline). *Lazy on open, not during bulk sync* — so
    we don't fire an extra Graph call per message on every poll.
  - `GET /messages/{id}/attachments/{aid}` → `StreamingResponse` of
    `gc.download_attachment(...)` with the right `Content-Type` +
    `Content-Disposition`. Cap size; stream, don't buffer whole in memory for big files.
  - **Both open `with_outlook_client` for the OWNER**, resolved via `msg.account_id`
    exactly as 0.1 (audit round 1) — not the viewer. Otherwise attachment download 409s
    "reconnect" for every non-owner viewer.
- UI: render an attachment chip list in both detail panes with a download action.
  (Inline images resolve later, tied to 0.1's iframe.)
- *Persisting attachment rows to `OutlookAttachment` + R2 is deferred* — the live path
  covers "open my attachment" without a storage bill.

**0.4 Wire the tagging engine (D3).**
- Call `tag_message(row, tenant_db, control_db)` for each **newly inserted** row inside
  `_persist_messages` (guard on `if existing is None`; the function is already
  idempotent and self-skips tagged rows). The sync task already holds both a tenant and
  a control session (`tasks.py:370-371`), so `control_db` is available for the (still
  stub) AI strategy — auto_match + job_thread cost one indexed `email_hash` lookup and a
  regex, negligible.
- One-time **re-tag backfill**: a management task that walks existing untagged
  `outlook_messages` and runs `tag_message` — otherwise the customer/job tabs stay empty
  for all *already-synced* mail.
- Manual override endpoints (needed for the P2 timeline and to correct auto-tags):
  `POST /api/outlook/messages/{id}/link { customer_id?, job_id? }` → `manual_tag`, and
  `DELETE .../link` → clear. Admin/office gated per the visibility model.

**0.5 Sync on connect + honor backfill_days (D5).**
In the OAuth callback, after `create_subscription`, **resolve the `OutlookAccount` row
the callback just created/updated** (the callback scope has `user_id`/`tenant_id`, not
`account_id` — audit round 1 catch) and enqueue `backfill_outlook_mailbox.delay(
str(account.id), str(tenant_id), days=settings.backfill_days)`. This is exactly what the
(currently dead) task was written for: a date-bounded initial pull that then primes the
delta tokens so the webhook takes over. This is **required, not just nicer** — the
fallback poller skips healthy-subscription accounts (D5), so nothing else backfills a
fresh connect. Fixes "empty on connect" *and* the unbounded first delta walk. If we
decide backfill is redundant, the honest alternative is to delete the task + the
`backfill_days` setting — but don't leave a setting that does nothing.

### Phase 1 — Make it feel like a mail client

**1.1 Search.** `GET /api/outlook/messages?q=…` — MVP: SQL `ILIKE` over `subject`,
`from_address`, `body_preview`, still routed through `filter_visible`. Add a search box
to both views. *Graph `$search` (covers full body server-side) is the follow-up* once we
know local preview search isn't enough; it costs a Graph round-trip per query and still
needs `filter_visible`, so start local.

**1.2 Pagination / load-more (fixes D7).** Add cursor or offset-based "load more" to both
lists (drop the hardcoded 200/100). Correct the visibility-after-limit gap: fetch a
larger candidate window then paginate post-filter, or push the cheap visibility
predicates (owner/tagged) into SQL. Document that per-page counts are approximate under
the Python visibility filter.

**1.3 Conversation / threading view.** `conversation_id` is already stored + indexed.
Add a grouped "conversation" mode: collapse a thread to its latest message + a count,
expand to the ordered chain. MVP is list-grouping; no schema change.

**1.4 Reply-all + forward.** Backend `send` already supports cc/bcc/attachments.
- Reply-all: pre-fill `to` = original from + original to/cc, minus the mailbox owner.
- Forward: subject `Fwd:`, quoted body, empty recipients. *Forwarding attachments needs
  re-uploading the original bytes* — MVP forwards text only and says so; full
  attachment-forward is a follow-up (fetch via `download_attachment` → re-attach).

**1.5 Drafts.** Compose becomes save-able: push a real Graph draft
(`POST /me/messages` / `createDraft`) instead of the MCP tool's local-only stub, so
drafts round-trip with Outlook and appear in the (already-shown) Drafts folder. Ties off
the dead-end where the Drafts folder renders but nothing can create one.

### Phase 2 — Make it worth having inside GDX (the business value)

**P2.1 Customer / Job email timeline.** With 0.4 done, add an **Email tab** on customer
detail and job detail that calls the existing `by-customer` / `by-job` endpoints, plus a
badge on inbox rows showing the linked customer/job. "This email ↔ this job" is the
whole reason to read mail inside GDX instead of Outlook.

**P2.2 Email → action (highest-leverage reuse).** From a message: **Create task /
lead**, **Attach to job**, **Create estimate**. Route "create task" into the *same*
`PlannerTask` needs-action surface the call-capture feature already feeds (v1.11.0) — an
inbound customer email becomes a follow-up that resurfaces in the morning digest, with
zero new reminder plumbing. This is the cross-sell between the phone-capture and email
worlds.

**P2.3 Save attachment → job attachment.** One click from an email attachment to a
`JobAttachment` (POs, signed contracts, site photos land on the job record). Builds on
0.3 + the existing job-attachment model.

**P2.4 AI-drafted replies / templates.** Surface the existing `/draft` + canned
templates (`routers/ai_communication.py`) as "Draft reply with AI" in the compose pane.

**P2.5 Send-from-job with a `[Job #<uuid>]` subject marker** so the `job_thread` tagger
(`tagger.py:91`) auto-links the reply back to the job — closes the tagging loop instead
of relying only on email-address matching.

**P2.6 Nav unread badge + new-mail toast.** The webhook already gives near-real-time
signal; surface an unread count on the sidebar Inbox item and a toast on arrival.

## PR slicing

1. **PR 1 — Working inbox (P0):** 0.1 body (+ sanitize) · 0.2 mobile send fix · 0.3
   attachment list/download · 0.4 wire tagging + manual link + re-tag backfill · 0.5
   connect-time backfill. Ship as one or split 0.1–0.3 (user-facing) from 0.4–0.5
   (sync-side). **Done = read a real HTML email with a downloadable attachment on
   desktop *and* phone, and a synced email shows on its customer's Email tab.**
2. **PR 2 — Mail-client UX (P1):** search · pagination/load-more · threading ·
   reply-all/forward · drafts. Independent, sliceable.
3. **PR 3 — GDX value (P2):** customer/job Email tab · email→PlannerTask · save
   attachment to job · AI draft/templates · job-thread send marker · nav badge.
4. **PR 4 — (separate track) revive or remove D6 auto-email automations** — decide with **[DECIDED 2026-08-31: removed — `dispatch_trigger`, its tests and the Outlook Auto-Email templates tab are deleted; Event Rules (`modules/workflows`) is the one event-email path. See unimplemented-endpoints-decision-list.md § 2026-08-31.]**
   Doug; not inbox-blocking.

Each PR: browser-verified light + dark, mobile viewport for the mobile paths, per the
usual manifest discipline. P0 verification is on **real prod mail** in Doug's connected
account (the preview-vs-full-body and attachment behavior only show up with real
messages) — **AND, mandatory (audit round 1), verified as a second, non-owner user**
(a tech or second office login who has NOT connected their own Outlook) opening a message
they're allowed to see. Testing only on the owner's account is the specific thing that
would let the owner-token bug ship green. Acceptance: that non-owner sees the full body
and can download an attachment.

## Open decisions (defaults chosen — flag if wrong)

1. **Body: live-fetch vs persist to R2.** Default = **live-fetch on open** (no migration,
   always fresh). Persist to R2 only if/when offline read or local full-text search is
   wanted. *Trade-off: live-fetch adds a Graph round-trip per open and can't show a body
   for a message deleted from Graph.*
2. **HTML rendering: sanitize vs sandboxed iframe.** Default = **sandboxed iframe, remote
   images off by default + "show images" toggle** (best privacy/security). Alternative:
   server-side bleach sanitize into the existing pane. Either way, **not raw `v-html`.**
3. **Attachments: live-fetch vs persist.** Default = **live-fetch** (no storage bill).
   Persist to `OutlookAttachment` + R2 only for P2.3 "save to job" (that one genuinely
   needs a stored copy).
4. **Tag on inbound only, or inbound + outbound?** Default = **both** (outbound to a
   customer is still customer correspondence); revisit if outbound noise pollutes tabs.
5. **backfill_days default depth on connect.** Default = keep **90**; large mailboxes hit
   the `BACKFILL_MAX_MESSAGES_PER_RUN=5000`/folder cap (`tasks.py:45`) — acceptable for a
   shop mailbox, flag if a power user needs deeper.
6. **D6 auto-email automations:** revive (wire `dispatch_trigger` to invoice/job/estimate **[DECIDED 2026-08-31: removed — `dispatch_trigger`, its tests and the Outlook Auto-Email templates tab are deleted; Event Rules (`modules/workflows`) is the one event-email path. See unimplemented-endpoints-decision-list.md § 2026-08-31.]**
   events) or delete the dead module. Needs a Doug call — it sends customer-facing mail,
   so reviving it is not a silent change.
7. **Non-owner write actions (audit round 1).** Reads fan out to many users, but
   mark-read / move / folder ops use `_account_for_user(uid)` and are **already
   owner-only** — a tech can read a shared message but not mark or move it (404 today).
   Default = **leave write actions owner-only in P0** (read is the 90% need; Doug is the
   sole connected user today, so this is latent). Revisit if/when a second person
   connects. Flag if the shared inbox is meant to be write-shared now.

## Risks / gotchas captured for the build

- **Owner-token resolution for ALL live-fetch** (0.1, 0.3 — audit round 1, the
  foundational one) — body + attachment fetch must use the mailbox **owner's** identity
  (`msg.account_id → OutlookAccount.user_id`), not the viewer's, or every non-owner gets
  "reconnect." Ship a helper (`_owner_client_for_message(msg, ...)`) and route both
  features through it. Verify as a non-owner.
- **XSS/tracking via email HTML** (0.1/decision 2) — the single biggest new *security*
  risk; sanitize or sandbox, never raw.
- **Graph rate limits** — 0.1 and 0.3 add per-open Graph calls; they're user-initiated
  (not bulk), but cache the body/attachment list for the life of the open detail pane.
- **`move_message` changes the Graph id** (`graph_client.py:346` note) — 0.1's live body
  fetch keys off `graph_message_id`, which the move path already reconciles
  (`folders_router.py:558-563`); confirm a just-moved message still opens.
- **Re-tag backfill cost** (0.4) — one `email_hash` lookup per untagged row; batch +
  commit in chunks on a big mailbox.
- **Visibility must wrap every new read** — the attachment endpoints and search MUST run
  through `can_view` / `filter_visible`, same as the existing views. This is the codebase's
  stated invariant (`visibility.py:5`).

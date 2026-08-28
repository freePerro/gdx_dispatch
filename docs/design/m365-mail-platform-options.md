# Microsoft 365 mail: what the GDX inbox could use, and whether the mirror is the right shape

Status: **RESEARCH — decisions pending Doug** (2026-08-27). Rung 1 (flag → top) is PR #510, from
`feat/inbox-flagged-to-top`, see `inbox-flagged-to-top.md`. Nothing else here is built.

Trigger: "if I pin something in Outlook, can it show pinned in GDX?" — and then "what we have
seems feature-less; dive deep, show me options."

## 0. What already exists (do not rebuild)

Verified against code 2026-08-27 (`modules/outlook/`, 18 files, ~7.8k lines):

| Already built | Where |
|---|---|
| Per-folder Graph **delta sync** storing the full deltaLink; 410 → full resync | `tasks.py::_sync_one_folder` |
| **Change-notification webhooks** (`/me/messages`, created+updated, 60 h lifetime, 6-hourly renew + self-heal create) with a 30-min fallback poller and hourly sync-health alarm | `subscriptions.py`, `webhook_router.py`, `core/scheduler.py` |
| Read/unread both ways, move, delete (→ Deleted Items), folder create/rename/delete/empty, folder colour/pin prefs | `folders_router.py` |
| **Reply in-thread** via Graph `/reply` (real In-Reply-To), forward via native `/forward`, real Graph drafts, attachments on send | `send_router.py` |
| AI draft reply, create Planner task from mail, new estimate from mail, link to customer/job (auto by email hash, `[Job #…]` subject marker, AI; manual override), personal/shared privacy, role-based visibility | `views_router.py`, `tagger.py`, `visibility.py` |
| Attachments live from Graph, download, **save to job**; bodies live-fetched in a sandboxed iframe | `views_router.py`, `EmailBodyFrame.vue` |
| Vendor-bill ingest from allow-listed senders (statement → order → LLM rungs) | `vendor_bill_ingest.py` |
| **Bounce detection** → estimate `rejected` / invoice back to Unsent | `bounce_detect.py` |
| Customer / job email timelines, sidebar unread badge, mobile inbox (read, reply, forward, task, personal) | `EmailTimeline.vue`, `MobileInboxView.vue` |

So the inbox is not feature-less on the backend. What is thin is **the list itself**: one fixed
sort, no filters, no flags/categories/importance, no full-mailbox search, junk/deleted folders
that render but say "open in Outlook", and a mobile view with no folders, link, move or delete.

### Dead or half-wired (inventory 2026-08-27)

- `automations.py::dispatch_trigger` — zero callers; the Auto-Email settings tab says so itself.
- `OutlookAttachment` table, `body_r2_key`, `in_reply_to`, `OutlookAccount.delta_token` — columns/tables nothing writes.
- `graph_client.copy_message`, `move_folder`, `get_mailbox_settings` — no callers.
- `tasks/email_poller.py` (IMAP) — registered, no beat entry, parallel `inbound_emails` schema.
- `email.draft` MCP tool — local-only draft that Graph never sees; refers to an `email.send` tool that doesn't exist.
- `EmailTimeline` "Open in Inbox →" goes to `/inbox`, not the message (no deep-link route).
- Stale docstrings: fallback poller "15 min" (it is 30), models header "5 tables" (7).

## 1. The pin question — closed

Outlook's *pin* has **no Graph surface**: the v1.0 and beta `message` resources (fetched
2026-08-27, ms.date 2024-08-23) contain no property that mentions it, it is absent from the
Update-message writable list, and no MS-OXPROPS tag is published for it. A Nov-2025 Q&A confirms
pins *do* sync server-side between OWA and new Outlook, so the state exists — Microsoft just
hasn't exposed it. Treat as unavailable. The **follow-up flag** (`flag.flagStatus`) is the
readable+writable stand-in → rung 1 below.

## 2. Graph features the inbox is not using

All v1.0 unless marked; scopes already granted: `Mail.Read Mail.ReadWrite Mail.Send
MailboxSettings.Read` (`token_refresh.py:29`). Sources: Microsoft Learn, cited by page + ms.date.

| Feature | Primitive | Scope | Notes |
|---|---|---|---|
| Follow-up flag (+ start/due/complete dates) | `flag` (followupFlag), PATCH on any message | Mail.ReadWrite ✓ | Due date needs start date or 400. *(followupFlag, 2024-04-03)* |
| Importance | `importance` low/normal/high, PATCH | Mail.ReadWrite ✓ | Also a KQL term. |
| **Categories** (labels visible in Outlook) | `categories` on message; master list `/me/outlook/masterCategories` (25 preset colours, name immutable) | Mail.ReadWrite ✓ + **MailboxSettings.ReadWrite ✗ (not granted)** | Two-way "Job #1234 / Needs quote" labels. *(outlookCategory 2024-08-08)* |
| Focused / Other | `inferenceClassification`, overrides (max 1000) | Mail.ReadWrite ✓ | Personal-inbox feature; low value for a service inbox. |
| `replyTo` | select it | Mail.Read ✓ | **Current reply path ignores it** — RFC says reply to `replyTo`, not `from`. Cheap correctness fix. |
| `webLink` | select it; `?ispopout=0` | Mail.Read ✓ | Opens OWA in a new tab. **Cannot be iframed**; no desktop-app URI scheme exists (Q&A 2024-09-18). |
| Internet headers | `internetMessageHeaders` (must `$select`) | Mail.Read ✓ | `Auto-Submitted`, `List-Unsubscribe`, DKIM/SPF results, our own `x-gdx-job-id` on outbound. |
| `uniqueBody` | select it; `Prefer: outlook.body-content-type="text"` | Mail.Read ✓ | The new part of a reply without the quoted thread — better previews + AI-draft input. |
| Inline images | `attachments` with `isInline`/`contentId`; match `cid:` in body | Mail.Read ✓ | **`hasAttachments` is false for inline-only mail** — customer door photos pasted inline are invisible today. |
| `conversationIndex` | select it | Mail.Read ✓ | Orders a thread without trusting timestamps. |
| Full-mailbox search | `$search="…"` KQL on `/me/messages` (≤1000 hits) or `POST /search/query` (searches attachment text) | Mail.Read ✓ | Current search is a LIKE over subject/sender/preview of the **mirror only**. *(search-query-parameter 2025-07-03)* |
| Server-side quoted replies | `createReply` / `createReplyAll` / `createForward` → PATCH → `send` | Mail.ReadWrite + Mail.Send ✓ | Already use one-shot `/reply`; the draft form allows editing the quoted body in-app. |
| Archive | `move` to well-known `archive` (`Prefer: IdType="ImmutableId"` avoids id churn) | Mail.ReadWrite ✓ | One-click archive from the list. |
| Inbox rules | `/me/mailFolders/inbox/messageRules` | MailboxSettings.ReadWrite ✗ | Better set once by hand in Outlook. |
| @mentions | `mentionsPreview`, `mentions` | **beta only** | Not for production. |
| Snooze | — | — | No API (`scheduled` folder exists, no way to schedule). Emulate: move + timer + move back. |
| Extended MAPI props | `singleValueExtendedProperties` (`PidTagFlagStatus` 0x1090 etc.) | — | Read-only on non-drafts; everything useful is already surfaced as `flag`. |

## 3. Real-time and limits

- Subscriptions: max **10,080 min (<7 d)** for messages, 1,440 with resource data; latency avg <1 min; 1,000 subs/mailbox. We run 60 h + renew — fine. Lifecycle notifications (`missed`, `subscriptionRemoved`, `reauthorizationRequired`) are **not** wired; the 30-min poller covers it.
- Throttling: **10,000 requests / 10 min / (app, mailbox)**, **4 concurrent**. A one-mailbox shop cannot get near this with delta + webhooks; the live-fetch of bodies and attachments is what to watch (4 concurrent).
- Delta: per-folder only; `$select` is baked **inside the opaque `$deltatoken`** (prod's 63 links carry no `$select=` in the URL) — **adding a field does nothing until the folder re-walks**. Rule: every `_DEFAULT_SELECT` change ships a migration that sets `full_resync_required` (082 is the template). A URL-inspecting guard was tried and killed by audit — it would have re-walked on every sync.

## 4. Architectures — the honest tradeoffs

| # | Shape | Verdict for one mailbox |
|---|---|---|
| A | **Mirror (current)** — delta as truth, webhook as trigger, write back a small property set | **Keep.** Every shared-inbox vendor (Front, Missive, Superhuman) lands here. The mirror isn't the problem; the mirror being thin is. |
| B | Live proxy — call Graph per page view, store only a link table (ids ↔ customer/job) | Zero drift, no mail at rest; but 4-concurrent cap, latency per page, blank inbox on outage, no cross-mailbox joins for reports. A reasonable fallback for junk/deleted folders only. |
| C | Embed OWA | **Blocked** — `webLink` "cannot be accessed from within an iFrame". Deep-link out only. |
| D | **Outlook add-in** (Office.js task pane: "Link to customer/job", "Create task", "Open in GDX") | Highest ceiling: context flows *into* Outlook where staff already read. Needs nested-app auth (MSAL NAA, legacy tokens are off), XML manifest for mobile, admin-center deploy (24–72 h). A second deployable with its own auth. Do after the list is worth linking into. |
| E | Shared mailbox (service@) + **application** Mail.Read scoped by Exchange RBAC-for-Applications | Stops the sync depending on one person's delegated token; team inbox by design. `.Shared` delegated scopes can't subscribe to webhooks — application permission is the way. A real change to consent + Entra setup. |
| F | Graph connectors → Copilot/M365 search | Only pays with Copilot seats. Skip. |
| G | Power Automate glue | Adds an unaudited second mutation path. Skip. |
| H | Retired: Outlook Customer Manager (2020) | Microsoft's own answer is now "Dynamics" or an add-in (D). |

Competitors: Jobber / Housecall Pro / ServiceTitan do **no** mailbox integration (outbound only, Zapier for the rest). HubSpot = connected inbox + Outlook add-in. Front/Missive = full Graph sync and explicitly **skip flags and categories**. GDX already has more inbound integration than any field-service peer.

## 5. Recommended ladder (value ÷ effort)

| Rung | What the office gets | Primitive | Size |
|---|---|---|---|
| **1 ✅ built** | Flag in Outlook → top of GDX inbox; flag/unflag from GDX | `flag`, PATCH | S |
| 2 | **Inline photos** show up (and can be saved to the job) | attachments `isInline` + `cid:` match | S |
| 3 | Reply goes to `replyTo`; previews use `uniqueBody` | `$select` additions | S |
| 4 | **Search the whole mailbox**, incl. attachment text, from the same box | `$search` / `/search/query` behind the existing search input | M |
| 5 | List controls: Unread / Flagged / Has attachments / Linked–unlinked filters; importance marker | columns already mirrored + `importance` | M |
| 6 | **Categories** as two-way labels ("Job #…", "Needs quote", "Waiting on customer") | masterCategories + `categories`; needs `MailboxSettings.ReadWrite` re-consent | M |
| 7 | Archive from list; junk/deleted via live proxy instead of "open in Outlook" | `move`→archive; live list for two folders | M |
| 8 | Mobile parity: folders, link, move, archive, flag | existing endpoints, UI only | M |
| 9 | Outlook add-in "Link to job" | Office.js + NAA | L |
| 10 | Shared-mailbox + app-permission sync | Entra + RBAC | L |

Rungs 2–5 need **no new consent** and no schema beyond a few columns; 6 needs one re-consent; 9–10 are product decisions.

## 6. Questions for Doug (product shape)

1. Is the inbox a **personal** view of Doug's mailbox (today) or a **team** inbox (rung 10)?
2. Categories: fixed vocabulary (rung 6) or free-form? Fixed is the only thing that stays tidy in Outlook.
3. Does the office want to work *in GDX* (rungs 2–8) or *in Outlook with GDX context* (rung 9)? Both is fine; the order matters.

## 7. Rejected here

- Pin sync — impossible (§1). Snooze — no API. @mentions — beta only. Copilot connectors / Power Automate — cost or audit gap. Embedding OWA — blocked by Microsoft.

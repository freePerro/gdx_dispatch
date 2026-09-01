# Remove the Communications screen — a third messaging system that never worked

Status: **RELEASED v1.113.0** — MERGED #549 (2026-09-01), prod + demo rolled
(2026-08-31; plan written 2026-08-26 against `origin/main @ f1cbd97`). Decisions
taken: §7.1 **delete**; §7.2 the DNC routes **go** (they never stored anything
durably); §7.3 `/messages` and `/inbound-comms` — and `/communications` itself —
**redirect to `/inbox`**. The `communications` module key is **kept, not
retired** (see §5 correction below): `routers/notifications.py` gates the bell
on it and prod holds the grant; its display name is now "Notifications". Step 1
shipped separately as **#492** (repointing notification destinations off
`/communications`). Regression guard: `tests/test_communications_shell_removed.py`.

`/communications` is a parallel implementation of two features that already
exist, work, and are in daily use. Its storage is a dict in process memory. Its
senders are unconfigured on prod. It reports success when it sends nothing.
Nobody has ever used it.

---

## 0. What already exists — and this time it is the whole argument

There is nothing to migrate, because both channels already shipped for real:

| Channel | Real implementation | Prod state 2026-08-26 |
| --- | --- | --- |
| Email | `/inbox` → `InboxView` → `modules/outlook` (8 tables) | **3,077 messages**, 1 connected account |
| SMS | `/phone-com/messages` → `phone_com_messages` | **155 messages** |
| *Both, fake* | `/communications` → `routers/communications.py` | **RAM. 0 sends, ever.** |

Your own nav already says so. `constants/modules.js:87` describes Phone.com SMS
as *"SMS threads on the Phone.com line **(separate from built-in
Communications)**"*. Someone met this duplication, wrote it down, and moved on.

## 1. Why it cannot work, not merely why it is unused

**Storage is a module-level dict.** `routers/communications.py:83`

```python
_EMAILS_BY_TENANT: dict[str, list[EmailMessage]] = {}
```

Threads, messages and the timeline live in the API process. They do not survive
a restart — and prod was restarted twice on 2026-08-25 deploying v1.102.0.
There is no table behind this screen and never was.

**The senders are unconfigured on prod.** `EMAIL_PROVIDER`, `MAIL_SERVER`,
`AWS_REGION`, `SMS_PROVIDER`, `TWILIO_ACCOUNT_SID`, `PHONE_COM_TOKEN` — all
unset. `core/email.py::send_email` therefore returns
`{"sent": False, "reason": "not configured"}`.

**And it says it worked anyway.** The route turns that into `status: "failed"`
and returns **HTTP 201** (`routers/communications.py:674-680`); the UI passes
`{ successMessage: 'Message sent' }` (`CommunicationsView.vue:691`) and never
reads `status`. Compose an email to a customer, hit send, get a green toast,
nothing leaves the building. That is invariant-level: *"an action that fakes a
success response without doing the work."*

**The contrast is the proof this is fixable-by-deletion, not by care.** The same
`core/sms.py` is consumed by three places. `routers/voice.py:74` and
`routers/dispatch_scheduling.py:169` both handle the unconfigured case
honestly — *"Twilio credentials are absent — prod's actual state"*, *"every
not-sent path must name itself"* — and record the reason. Only the fake screen
claims success. The problem is not the sender; it is this screen.

## 2. Blast radius: zero

* **0** `communication_sent` audit rows on prod, ever (audit spans 2026-06-22 →).
* **0** consumers of any of its 13 routes outside the SPA — no MCP tool, no
  plugin, no Python caller, no mobile surface.

## 3. What goes

| Item | Size | Note |
| --- | --- | --- |
| `routers/communications.py` | 828 lines, 13 routes | whole file |
| `frontend/src/views/CommunicationsView.vue` | 1,016 lines | whole file |
| `core/email.py` | 91 lines | **falls out free** — this router is its only non-test importer, which closes `email-readability-and-delivery-plan` Phase 4b |
| Nav entry | `constants/modules.js:84` | `communications` key |
| Module toggle | `components/AppTopbar.vue:234` | `communicationsEnabled` |
| Routes + redirects | `router/index.js:179,240,246` | `/communications`, `/messages`, `/inbound-comms` |
| Tests | `tests/test_communications.py`, `tests/test_email_sms.py`, `tests/e2e/test_communications.py` | |
| QA probe | `tools/qa_tier1.py:32` | drop `/api/communications/threads` from the sweep list |
| The `communications` tenant module flag | | must be retired, not left dangling |

**What does NOT go: `core/sms.py`.** It has two other consumers
(`routers/voice.py:74`, `routers/dispatch_scheduling.py:169`) which use it
correctly. Deleting it with this feature would break two working paths.

## 4. The DNC routes get their own decision — not a footnote

`/api/communications/dnc` (POST / DELETE / GET) live in this router. They are
backed by `_DNC_LIST_BY_TENANT` (`:756`), an in-memory set that empties on
restart, with **no UI caller**. Functionally nothing is lost by deleting them.

**But do-not-contact carries legal weight, and removing a compliance surface
inside a commit about a fake messaging screen is how one disappears without
anyone deciding to remove it.** So it is stated here on its own line:

> These routes never stored a do-not-contact instruction durably. The real
> opt-out is `customers.email_opt_out` / `sms_opt_out`, and **that** is
> currently written and never read — see
> `contact-opt-out-suppression-plan.md` (#491). Deleting these routes removes
> nothing that was protecting anyone; the protection was never there. It must
> still be an explicit decision, recorded here, rather than a side effect.

## 5. Traps

* **Do not delete `core/sms.py`** (§3). Two working consumers.
* **The `communications` module flag** is checked by `AppTopbar.vue:234`. A
  tenant with it enabled must not be left pointing at a route that no longer
  exists — retire the flag in the same change.
  **Correction (2026-08-31, at build time):** the flag cannot be retired. It
  is the gate on `routers/notifications.py` (the in-app bell, and the reason
  `AppTopbar` reads it at all), and on `routers/inbound_comms.py` (admin) and
  `routers/ai_communication.py`. Retiring it would 403 the bell on every
  install that holds the grant — prod does. The key stays; what changed is its
  display name (`core/modules.py` → "Notifications"), the topbar computed
  (`notificationsEnabled`, with the key's history in a comment), and the nav
  entry, which is gone.
* **`routers/voice.py` owns `POST /api/communications/missed-call`** — a
  Twilio voice webhook (#187) on the same URL prefix that was never part of
  this router and consumes `core/sms.py` correctly. It stays; the route-table
  guard allowlists it by name.
* **`/messages` and `/inbound-comms` are bookmark redirects** added 2026-04-29.
  Removing the target turns two live URLs into dead ones. Decide: drop them, or
  repoint at `/inbox`.
* **`qa_tier1.py` will start failing** on a 404 for a route that was
  deliberately removed — update the sweep list in the same PR or the next QA run
  reports a false regression.
* **The absence guard must be a route-table assertion**, not a status-code one.
  This app answers **405** for a POST to any unmatched path (the SPA catch-all
  is GET-only on `/{full_path:path}`) — a path that never existed returns 405
  too. Asserting 404 passes in the test app and describes something production
  never does; that exact mistake shipped in #485 and was corrected in #486.

## 6. Verification

* Full backend matrix; every FAIL and SKIP named.
* Route table via `tests/conftest.py::iter_app_routes` asserting zero
  `/api/communications/*`, `/api/sms/*` and `/api/inbox/*` routes remain, and
  that nothing imports `core.email`.
* Frontend suite; no dangling import of `CommunicationsView`.
* Browser walk on a throwaway: the nav no longer offers Communications; `/inbox`
  and `/phone-com/messages` still work; a bookmark to `/communications` lands
  somewhere sensible rather than on a blank router error.
* Prod after deploy: `outlook_messages` and `phone_com_messages` unchanged.

## 7. Open decisions — DECIDED 2026-08-31 (this build)

1. **Delete** (not hide-and-hold). 2. **The DNC routes go** — they never stored a
do-not-contact instruction durably; the real opt-out is `customers.email_opt_out`
/ `sms_opt_out` (#491), unchanged. 3. `/messages`, `/inbound-comms` and
`/communications` **redirect to `/inbox`**. Original questions kept below.

1. **Delete, or hide-and-hold?** Deleting is reversible in git but not in
   muscle memory. `4 · Long-term hold` exists in the ledger now and would fit —
   hide from nav, keep the code. Recommended: **delete**, because a screen that
   fakes success is worse than one that is missing, and two working
   replacements already carry the load.
2. **§4 — confirm the DNC routes go** with the rest.
3. `/messages` and `/inbound-comms`: drop, or repoint to `/inbox`?

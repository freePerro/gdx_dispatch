# Mobile: One Job Card

**Date:** 2026-08-21
**Status:** PLAN — nothing built. Decision locked (Doug, 2026-08-21): one card
component, always compact, on all three mobile surfaces; the job actions live
on the job detail screen only.

**Related plan — read it first:** `tech-mobile-workflow-plan.md` (v3,
2026-07-17), whose status line was corrected to PARTIALLY BUILT by **#389**
(`6b4e7af`, 2026-08-21) — that header is current; its *body* still describes a
July world. That doc's **"extraction question" is dissolved by this decision**,
not answered by it:
if Today's Route carries no actions, there is no action duplication to share,
so its recommended `useJobActions()` + `<MobileJobActionBar>` are **not
needed**. Its PR A (one job shape) is still live and largely shipped.

---

## What already exists (do not rebuild)

Verified against code on 2026-08-21, not against any doc.

### The job detail screen is already a real work surface

`MobileJobDetailView.vue` is **1,697 lines**, not the 224-line stub the rival
plan describes. Eight of the eight actions that plan wanted it to "gain" have
shipped:

| Action | Where | Status |
|---|---|---|
| On my way | `MobileJobDetailView.vue:619` | ✅ shipped |
| I'm here | `:627` | ✅ shipped |
| Complete (closeout dialog) | `:635` | ✅ shipped |
| Bill / collect (invoice dialog) | `:643` | ✅ shipped |
| Navigate | `:651` | ✅ shipped |
| Add note | `:314` | ✅ shipped |
| Add photo | `:327` (`usePhotoQueue`) | ✅ shipped |
| Request part / Used it | `:543` / `:534` | ✅ shipped |

Its own doc's body still calls it a 224-line stub with "Actions: None"; the
corrected header says otherwise. Read the code.

It also carries Customer, Fix/Add address, add email + contact, Description,
Install Specs (`DoorSpecList`), Notes, Photos, Parts, Time, and a closeout
summary — none of which are on Today's card.

### The backend already emits one job shape for two of the three surfaces

The rival plan's PR A largely shipped; #389 recorded that. `get_mobile_job_detail`
(`routers/mobile.py:2198`) now emits **nested `job["customer"]`**, a
server-built **`navigation_link`**, and the full `site_*` block — the code
even carries PR A's reasoning verbatim ("two copies of one customer in one
payload is a divergence trap"). Do not rebuild this.

### Two traps from the rival plan are already closed

- **Trap #4 "no `useToast` in the detail view"** — stale. It imports
  `useToast` at `:699` and instantiates at `:709`.
- **Trap #6 "offline banner is Today-only"** — stale. The detail view has
  offline handling (`useOfflineSync`, `queuedWriteStatus`).

---

## The problem

Three separately-written card markups for one concept, and the primary one is
not tappable:

| Card | File | Links to detail? | Size |
|---|---|---|---|
| Route card | `MobileTodayView.vue:1186-1448` | ❌ **no** | ~260 lines |
| "In the area" card | `MobileTodayView.vue:1467-1481` | ✅ yes | ~15 lines |
| Jobs-list card | `MobileJobsView.vue:79-115` | ✅ yes | ~40 lines |

A tech standing in front of the job, on the screen they use all day, has no
path from the route card to the job's notes, photos, description, install
specs, or contacts.

**This bug class was already fixed once and this instance was missed.** Commit
`67b8a5d` (2026-07-16) created `MobileJobDetailView`, made the Jobs-list card
a link — its own comment reads *"cards used to be display-only — a tech had NO
path from this list to notes/phone/description"* — and touched
`MobileTodayView` (+84 lines) to add the area section **with** a link. The
route card in the same file was left unlinked. No sibling sweep.

---

## The decision (Doug, 2026-08-21)

**One card component. Always compact. Same card everywhere.** The job actions
move to the job detail screen; the card's job is to identify the job and open
it.

The trade-off was named before the choice and accepted: every status advance
becomes tap-in → act → tap-back on the screen techs use all day.

### Field usage, re-measured on prod 2026-08-21 (this settles it)

The rival plan's July counts are **obsolete**. The tech mobile workflow went
live in the last ~3 weeks and is in **daily** use:

| Signal | July 2026 (rival plan) | Prod, 2026-08-21 |
|---|---|---|
| Jobs | 205 | 230 (29 in last 30d) |
| En route | — | 20 |
| Arrivals | 4 | **18** |
| Closeouts | 4 | **19** (14 in last 30d) |
| Photos from the field | 0 | **51** (first 2026-07-29, latest 2026-08-20) |
| Parts logged from the field | 0 | **73** (69 `request`, 4 `closeout`) |

Two conclusions, pulling in opposite directions — both matter:

1. **The decision is safer than July suggested.** Photo capture exists *only*
   on the job detail screen. 51 photos in three weeks means techs are already
   routinely tapping through to it. The destination of this plan is proven
   reachable and in daily use; the tap-through is not a hypothetical.
2. **The stakes are higher than July suggested.** This is no longer a dead
   surface nobody would miss. It is live, in daily use, and a regression costs
   real field work today. PR A must be *complete* before PR B removes
   anything, and the headed walk is not optional.

---

## PR A — the detail screen gains what only Today has (additive, nothing removed)

Ships value on its own and leaves Today untouched. **Must land before PR B**,
or removing Today's action row orphans three dialog components with no caller
— a "no orphans in either direction" defect.

| Moving | Component / behaviour | Currently only on Today |
|---|---|---|
| Build quote / Show quote | `MobileQuoteBuilderDialog` + `presentQuote` | `MobileTodayView.vue:1405` |
| Change order | `MobileChangeOrderDialog` | `:1429` (`mtv-change-order-btn`) |
| Chat with dispatch | `MobileChatDialog` | `:1437` |
| Install & equipment | equipment expander + panel | `:1331` |
| Job context | priority tag, alerts, multi-tech, customer notes | `:1195`–`:1265` |

Verified absent from `MobileJobDetailView.vue`: `Chat`, `Change order`,
`equipment_type`, `alerts`, `priority`, `otherTechs`, `customer.notes`, and
any quote action ("quote" appears there once, in a comment on `:264`).

Backend gap to close in this PR: the detail payload does not emit
`parts_summary`, `priority`, `alerts`, or `service_type`. Add them via the
same `_job_card` enrichment `/today` uses, plus **a parity test asserting
`/today` and `/job/{id}` produce the same card keys for the same job** — the
rival plan's idea, still unbuilt, and the only thing that stops these two
drifting apart again.

## PR B — one card component, adopted on all three surfaces

New `components/MobileJobCard.vue` — compact, always a `router-link` to
`/mobile/jobs/:id`, chevron affordance, `:active` press state, ≥44px target.

Replaces all three markups. Today's route card sheds its action row, both
expanders, and both inline panels.

**Route-level chrome stays on the list, not the card:** stop number, drive-time
leg separators, and reorder mode are properties of *the route*, not of a job.
Recommend a named slot on the card for the leading badge (stop # in Today,
nothing elsewhere) and keeping reorder controls in the `<li>` wrapper.

**The field-shape mismatch is the real work.** The three surfaces read
different keys for the same data:

| Concept | `/today` + `/job/{id}` | `/mobile/jobs` list (`routers/mobile.py:1691`) |
|---|---|---|
| Customer name | `job.customer.name` (nested) | `customer_name` (flat) |
| Address | `job.site_address` | `display_address` |
| Tech | `assignments[]` | `assigned_tech_name` |

Fix it **server-side** — bring the list endpoint onto `_job_card` — rather
than prop-mapping in the component. Client-side mapping leaves the divergence
in place and is exactly what PR A's nested-customer decision rejected.

---

## Adversarial audit, 2026-08-21 — PR B is GATED, not cleared

Pre-code audit against the plan commit `7e6926f`. Five findings, **all five
independently re-verified against source by the author before acceptance.**
The audit's verdict: *do PR A; do not do PR B as written.* Accepted.

### A1 — The foundational lie: "the detail screen is a complete substitute"

It is a complete substitute **online only.** Two structural facts, neither
fixed by porting buttons:

- `MobileJobDetailView.vue:1047-1070` — `advance()` deliberately does **not**
  flip status locally; it `await refresh()`es. Its own comment says why:
  *"Refetch rather than guess the new state locally — Today flips the status
  before checking the result and never rolls it back on failure… Don't copy
  that."* Offline the refetch throws into the outer `catch`, so the tech sees
  **"Saved offline" and "Could not save" together**, and the button still
  reads "On my way". Status never advances.
- `MobileTodayView.vue:248-258` / `:303-312` flips `job.dispatch_status`
  unconditionally, so offline progression works **there and only there**.
- `load()` (`:96`) has no cache. Offline, a tap-back remount leaves
  `jobs.value = []` → the route reads "Nothing scheduled today." **The tech
  loses the route mid-day.**

Neither surface is correct today: Today flips without rollback, detail does
not flip at all. **The right fix is flip + roll back on throw** — the July
plan's trap #1 — and it belongs to *both*.

### A2 — Multi-job state is unportable

The route card holds **N jobs**. `refreshAllUnseenCounts()` (`:736`) computes
dispatch's part answers across *all* stops and toasts "tap the parts row" — a
row PR B deletes. `advancing` is per-job (`advancing[job.id]`); detail's is a
single boolean, so a tech cannot start stop #4 while #3 syncs.

### A3 — The key-parity test is theater (author's own)

`_job_card`'s nested customer carries `notes`+`tags` and **no `email`**
(`routers/mobile.py:1002-1007`). Detail's hand-built customer carries `email`
and no notes (`:2251-2263`). `MobileJobDetailView.vue:921` reads
`job.value?.customer`; `:172-178` renders `customer.email` as a `mailto:` row
and `:210` offers "Add email" when it is missing — beside a comment noting
**219 of 382 customers have no email at all.** Swapping detail onto
`_job_card` deletes that row, and a top-level key-parity assertion **goes
green on the exact swap that breaks the screen.** July caught this; this
plan's "re-verified" list dropped it. Any parity test must descend into
`customer`, or unify the sub-dict first.

### A4 — The blind spot: tap-to-navigate

`MobileTodayView.vue:1238-1240` — `.job-address` carries
`@click="openMaps(job)"`. **Tap the address, maps opens.** That is the gesture
used between every pair of stops. `MobileJobsView.vue:104-110` has no such
handler *precisely because* it already sits inside a `router-link`. Making the
route card a link forces the same removal — or nests an `@click` inside an
`<a>` and fires both. `openMaps` appeared nowhere in this plan.

### A5 — Trap #5 was stale in this doc

Detail **does** offline-queue part requests (`:1282`, `postQueued`). Only
Today's parts path does not. The trap list below is corrected.

### What this changes

**PR A proceeds as planned** — additive, no removals, endorsed by the audit.

**PR B is gated.** It may not ship until all four are true:

1. The detail screen advances status **offline** — optimistic flip **with
   rollback on throw**, on both surfaces. This is a **live bug today**,
   independent of this plan: any tech opening a job from the Jobs list in a
   dead zone hits it right now. Filed as its own PR, never bundled.
2. Today's list survives a cold remount with no network (cache the route, or
   do not navigate away for status).
3. Tap-to-navigate survives, by design and by test.
4. The customer sub-dict is unified **before** any endpoint swap, and the
   parity test descends into it.

**The two-PR split protects code, not techs.** There is no gate, metric, or
flag between A and B, and B is a ~270-line deletion of working field code.
Real safety: ship A, deploy, confirm closeouts and part requests keep flowing
from the detail screen, *then* revisit B.

**On the evidence used to justify B:** 51 photos prove techs reach detail
*for photos* — the only place photos exist. That cannot prove they would
accept losing in-card actions. The 73 part requests are `source='request'`
from **both** surfaces and are unattributable; this plan used them as consent.
They are not.

**Honest answer to "is the tech better off?"** After PR B as written:
**worse.** One thumb-tap to go en route becomes tap → GET → tap → tap back →
GET, and in a dead zone it does not advance at all and empties the route on
the way back.

---

## Known traps (re-verified 2026-08-21)

Carried from `tech-mobile-workflow-plan.md` §"Known traps", re-checked:

1. **Optimistic flip is never rolled back** — `dispatch_status` is set before
   the result is checked and not reverted on throw. Still live. Do not carry
   it into the detail view.
2. **Actions mutate the `job` prop object** — works in Today (array item);
   detail's `job` is a standalone ref. Decide mutate-and-document vs
   `emit('updated')`.
3. **`tests/test_mobile_job_cards.test.js` mocks `api.get` with
   `mockResolvedValueOnce`** — exactly once. Any extra mount-time GET resolves
   `undefined` and throws. Lazy-load on demand.
4. ~~No `useToast` in the detail view~~ — **stale, closed.**
5. ~~Parts create/edit is not offline-queued~~ — **stale for the detail
   view**, which queues at `MobileJobDetailView.vue:1282`. Still true of
   **Today's** parts path only. Corrected by audit finding A5.
6. ~~Offline banner is Today-only~~ — **stale, closed.**
7. **`MobileInvoiceDialog`'s header is "Close out"**, the same wording as the
   closeout trigger. Today's trigger says "Close out"; detail's says "Bill /
   collect". Unify on "Bill / collect" when the two land on one screen.
8. **Testids to preserve:** `mtv-change-order-btn`, `parts-summary-{id}`,
   `equipment-summary-{id}`, `equipment-panel-{id}`, `mobile-area-job-{id}`,
   `mobile-job-card-{id}`, all `mjco-*` / `mid-*`.

Added here:

9. **`useDestructiveConfirm` auto-accepts silently** (issue #215) — any
   confirm added on this path does not actually confirm.
10. **jsdom applies no media queries.** Card layout at 375×812 is provable
    only in a real browser.

---

## Verification

- Backend: pytest — detail-payload parity with `/today`, list endpoint on
  `_job_card`, ownership gate unchanged (404-not-403).
- Frontend: vitest — one card renders on all three surfaces; every instance
  links; action guards by `dispatch_status` on the detail view.
- e2e: touch targets ≥44px at 375×812 on `/mobile`, `/mobile/jobs`,
  `/mobile/jobs/:id`.
- **Headed walk on the Android emulator or a real phone, as a tech** — light
  and dark. The walk that PR #153 never got and this plan still owes: tap a
  route card, reach the job, advance status, come back, confirm the route
  survived the round-trip.

---

## Open items

- ~~Re-measure the field counts before building.~~ **DONE 2026-08-21** —
  measured on prod, table above. The July figures were stale by an order of
  magnitude.
- **Sibling finding, out of scope:** `job_photos.created_at` is a nullable
  vestigial column, 0 of 51 rows populated; the live column is `uploaded_at`
  (NOT NULL, 51/51). Querying `created_at` silently returns zero rows — it
  cost one wrong measurement in this very plan. Likely the `schema_reconcile`
  auto-ALTER drift pattern. Worth a follow-up issue, not this PR.
- **Reorder mode + stop numbers**: slot on the card, or list-wrapper only?
  Recommendation above; not yet decided.
- **Does the compact card keep the status pill and time?** The Jobs-list card
  shows status + date; the area card shows neither. Pick one and apply it
  everywhere — that is the point of the exercise.
- `tech-mobile-workflow-plan.md` needs one more edit: its header is already
  correct (#389), but the **body** still recommends
  `useJobActions`/`MobileJobActionBar`, which this decision supersedes. Mark it
  there, in that doc, so the two plans cannot be read as disagreeing.
- **`/api/mobile/jobs/{id}/clock-in|clock-out` have zero frontend callers**
  (#389's header names this). Orphaned endpoints on the same screen this plan
  touches — not in scope here, but decide whether PR A's detail-screen work is
  the natural place to finally call them.

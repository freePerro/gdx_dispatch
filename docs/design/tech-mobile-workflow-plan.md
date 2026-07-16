# Tech Mobile Job Workflow — Implementation Plan (v2, audit round 1 folded in)

**Date:** 2026-07-17 · **Status:** awaiting Doug's approval. No code written.
**Audit round 1 [AR1]:** adversarial review of the v1 plan verified every claim against code. Five substantive corrections, all folded in below — v1's central recommendation was **inverted**:
- **(a) Photos are NOT online-only.** `lib/offlineDb.js:35` already ships a `photos` Blob store ("captured photo blobs awaiting upload… blob stored as Blob (not base64)") with **zero writers and zero readers**. v1 read `postQueued` alone, inferred a capability ceiling, and planned to ship the headline feature broken in exactly the dead zones it's used in. Same error class as inferring intent from a function name.
- **(b) "~40 duplicated lines" was a 6× lie.** Measured: 86 (`onMyWay`+`imHere`) + 73 (quote/invoice/CO/chat) + 78 (`.job-actions` template) + ~30 (dialog refs/mounts) ≈ **270**. That number was the load-bearing argument for Option 3. Struck → **the recommendation inverts to Option 2**.
- **(c) There is no back-compat constraint.** `test_mobile_job_cards.test.js` mocks `api.get`, pinning the *component's read shape*, not the server's emission. Single tenant, one frontend shipped with the backend. v1 invented a constraint and paid a permanent dual-shape payload for it.
- **(d) The divergence trap was misdiagnosed.** Not ciphertext — both paths are ORM (`select(Customer)` at mobile.py:1811; `_job_card` takes ORM rows) so both decrypt via the mapper. The real gap: the sibling customer carries **`email`**; `_job_card`'s nested customer carries **`notes`+`tags` and no `email`**.
- **(e) Deferring clock in/out was a category error.** #154 forbids *inventing* hours. A tech tapping clock-in and clock-out **attests both ends** — that IS the attestation. The invented-hours bugs were a *sweep* with no human action, and a clamp that was itself the invention. Ship it; flag implausible spans to the office; never clamp, never block.

Also confirmed by [AR1]: `<Toast/>` IS mounted globally (`App.vue:3`) — v1's trap #4 was false. `_job_card` does **not** drag in drive-time/geocoding (applied later at mobile.py:1092-1105); it is sync and pure, so PR A is cheaper than v1 feared. `/api/mobile/jobs` (mobile.py:1262) is explicitly all-dates, so the "unreachable except via the read-only path" premise holds.
**Trigger:** Doug — *"The job workflow for a mobile tech lets you open a job but does not give any details about the job besides address and phone number. Michael can click on a job card but that is it… you should be able to clock in and out and add parts etc. A tech's workflow needs to be able to complete and send it to billing or be able to show the bill to the customer to collect payment."*

## The verdict being fixed

**The workflow is built. Techs cannot reach it.**

Live counts: **205 jobs / 184 completed**, but **4 arrivals, 4 closeouts, 0 photos, 0 parts logged from the field**. 319 invoices / 279 payments — the office does all of it on desktop.

Two unequal paths to a job:

| Path | Component | Actions |
|---|---|---|
| `/mobile` (Today) | `MobileTodayView.vue` (2054 lines) | **Everything** — inline in expandable cards |
| `/mobile/jobs` → `/mobile/jobs/:id` | `MobileJobDetailView.vue` (224 lines) | **None** — Back and Retry |

`MobileJobDetailView` was created 2026-07-16 (PR #153) purely so tapping a card did *something*. It is the path Michael takes. **Any job not scheduled today is reachable only through it** — viewable, never workable.

## Scope decisions (Doug, 2026-07-17)

- **Payment: cash/check only.** No Stripe/card. The existing `MobileInvoiceDialog` already does cash/check + offline queue.
- **Sequence: fix bugs first, then build.** Done — PR #154 (merged), PR #155 (open).
- **Parts→billing: already built** (PR #112). All three field paths tag `JobPartNeeded.source`. Not in scope.
- **Never block the tech in the field** (Doug 2026-05-10). Gates stay opt-in.

## Design principles

- **Do not duplicate the money path.** Closeout, invoice and payment already live in self-contained dialogs. They get reused, never copied.
- **One job shape.** Both surfaces consume the same card contract, or the shared code branches on which parent it's in — which is how drift starts.
- **Never invent data** — the standing rule from #154/#155. Applies here to timers and photo metadata.
- **The tech's surface must not regress.** Today is the *only* thing in use. Any extraction that risks it must be provably behaviour-preserving.

---

## PR A — Backend: one job shape (prerequisite, no UI)

*`/api/mobile/job/{id}` cannot support the actions today. This is not optional and must land first.*

`get_mobile_job_detail` (`routers/mobile.py:1789`) builds its job from `_get_job()` — a raw SELECT of `id, customer_id, title, description, dispatch_status, scheduled_at, completed_at, signature_data, signed_by, signed_at, created_at`. Today's cards come from `_job_card()` (`:730`).

**Missing, and each blocks an action:**

| Field | Blocks | Why |
|---|---|---|
| `job.customer` (nested) | toasts, equipment, change order | detail returns `customer` as a **sibling key**; `job.customer?.id` is undefined → equipment panel `if (!cid) return`, change order sends `customer_id: null` |
| `navigation_link` | Navigate | `:disabled="!job.navigation_link"` → permanently dead |
| `en_route_at`, `arrived_at` | any timestamp/timer display | **neither endpoint returns these on the job** — they exist only in `assignments[].*` on `/today` |
| `parts_summary` | parts row label | cosmetic |
| `service_type`, `priority`, `alerts` | alert tags | cosmetic |

**Change:** rewrite `get_mobile_job_detail` to build its card via `_job_card(job_obj, customer_obj, appointment, tags)` + the same `parts_summary`/`assignments` enrichment `/today` uses. Requires switching `_get_job`'s raw SELECT to an ORM `Job` fetch (`_job_card` takes ORM rows). [AR1] confirmed `_job_card` is sync and pure — drive-time/geocoding are applied later (mobile.py:1092-1105), so this does not drag in the `/today` pipeline.

**[AR1] ONE shape, not two.** v1 planned to keep the sibling `customer`/`notes`/`photos` AND add a nested `job.customer`, citing back-compat. **There is no back-compat constraint:** `test_mobile_job_cards.test.js` mocks `api.get` — it pins the *component's* read shape, not the server's emission. Single tenant, one frontend shipped with the backend, no third-party consumer. Emitting the customer twice in one payload is a permanent divergence trap for nothing: the sibling carries `email`; `_job_card`'s nested customer carries `notes`+`tags` and **no `email`**. Whichever the code reads, the other rots.

So: **nested `job.customer` only.** `notes`/`photos` stay siblings (they aren't on the card). Update `MobileJobDetailView` + its mock in the same commit. Ownership gate (`_assert_job_access`, 404-not-403) unchanged. If `email` is needed on the card, add it to `_job_card` so BOTH surfaces get it.

**Also unify `navigation_link`:** `_build_navigation_link` emits `https://maps.google.com/?q=`; `MobileJobDetailView` hand-builds `/maps/dir/?api=1&destination=`. Two formats. Pick the backend one; delete the client-side builder.

**Tests:** detail endpoint returns nested `job.customer` + `navigation_link` + `arrived_at`; sibling keys still present (back-compat); parity test asserting `/today` card and `/job/{id}` card have the same keys for the same job.

---

## PR B — Frontend: the actions reach the detail view

### The extraction question (the real decision)

Three options. **None is free.**

**Option 1 — `<MobileJobActions :job :layout>` shared component.** One component, both views.
- ✅ Zero drift; one place for the money path.
- ❌ Rewrites the *only* working tech surface. Today's actions are inline in a card row; Detail wants a sticky bottom bar → a `layout` prop that is really two markups in one file.
- ❌ **Breaks 7 of 9 assertions in two static-source specs** that `readFileSync` `MobileTodayView.vue` and regex it (`MobileTodayCloseoutWiring`, `MobileTodayInstallEquipment`). They must be re-pointed in the same PR. The `/complete`-stays-gone negative assertion still *passes* on a gutted MobileTodayView — **vacuously**. It must follow the code or the guard is lost silently.

**Option 2 — `useJobActions(job, {onUpdated})` composable.** Logic shared, markup per view.
- ✅ Each view keeps its natural markup (card row vs sticky bar).
- ✅ API contracts shared → no drift on payloads/queueing.
- ❌ Same static-source spec breakage (the strings move out of the view).
- ❌ Parts modal + quote dialogs are markup-heavy → still duplicated or still parent-owned.

**Option 3 — Detail composes the existing dialogs directly; Today untouched.**
- ✅ **Zero risk to Today.** No spec churn.
- ✅ The money path is *already* self-contained dialogs (`MobileJobCloseoutDialog`, `MobileInvoiceDialog`) — reused, not copied.
- ❌ en-route/arrived/parts handlers exist twice → drift on exactly the paths #154 just fixed.

**~~Recommendation: Option 3~~ — WITHDRAWN. [AR1] measured the duplication at ≈270 lines, not ~40.** That number was the whole argument, and it was a 6× understatement: `onMyWay`+`imHere` = 86, quote/invoice/CO/chat handlers = 73, `.job-actions` template = 78, dialog refs/mounts ≈ 30.

**Recommendation: Option 2 — `useJobActions(job, {onUpdated})` composable, now.**

Rationale, corrected:
- 270 duplicated lines is not "a few status handlers" — it *is* the workflow, and it sits on **exactly the en-route/arrived paths #154 just fixed**. Duplicating them means the next timer fix lands in one copy while the other rots — on the path Michael actually takes, with Today's green tests reporting all clear.
- [AR1]'s sharpest point: Option 3 was credited with "no spec churn" because the two static-source specs regex `MobileTodayView.vue` as text. **Choosing an architecture to keep a regex green is theater.** The plan itself admitted the `/complete`-stays-gone assertion would pass *vacuously* on a gutted view — i.e. the guard silently dies either way.
- Composable over component: each view keeps its natural markup (Today = card row, Detail = sticky bar) while the API contracts, payloads and offline semantics live in one place. The heavy markup (closeout, invoice, change-order, chat) is *already* self-contained dialogs — those get mounted by each view, not copied.

**Obligation:** re-point `MobileTodayCloseoutWiring.spec.js` and `MobileTodayInstallEquipment.spec.js` at the composable **in the same PR**, and make the `/complete`-stays-gone assertion non-vacuous by asserting it against the file that now owns the call.

### What the detail view gains

Sticky bottom action bar (≥44px targets — `e2e/mobile-touch-targets.spec.js` walks `/mobile/jobs` at 375×812):

| Action | Guard | Call | Notes |
|---|---|---|---|
| On my way | `assigned`/`unassigned`/falsy | `postQueued('/api/mobile/jobs/{id}/en-route', {})` | `actionType:'job.en_route'` |
| I'm here | `en_route` | `postQueued('/api/mobile/jobs/{id}/arrived', geo)` | geo best-effort, 3s timeout, `{}` on failure — never blocks |
| Complete | `on_site` | `MobileJobCloseoutDialog` (drop-in, props are strings) | parts + hours + signature + notes |
| Bill / collect | `done` or accepted quote | `MobileInvoiceDialog` (drop-in, `:job="{id}"`) | shows the bill; cash/check payment |
| Navigate | `!== 'done'` | `window.open(job.navigation_link)` | needs PR A |
| Add note | always | `POST /api/jobs/{id}/notes` | endpoint exists, no caller today |
| Add photo | always | `POST /api/mobile/jobs/{id}/photos` multipart | see below |
| Request part | always | `POST /api/jobs/{id}/parts-needed` | ⚠ not offline-queued today |

### Photo capture (backend fully built, zero UI)

`routers/mobile.py:2895`, one handler two routes. `multipart/form-data`, `file` (must be `image/*`), `kind ∈ {before,during,after}`. Setting `tech_mobile.photo_slot_tagging` (`optional` default | `required`): omitted+required → 400; omitted+optional → `during`.

⚠ **201 response has no `url`** — must re-`GET /api/mobile/job/{id}` to render the thumb.

**[AR1] Photos are offline-capable. v1 was wrong.** `postQueued` really can't carry FormData (`useOfflineSync.js:_drainOne` hardcodes `Content-Type: application/json` + `JSON.stringify(entry.body)`) — but that is a limit of *one function*, not of the offline layer. `lib/offlineDb.js:35` already ships the store this needs:

```js
// photos: id PK + job_id index; blob stored as Blob (not base64).
photos: 'id, job_id, status, created_at',
```

Header comment: *"photos — captured photo blobs awaiting upload… Photos use IndexedDB blob storage (no base64 overhead)."* **Zero writers, zero readers.** It was designed for exactly this and shipped empty.

A tech photographs a door in a garage or on a rural drive — the dead zones are *where the feature lives*. Shipping it online-only and printing an apology is not a design, and "0 photos across 205 jobs" is not going to be fixed by a feature that fails where it's used. **Write the Blob to `db.photos`, add a FormData branch to `_drainOne`, drain on reconnect** (the same `online`/`visibilitychange` hooks the JSON queue uses). Surface the pending count so the tech knows it's saved, not lost.

Setting read: `GET /api/me/tech-mobile-settings` → `settings['tech_mobile.photo_slot_tagging']`. **No composable exists** (`TimeclockView.vue:410` inlines it). Extract `useTechMobileSettings()` rather than write a third copy.

### Clock in/out — IN this PR [AR1 reversal]

v1 deferred it, arguing a manual timer would re-introduce invented hours. **[AR1]: category error, and it quietly failed to deliver what Doug asked for.**

The rule from #154 is that labor code may not **invent** hours. A tech tapping *clock in* and later *clock out* **attests both ends** — that IS the attestation. The bugs #154/#155 killed were a Celery sweep acting with no human involved, and a 12h clamp that was itself the fabrication. A human tap is the opposite of those.

So: ship it, with the #154 rule intact.

- `POST /api/mobile/jobs/{id}/clock-in` / `clock-out` (exist, zero callers today — so their behaviour is *unproven*, not merely unused; test them for real).
- **Fix `_close_open_time_entry` (mobile.py:497) first**: it closes with unclamped elapsed and sets no `hourly_rate` — the same shape #154 fixed in closeout. A tap-attested span is payable; it still must not be clamped, invented, or silently mis-costed.
- **Never clamp. Never block the tech** (Doug 2026-05-10). An implausible span is real data the tech attested — it goes to the **office exceptions surface** (PR #155's card), which is exactly Doug's rule: *"it should be the dispatcher or office personel that get told about the discrepency."*
- Show the running timer. Today the arrival auto-clock-in is invisible; the tech has no idea a clock is running on them.

### Explicitly NOT in this PR

- Card payment (Doug 2026-07-17: cash/check only).
- Day-vs-job variance report — still meaningless until per-job time is actually captured. Revisit after this ships.

---

## Known traps (from the extraction map)

1. **Optimistic flip is never rolled back.** `job.dispatch_status='en_route'` is set *before* checking the result and not reverted on throw — the card shows en-route while an error toast fires. Do not copy this bug into the new surface.
2. **Actions mutate the `job` prop object.** Works in Today (array item); Detail's `job` is a standalone ref → behaviour diverges. Decide: mutate + document, or `emit('updated')`.
3. **`tests/test_mobile_job_cards.test.js` mocks `api.get` with `mockResolvedValueOnce` — exactly once.** Any extra mount-time GET (quotes, parts, settings) resolves `undefined` → throws. Lazy-load on demand.
4. **No `<Toast/>` in `MobileJobDetailView`, no `useToast` import.** Verify the `/mobile` shell mounts one or every toast silently no-ops.
5. **Parts create/edit is not offline-queued** while en-route/arrived/closeout/payment are. In a dead zone the tech's part request just errors.
6. **Offline banner is Today-only** — detail will queue silently with no pending indicator.
7. **`MobileInvoiceDialog`'s header is "Close out"** — same wording as the closeout trigger. Already ambiguous; adjacent in a detail view it is worse. Rename the trigger to "Bill / collect".
8. **Testids to preserve:** `mtv-change-order-btn`, `parts-summary-{id}`, `equipment-summary-{id}`, `equipment-panel-{id}`, all `mjco-*`/`mid-*`.

## Verification

- Backend: pytest — endpoint parity, back-compat keys, photo `kind` matrix.
- Frontend: vitest — action guards by `dispatch_status`, offline-queue path, photo online-only messaging, re-fetch after upload.
- e2e: touch targets ≥44px at 375×812 on `/mobile/jobs/:id`.
- **Headed browser walk on a real device/emulator** as Michael — per Doug's standing preference. The prior mobile fix (#153) was verified by API + live data, never by driving the actual phone; that walk is still owed.

# Frontend ↔ backend contract gaps — 2026-08-12

First run of `gdx_dispatch/tools/frontend_contract_scan.py`, checked against the
**live** route table (1,444 routes) rather than static parsing.

**60 findings: 1 × C1, 7 × C2, 14 × C3, 12 × C5, 26 × C6.** Nothing is fixed yet.

C6 did not exist when this scan was written. It came out of *verifying* C2:
chasing the JobCosting parts mismatch led to a handler that answers `{"ok":
true}` without touching the database, and 26 more like it. **That class is the
most severe thing in this document** — see below.

## Why this seam had no coverage

The other ten scanners in `tools/` all check backend↔backend or backend↔DB.
Nothing compared the browser to the API. There are ~15 hand-written contract
tests (`test_jobs_create_payload_contract.py`, `test_mobile_job_create_contract.py`,
…) but each pins **one** endpoint and each was written *after* a bug.

A manual backend↔Vue sweep on 2026-07-24 found 9 dead delete buttons and several
unreachable pages; those are still unfixed. A one-off sweep finds real bugs and
does not stick — which is the argument for a scanner.

## C2 — method mismatches (7): VERIFIED by real requests

Not inferred from the route table — each verb was actually issued against the
built app via `TestClient`, alongside a control verb the backend does
advertise. Routing rejects the frontend's verb before any auth or handler
runs, so the control returning 401 while the frontend's verb returns 405 is
proof the mismatch is real and not an auth artefact.

```
finding                          FE verb  status  control  status  verdict
EquipmentView save-edit          PATCH    405     PUT      401     CONFIRMED
CustomerDetail remove-portal     DELETE   405     GET      401     CONFIRMED
JobCosting delete-part           DELETE   405     PATCH    401     CONFIRMED
JobCosting save-costing          PATCH    405     GET      401     CONFIRMED
SegmentsView save-edit           PATCH    405     GET      401     CONFIRMED
ServiceAgreements del-template   DELETE   405     PATCH    401     CONFIRMED
JobCosting load-parts            GET      404     POST     422     404, not 405
```

**6 of 7 confirmed as 405.** The seventh behaves differently and the original
report was imprecise: `GET /api/jobs/{id}/parts` returns **404**, because only
`POST` was ever built for that path and this app's custom route wrapper drops
the partial-match signal Starlette would use to answer 405. The user-visible
effect is the same — the parts list never loads — but the mechanism is "the
read endpoint was never written", not "wrong verb". Note `GET
/api/jobs/{job_id}/parts-needed` does exist and may be the intended path.

All seven call sites are real user actions, not dead code: edit-save handlers,
delete buttons, one behind a confirm dialog.

The path exists, the verb doesn't, so the request 405s. Invisible by eye
because the URL looks right.

| Frontend | Sends | Backend serves |
|---|---|---|
| `EquipmentView.vue:299` | `PATCH /api/equipment/{id}` | `PUT` (+GET/DELETE) |
| `CustomerDetailView.vue:1022` | `DELETE /api/customers/{id}/portal-account` | `GET`, `POST` |
| `JobCostingView.vue:975` | `GET /api/jobs/{id}/parts` | `POST` |
| `JobCostingView.vue:1029` | `DELETE /api/jobs/{id}/parts/{part_id}` | `PATCH` |
| `JobCostingView.vue:1105` | `PATCH /api/jobs/{id}/costing` | `GET` |
| `SegmentsView.vue:763` | `PATCH /api/segments/{id}` | `GET`, `DELETE` |
| `ServiceAgreementsView.vue:533` | `DELETE /api/service-agreements/templates/{id}` | `PATCH` |

**The equipment one has a traceable root cause and is the template for the rest.**
`equipment_tracking_router` was deliberately unwired on 2026-05-03 in favour of
the canonical `modules/equipment/router.py` (see the note at `app.py:1632`). The
old router served `PATCH /api/equipment/{id}`; the canonical one serves `PUT`
(`modules/equipment/router.py:356`). The backend consolidation was completed and
documented — the frontend was never updated. So **saving an equipment edit has
been silently failing since 2026-05-03.**

Note how many of the others resolve to `ui_compat.py`, the compatibility shim.
That layer is where FE and BE disagree most.

## C6 — fake success (26, of which 19 are UI-reachable): the worst class

A mutating handler whose entire body is a `return` of a constant cannot have
changed anything — yet it answers 200 with a success sentinel. The Vue pops
"Saved", closes the dialog, reloads, and shows the unchanged value.

This is strictly worse than a 405. A 405 errors and someone eventually
notices. This *confirms* the write landed.

**Proven end-to-end:** [`PricingView.vue:301`](../../gdx_dispatch/frontend/src/views/PricingView.vue)
sends `PATCH /api/pricing/{id}` with `{ successMessage: 'Entry updated' }`.
The live winner for that route is `ui_compat.update_pricing_entry`, whose
whole body is `return _ok()` → `{"ok": True}`. The user sees **"Entry
updated"**, then `loadPricing()` re-renders the old price. Editing a price
silently does nothing while claiming success.

19 are reachable from the UI today. The ones that look most expensive:

| Route | Frontend | Consequence |
|---|---|---|
| `POST/PATCH /api/pricing[/{id}]` | `PricingView:301,303` | price create + edit both no-op |
| `POST /api/payroll/run-current-period` | `PayrollView:202` | "run payroll" does nothing |
| `POST /api/communications/bulk-sms` | `SegmentsView:689` | bulk SMS reports sent, sends nothing |
| `POST /api/customers/{id}/recurring-jobs` | `CustomerDetailView:911` | recurring job never created |
| `POST /api/customers/{id}/portal-account` | `CustomerDetailView:1002` | portal account never created |
| `POST/PATCH /api/scheduling[/{id}]` | `SchedulingView:484,486` | scheduling edits discarded |
| `PATCH /api/sso`, `POST /api/sso/test-connection` | `SsoView:222,232` | SSO config discarded; "test" always passes |
| `PATCH /api/booking/{slot_id}` | `BookingView:273` | booking edits discarded |
| `POST/PATCH /api/equipment-tracking[/{id}]` | `EquipmentTrackingView:230,232` | equipment-tracking writes discarded |
| `PATCH /api/jobs/{id}/parts/{part_id}` | `JobCostingView:1016` | part edits discarded |
| `POST /api/reviews/{id}/responses` | `ReviewsView:322` | review replies discarded |
| `POST /api/jobs/{id}/apply-template` | `JobStateOverrideDialog:171` | template never applied |
| `PATCH /api/service-agreements/templates/{id}` | `ServiceAgreementsView:512` | template edits discarded |

25 of the 26 live in `routers/ui_compat.py`.

Two refinements were needed before these numbers could be trusted, and both
materially changed the result:

1. **Shadowing.** A no-op that loses route-order arbitration to a real handler
   is dead code, not a bug. Filtering on the endpoint that actually wins
   dropped C6 from 45 → 35 and C5 from 19 → 12. (`campaigns/activate` and
   `onboarding/checklist` have real handlers registered first.)
2. **Thin controllers.** `return add_proposal_tier(estimate_id, ...)` is a
   single-return body that delegates real work — the ordinary controller
   pattern, not a no-op. Excluding calls-with-arguments dropped C6 35 → 26 and
   removed every false hit in `modules/proposals/`. This one nearly produced a
   badly wrong report: proposal tiers demonstrably work.

The remaining 7 non-UI-reachable hits (`/api/estimate/save`, `/api/uploads`,
`/api/billing/cancel`, `/api/ai/*`, custom-fields PUTs, …) have no frontend
caller. Vestigial routes — lower priority, but they are live URLs that answer
success to anything that finds them.

## C5 — stub endpoints (12): pages wired to permanent blanks

Handlers whose entire body is `return {...empty...}`. The request succeeds, the
page renders, the user sees an empty screen forever, and nothing is logged. The
check is strict — a `delete_*` that does real work and returns `{}` is not
counted.

Highest-impact:

| Endpoint | Effect |
|---|---|
| `GET /api/dispatch/optimize-route` | route optimisation returns no stops — the feature does nothing |
| `GET /api/loyalty` | Loyalty page always empty |
| `GET /api/maps` | Maps page always empty |
| `GET /api/sso` | SSO config page always empty |
| `GET /api/technicians/skills` | skills always empty |
| `GET /api/users/staff` | staff list always empty |
| `GET /api/onboarding/checklist` | onboarding checklist always empty |
| `GET /api/billing/invoices`, `/billing/payment-methods`, `/billing/usage` | billing sub-pages always empty |
| `GET /api/ai/quality/summary`, `/ai/quality/recent` | AI quality always empty |
| `GET /api/customers/{id}/portal-account` | portal-account status always empty |
| `GET /api/quickbooks`, `/api/admin/permissions`, `/api/campaigns/{id}/preview`, `POST /api/campaigns/preview-filter`, `GET /api/role-permissions/migration-banner`, `POST .../ack` | as above |

15 of the 19 live in `routers/ui_compat.py`. That file is doing more load-bearing
work than its name suggests — it is the backing implementation for several whole
pages, not a compatibility shim.

Worth deciding per endpoint: build it, or remove the page. A stub is worse than
a missing page, because it looks like "no data yet" rather than "not built".

## C3 — phantom response fields (14): leads, not verdicts

The Vue reads a field the handler cannot return. Precision is deliberately
imperfect here: defensive fallback chains produce false positives. `LoyaltyView`
reads `data.members ?? data ?? data.items` — the `data.items` arm is a tolerated
alternative shape, not a bug.

That said, C3 is what surfaced the C5 class: most of these hits are views reading
fields off stub endpoints (`DispatchView` ← `optimize-route`, `MapsView` ←
`/api/maps`, `SsoView` ← `/api/sso`, `LoyaltyView` ← `/api/loyalty`). Treat C3
as a lead generator and read each hit.

Genuinely suspicious, not yet verified:

- `JobDetailView.vue:2364` reads `templates.length` from `GET /api/job-templates`, which returns `{"items": [...]}` — a bare array read against an envelope.
- `PerformanceView.vue:121` reads `r.items` from `/api/performance/users`, which returns `{"period", "users"}`.
- `EstimateView.vue:1597` reads `result.line_items` from `POST /api/ai/estimates/suggest`, which returns `suggested_lines`.

## C1 — dead calls (1)

`usePluginScreen.js:96` calls `GET /api/plugins/${pluginKey}/ui`. Plugin routes
mount at runtime per-plugin, so this is expected to be absent from a
statically-dumped table — **likely a false positive**, but worth one look since
plugin routes were the subject of a recent authz fix.

## Running it

```
# ground truth (preferred)
docker run --rm --entrypoint python -e PYTHONPATH=/app \
  -e JWT_SECRET=<32+ bytes> -v $PWD:/app -w /app docker-app \
  gdx_dispatch/tools/frontend_contract_scan.py --dump-routes /app/routes.json
python3 gdx_dispatch/tools/frontend_contract_scan.py --routes routes.json

# no docker — static parse, 99.3% accurate (misses runtime-mounted /api/plugins)
python3 gdx_dispatch/tools/frontend_contract_scan.py
```

19 contract tests in `tests/test_frontend_contract_scan.py` pin both directions:
the real detections, and the false-positive classes found on the first live run
(query-string suffixes, computed path segments, scaffolding templates, comment
examples, and per-function variable scoping).

## Suggested order

1. **C6 first.** These report success while discarding the write — pricing
   edits, payroll runs and bulk SMS among them. A user cannot tell anything is
   wrong, so this is the only class actively producing bad data and false
   confidence right now. 25 of 26 are in one file.
2. **C2** — 7 confirmed broken buttons. Small fixes, and unlike C6 they at
   least fail loudly. The equipment one is a one-line verb change.
3. **C5** — decide build-or-delete per stub; `optimize-route` first, since a
   feature that silently no-ops is worse than one that is absent.
4. **C3** — verify the three suspicious ones above.

## What `ui_compat.py` actually is

25 of 26 C6 hits, 15 of 19 original C5 hits, and several C2 mismatches resolve
to `routers/ui_compat.py`. The name reads like a thin compatibility shim. It is
not: it is the live, winning implementation behind Pricing edits, Payroll runs,
bulk SMS, SSO config, Scheduling, Booking, Reviews and more — and for those
routes the implementation is `return {"ok": True}`.

Whatever is decided per-endpoint, that file is the single highest-leverage
place to look, and its docstring should say what it really is.

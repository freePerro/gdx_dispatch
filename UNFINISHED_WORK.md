# Unfinished Work — GDX Dispatch (v2)

**Compiled 2026-08-23** against `origin/main @ eb5559f`, prod + demo on **v1.75.0**.
Supersedes the 2026-07-19 edition, which had gone stale in almost every section.

Two rules this file tries to obey, because the last one broke both:

1. **Every claim names how it was checked** — a PR number, a query against prod, a
   file and line. "Verified" without a receipt is not verified.
2. **Rows are dated.** Anything not re-checked on 2026-08-23 says so, and should be
   re-checked before you act on it.

---

## ✅ Closed since the last edition

The whole money-reporting cluster, released as **v1.75.0** on 2026-08-23, deployed to
prod and demo and walked on prod in light and dark.

| PR | What it closed |
| --- | --- |
| #399 | **M8** — four revenue surfaces summed `invoices.total_amount`, NULL on all 349 prod rows. Charted $0 against $829,164.66 of billed work. Two further bugs hid behind it: the frontend read fields the API never emitted, and periods grouped on `created_at` (the import run — a $607,419.52 spike on one day). |
| #400 | **M35** — `amount_paid` had no runtime writer and had drifted $62,473.72 across 24 invoices. Seven readers moved to `core/invoice_paid.py`. The office invoice detail now carries paid-to-date, so MobileBillingView's "Paid" row renders for the first time. |
| #401 | The appointment-reminder stub (three no-ops on an hourly beat, logging success forever) deleted; two SMS paths stopped recording deliveries they never made. |
| #402 | **M19 / M20 / half of M18** — credit memos reduce revenue; AR aging is a backlog that agrees with cash-risk (both 19 invoices / $19,337.91, previously $16,324.21 apart); sales tax is "collected" only against real cash. |
| #403 | **Migration 073** dropped `invoices.total_amount`, `invoices.amount_paid`, `jobs.dispatched_at`. |
| #404, #405 | Corpus corrections + released-status lines. |

`backend-vue-contract-gaps-2026-07-24` is now **FIXED** end to end.

---

## 🔴 Needs a decision from Doug — not build work

- [ ] **M18's other half — how does a credit memo split into tax and non-tax?**
      `invoice_adjustments` carries a flat `amount` with no tax component, so a credit
      cannot reduce the tax it originally charged. Prod exposure: **4 credited invoices
      carrying $570.79 of tax against $797.45 of credits.** Options are pro-rata at the
      invoice's rate, operator-entered, or "credits never reduce tax". Needs a migration
      too. Deliberately not guessed. _(measured on prod 2026-08-23)_
- [ ] **Should "On My Way" text the customer?** The button techs actually press
      (`MobileJobDetailView` → `POST /api/mobile/jobs/{id}/en-route`) sends **no SMS**.
      It does record time tracking — `job_assignments.en_route_at`, 21 rows, 20
      `arrived_at`, latest 2026-08-21 — so it is load-bearing and stays. Whether
      pressing it should notify the customer via Phone.com is customer-facing product
      shape. _(verified on prod 2026-08-23)_
- [x] **D6 auto-email on/off** — `outlook/automations.py:73 dispatch_trigger` has zero **[DECIDED 2026-08-31: deleted — `dispatch_trigger`, its tests and the Outlook Auto-Email tab are removed; Event Rules (`modules/workflows`) is the event-email path. See docs/design/unimplemented-endpoints-decision-list.md § 2026-08-31.]**
      production callers. Revive or delete. _(carried from 2026-08-20 read)_
- [ ] **17 unimplemented endpoints** — `unimplemented-endpoints-decision-list` still
      awaits a build/remove/leave call on each. _(carried)_

---

## 🟠 Real work, verified open

- [ ] **PG test fixture has drifted badly from the ORM.** `tests/fixtures/structure.sql`
      is missing **90 tables and 128 columns**, so every `requires_pg` test runs against
      a schema production does not have. #403 patched only the three money tables and
      added a guard scoped to them, which says so in its own docstring. The
      `refresh_test_schema.sh` that the fixture's docstring tells you to regenerate with
      **does not exist**. _(measured 2026-08-23)_
- [ ] **`--network host` fails 15 tests that pass without it.** Needed to run the
      `requires_pg` tests; the container then reaches the live local Redis/Postgres.
      Reproduces on pristine main — 70 passed without it, 15 failed with it, same code.
      Run the matrix without it, and PG tests separately. _(measured 2026-08-23)_
- [ ] **Two MCP tools were reporting $0 and nothing caught it.** Fixed in #403, but the
      reason they hid is unfixed: `test_tool_revenue_summary.py` passes a **mock db**, so
      the SQL string never touches an engine and stays green whatever it says. Same
      pattern elsewhere is worth a sweep. _(2026-08-23)_
- [ ] **The demo seeder is gitignored.** `gdx_dispatch/docker/demo/` is excluded from
      git by design, so its fix for the dropped columns shipped by `rsync`, not by the
      release. It is correct on the VPS as of 2026-08-23 (verified — the post-release
      re-seed succeeded), but any future schema change has to remember it.
- [ ] **`closeout-parts-autopricing-plan`** — all four named items still unbuilt: no
      price provenance column, no server-side unbilled-parts gate on `verify_invoice`,
      void doesn't release claims, autodraft release still deletes office lines. _(carried)_
- [ ] **`design-doc-corpus-audit-2026-08-18` §4 live defects** — still live:
      `payments.py:48/54` caller-supplied currency, the money probe behind a marker
      `pytest.ini` excludes, the idempotency middleware inert, mobile clock endpoints
      orphaned. _(carried)_
- [ ] **PO receive route collision** — 3 systems, `po_workflow.py` shadowed/dead on the
      same prefix, `modules/purchase_orders/` unmounted. _(carried from July; not re-checked)_

---

## 🟡 Built but parked

- [ ] **GL** — live on prod since the July cutover, trial balance nets to zero. §12's six
      CPA questions unanswered, and **§11 step 4's gate is unsatisfiable**: it wants a
      monthly hand-check against a QuickBooks we can no longer reach. _(carried)_
- [ ] **`gl-phase2` QBO half** — ⛔ won't build; retired by the QuickBooks phase-out.
- [ ] **n8n Sprint 2b** — no `/internal/schedule/{key}/{name}` route, no schedules beat
      driver, no consent UI, no drift banner. _(carried)_
- [ ] **Bank statement import** — 3 slices on main, never exercised on prod:
      `bank_matches` has 0 rows, so nobody has run a reconcile. _(carried)_
- [ ] **Plugin storefront S3** — catalog signature with a pinned key absent; §5 labels it
      "V2 / phase later", so this may be an intended deferral. _(carried)_
- [ ] **Midland plugin / CHI pricing plugin** — PRIVATE and git-ignored on purpose.
      Never commit, push or merge them.

---

## ⚙️ Housekeeping

- [ ] **A stray `stash@{0}` on this machine.** An aborted rebase during the v1.75.0 work
      left an autostash holding a _stale reversion_ of `LineItemEditor.vue` and
      `EstimateView.vue` (230 deletions of catalog-picker work already on main). The
      files themselves were restored and match main exactly. The stash is junk but was
      left in place rather than dropped — deleting stashes is the owner's call.
      `stash@{1}` and `stash@{2}` predate this session.
- [ ] **~18 untracked screenshots in the repo root** (`eventlog-*.png`, `n8n-*.png`,
      `prod-*.png`, …) from earlier sessions. Not mine to delete.
- [ ] **13 of the 59 design docs are untracked** — written but never committed, so they
      exist only on this machine. The completion ledger classifies all 59, which means 13 of
      its rows describe plans nobody else can read. Committing them is what makes that audit
      reproducible. _(counted 2026-08-23: 46 tracked, 13 untracked)_

---

## Not in this file

Anything the [Design Doc Completion Ledger](https://claude.ai/code/artifact/6294e8e2-c7ab-4f8f-9b6f-cf9480c9dd5b)
already tracks row by row — **24 completed, 23 not completed, 10 not started, 2 superseded**,
which is all 59 files in `docs/design/`. This file is the short list of what needs a person;
the ledger is the exhaustive one.

One caveat that applies to both: of those 59 docs, **46 are on `origin/main` and 13 are
untracked** — local to one machine. A ledger row for an untracked doc describes a plan that
exists nowhere else, so committing them is what makes this audit reproducible by anyone but
the person holding the laptop.

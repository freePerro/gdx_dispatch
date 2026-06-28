# Forecasting Accuracy Roadmap

**Status:** Experimental feature (grouped under the "Experimental" nav category).
**Owner:** Doug
**Created:** 2026-06-28

## Why this doc exists

The Forecasting module ([modules/forecasting/](../gdx_dispatch/modules/forecasting/))
currently produces a single-number revenue projection from an
**aging-bucket × static-rate** model. That is the industry baseline method —
and the one every cash-forecasting practitioner names as the *least* accurate
(~60–70% accuracy at a 30-day horizon, vs. 85–95% for per-entity learned
models). This doc lays out a staged path to better accuracy. Stage A (the
measurement loop) is implemented alongside it.

## Current model (as-built)

`revenue_projection()` in [service.py](../gdx_dispatch/modules/forecasting/service.py)
sums three components:

| Component | How it's computed | Weakness |
|---|---|---|
| **Open AR** | Each open invoice's `balance_due` × a static per-bucket **lifetime** collection rate (`collect_rate_0_30` … `_90_plus`, defaults 95/80/60/30%). Not bounded to the window. | Rates are global guesses, not learned from the tenant's own history. Lifetime, not window-specific. Ignores per-customer pay behavior. |
| **Scheduled jobs** | Latest estimate total per job × one global `scheduled_realization_rate` (default 70%). | One knob for all lifecycle stages and job sizes. |
| **Recurring** | Deterministic schedule walk; midpoint of observed min/max. | Most reliable input. |

Output is a single point estimate; no confidence range.

## Stage A — Measurement loop  ✅ implemented

### The trap we had to avoid (audit history)

A first cut of this loop computed a single MAPE by comparing
`predicted_ar_collection` (open balance × **lifetime** rate, no time bound) to
`actual_ar_collected` (payments within `[as_of, horizon_end]`, a 30-day slice).
An adversarial audit correctly rejected it: those are two different physical
quantities (lifetime expectation vs. one window's cash), so the "MAPE" measured
horizon mismatch, not forecast error. Worst case is the 90+ bucket — an invoice
predicted at "30% will *eventually* pay" scored on "did 30% arrive in 30 days"
shows catastrophic false over-forecast even when the lifetime rate is exactly
right. The first design's one test used a 0-30 invoice (the only bucket where
lifetime ≈ window), hiding the defect.

### What this loop actually measures

Per **aging bucket**, the fraction of snapshotted AR that is actually collected
**within the window** — the empirical *within-window* collection rate. This is
dimensionally coherent (collected-in-window ÷ face, both windowed) and is
exactly the quantity Stage B needs to replace the lifetime defaults with
window-calibrated rates.

- **Models:** `ForecastSnapshot` (header: `as_of`, `window_days`, `horizon_end`,
  `assumed_rates`, `bucket_results`) + `ForecastSnapshotInvoice` child (one row
  per open invoice with its bucket and **frozen** `face_at_snapshot`). Child
  rows, not a JSON id blob, so thousands of open invoices don't balloon a column.
- **Service:** [accuracy.py](../gdx_dispatch/modules/forecasting/accuracy.py) —
  `capture_snapshot`, `reconcile_due_snapshots`, `accuracy_summary`.
- **API:** `POST /api/forecast/snapshots`, `POST /api/forecast/snapshots/reconcile`,
  `GET /api/forecast/accuracy` (per-bucket calibration table), `GET /api/forecast/snapshots`.

### Reconciliation rules (and the audit findings they address)

- **Population is frozen at `as_of`** via child rows; payments are attributed
  only to invoices that were open then (payments on later invoices don't count).
- **Collection is capped per-invoice at `face_at_snapshot`** — an overpayment or
  a payment against a since-grown balance can't push realization above 100%.
- **Window is `[as_of, horizon_end]` inclusive**; payments outside don't count.
- **No single headline MAPE, and no gap subtraction.** The output is the
  per-bucket `observed_window_rate` (the deliverable), with `assumed_lifetime_rate`
  reported alongside *for reference only*. The two are deliberately not
  differenced: they're different horizons (eventual vs. this window), so a "gap"
  would be the same apples-to-oranges metric the first design was rejected for.
  A large divergence on aged buckets is EXPECTED (a "30% will eventually pay"
  invoice rarely pays 30% inside one 30-day window) — it is signal for Stage B,
  not model error. The field is named `assumed_lifetime_rate` precisely so a
  dashboard can't naively subtract it and render "model is X% wrong."
- **Collection is clamped to `[0, face]`** per invoice: the cap stops overpayment
  inflating the rate above 100%; the floor stops a refund/credit-memo (net
  negative window) producing a negative rate that would poison Stage B.

### Operationalize  ✅ wired

A daily Celery beat task `forecasting-measurement-tick-daily` (05:00 UTC) fires
`advance_forecast_measurement_dispatcher` → `advance_forecast_measurement_task`,
which captures today's snapshot and reconciles any matured ones per tenant
(`modules/forecasting/tasks.py`). The admin endpoints remain for manual
drive/inspection. (`forecasting.tasks` is now registered in the celery `include`
list + explicit import — previously it was only registered transitively.)

`ForecastSnapshot`/`ForecastSnapshotInvoice` are TenantBase, built by
`create_orm_tables()` at startup before alembic (the #41 pattern) — no migration
needed **for this initial table creation** (verified against real Postgres on a
fresh DB).

> ⚠️ **Migration caveat (plugin-table-drift twin of #41):** `create_all` is
> `checkfirst` — it creates *missing* tables but does NOT alter existing ones.
> So this works on first ship, but **any future column added to these tables
> (e.g. in Stage B/C) WILL need a hand-written `ALTER … ADD COLUMN IF NOT
> EXISTS`** (or a migration). Without it, fresh-SQLite tests pass while every
> existing tenant 500s with `UndefinedColumn`. Don't add a column to
> `forecast_snapshots*` without the ALTER.

**Exit criterion to drop "Experimental":** enough reconciled snapshots that the
per-bucket observed window-rates are stable, Stage B consumes them, and the
resulting window-bounded forecast hits a target error on real data.

## Later stages

### Stage B — Calibrate rates from the tenant's own history  ✅ implemented

`calibration.py` aggregates reconciled snapshots into a per-bucket **window**
collection rate (face-weighted observed rate) and the AR projection
(`_open_ar_projection`) uses it in place of the configured prior, **per bucket**,
once a cold-start threshold is met (`CALIBRATION_MIN_SNAPSHOTS`, default 3
reconciled snapshots *at the matching window*). Until then a bucket falls back
to its configured rate, so behaviour is unchanged with no data and self-tunes as
snapshots accrue. Each bucket's `rate_source` (`calibrated`|`configured`) is
surfaced in the forecast output, and `GET /api/forecast/calibration` shows the
calibrated rate next to the prior. Computed live from snapshots — no persisted
calibration state (nothing to drift), and **no new table** (the configured rates
in `ForecastSettings` are untouched, so no ALTER per the caveat above).

Window matching is enforced: a rate measured over a 30-day window only
calibrates a 30-day forecast; mismatched windows fall back to the prior.

**Storage + staleness:** calibration only reads reconciled snapshots whose
`as_of` is within `CALIBRATION_LOOKBACK_DAYS` (180), and the daily task prunes
reconciled snapshots older than that (`accuracy.prune_reconciled_snapshots`) so
neither snapshot table grows without bound and an old collection regime can't
dominate forever.

**Known statistical limitations (honest, not hidden):**
- Daily snapshots over the same open AR are *correlated*, so `sample_size`
  counts closed windows, not independent samples — the effective N is lower
  than the count. `CALIBRATION_MIN_SNAPSHOTS` gates on "enough closed windows,"
  not statistical power.
- A long-unpaid invoice recurs across many daily snapshots, all showing "not
  collected," so persistent non-payers are over-weighted and the face-weighted
  rate is biased *down*. A **cohort** rate (fraction of invoices *entering* a
  bucket that pay in-window) removes this and is the right Stage C refinement.
These are acceptable for a self-correcting prior that's clearly labelled
`rate_source` per bucket; they are not acceptable to hide.

Semantics note (audit-aware): calibrated buckets make the AR component a genuine
*within-window* expectation; uncalibrated buckets use the configured rate as a
*prior*. The output's per-bucket `rate_source` makes the mix explicit. The
configured rate and calibrated rate are reported side by side but never
differenced.

### Stage C — Per-customer / per-stage granularity
- **AR:** per-customer average days-to-pay (no ML); cohort fallback for new
  customers until ~6–12 months of history exists.
- **Scheduled jobs:** per-lifecycle-stage win probabilities from historical
  conversion, replacing the single realization knob.

### Stage D — Probabilistic output (confidence range)
Replace the single `expected_total` with **P10 / P50 / P90** lines (Monte-Carlo
over per-invoice payment probabilities or per-bucket variance quantiles).

### Out of scope (for now)
ML (XGBoost / LSTM / TFT) is overkill at our volume and needs 2+ years of clean
history. Stages B–C capture ~80% of the gain and are prerequisites for ML anyway.

## Sequence

A (measure) → B (window-calibrated rates) → C (granularity) → D (confidence range).
Each is independently shippable and validated against A.

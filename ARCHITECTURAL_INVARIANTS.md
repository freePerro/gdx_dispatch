# Architectural Invariants Registry

> The architecture is the set of running scans. Everything else is aspirational.

This file is the single source of truth for **load-bearing invariants** in the application — assumptions whose violation produces a class of compounding bugs. Conventions you can flex live in `BUILD_RULES.md`. Laws live here.

## The policy

1. **Every retro that surfaces a class-of-bug must add or update a row.** New invariants enter as `documented-only`.
2. **Every sprint should convert at least one `documented-only` row to `enforced`** — i.e., write a scan and gate CI on it. 
3. **`unknown` is a debt admission**.
4. **A scan exists or it doesn't.** "Soft enforcement via code review" counts as `documented-only`.
5. **When a scan is added, file the existing-violation count as a baseline.**

## The registry

| # | Invariant | Bug class it prevents | Detection | Status |
|---|---|---|---|---|
| 1 | **Audit logging on every mutation** — `log_audit_event()` on every create/update/delete in routers/services. | Untraceable changes; SOC2 evidence gap. | Need: AST scan of router functions decorated `@router.(post\|patch\|put\|delete)`. | **documented-only** |
| 2 | **Soft-delete, not hard-delete** — set `deleted_at`; never `DELETE` from tables that carry the column. Queries filter `deleted_at IS NULL`. | Lost referential integrity in audit/billing chains. | Need: grep for raw `DELETE FROM <soft-deletable>` in services/routers. | **documented-only** |
| 3 | **HTTPException shape contract** — every error path raises `HTTPException(status_code=N, detail="…")`; never bare `raise Exception`. | Generic 500s leaking stack traces. | **Claimed "partially handled in semgrep rules" until 2026-09-07 — no semgrep rule file, CI step or dependency exists in this repo; only a `.semgrepignore`. Need: write the rules and gate them, or drop the claim.** | **documented-only** |
| 4 | **Route-table snapshot freshness** — `gdx_dispatch/openapi_routes.txt` (one `METHOD path` per line, generated) equals the route table the app publishes; every router `app.py` names is importable; the published document is identical across processes. | A route added or removed shows as a one-line reviewable diff in the PR; a router whose import silently fails (app.py falls back to an empty router) is caught as missing routes instead of shipping; a per-process value in the schema (the 2026-08-31 baked-in random secret) cannot come back. | `tests/test_openapi_snapshot_current.py` in the default suite; refresh with `python -m gdx_dispatch.tools.openapi_snapshot --write`. Rewritten 2026-08-31: the old row named `frontend/types/api.d.ts` (generated, **nothing imported it** — deleted with its generator), a 2.8 MB `openapi.json` nothing read (deleted; `/openapi.json` is served live), and a gate script that never existed. | **enforced** |
| 5 | **Test pollution containment** — no test rows in production with the markers. | Test data ages into prod and pollutes metrics. | Named `tools/pollution_check.py` cron until 2026-09-01. **No file of that name exists in the repo**, and nothing in `core/scheduler.py` or `.github/workflows/` runs it. Need: write the check, or drop the claim. | **documented-only** |
| 6 | **Schema = ORM** — every column matches the ORM definition. | "It works in dev but the prod column type is different" surprises. | Four scanners exist as CLI tools (`tools/drift_scanner.py`, `tenant_plane_schema_drift.py`, `tenant_schema_drift_check.py`, `comment_drift_scan.py`) and `drift_scanner` has unit tests. But **no beat entry, workflow or systemd unit in this repo runs any of them nightly** — the "nightly cron" this row claimed is not visible in the tree. A cron may exist on the VPS; the repo cannot show it, which is the point. Need: schedule one here, or cite the unit that runs it. | **documented-only** |
| 7 | **Refresh-token rotation = revoke-on-first-reuse** (RFC 9700). On detected reuse outside the 30s leeway window, the *entire refresh family* for the user is revoked. | Stolen refresh tokens silently working alongside the legitimate user. | `routers/auth.py refresh()` enforces | **enforced** |
| 8 | **Money postings name the acting user** — every GL journal entry for an `invoice` or `payment` source carries a non-NULL `created_by`: a user id, or a named system identity (`stripe-webhook`, `stripe-confirm`, `stripe-recovered`, `portal-charge-method`). `_recalculate_invoice` takes an `actor` and passes it to the repost and the auto-flip. **Rows posted before GDXA-615 (2026-10-11) may have a NULL `created_by`** (prod, 2026-09-10, triage figure not re-queried: 4 invoice, 6 payment entries). **Not yet held everywhere:** `routers/mobile_invoicing.py` (tech mobile send of a draft) still posts P1 with no actor until mobile-tech's sibling fix lands. They are accepted as they are and documented here, not backfilled: the GL is append-only behind integrity triggers. | An entry nobody can attribute; a `gl_posted_into_locked_period` override audited as anonymous. | `modules/ledger/engine.py:_insert_entry` logs `gl_posting_without_actor` and **still writes the entry**: refusing it would abort the operational write that recorded the money. `gdx_dispatch/tests/test_gl_actor_gdxa615.py` pins `_mark_invoice_paid` (each processor source), `record_payment`'s auto-flip, and the `_recalculate_invoice` repost; the other router recalc callers, the webhook reversal/reinstatement recalcs and the deposits service are threaded but **not tested** for the actor. A log is not a gate. | **documented-only** |

## Reading guide for new contributors

If you're about to add a new router, migration, or core service: **read this file first.** Conventions in `BUILD_RULES.md` are flexible. Invariants here are not.

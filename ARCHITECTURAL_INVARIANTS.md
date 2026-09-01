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
| 3 | **HTTPException shape contract** — every error path raises `HTTPException(status_code=N, detail="…")`; never bare `raise Exception`. | Generic 500s leaking stack traces. | Partially handled in semgrep rules. | **documented-only** |
| 4 | **Route-table snapshot freshness** — `gdx_dispatch/openapi_routes.txt` (one `METHOD path` per line, generated) equals the route table the app publishes; every router `app.py` names is importable; the published document is identical across processes. | A route added or removed shows as a one-line reviewable diff in the PR; a router whose import silently fails (app.py falls back to an empty router) is caught as missing routes instead of shipping; a per-process value in the schema (the 2026-08-31 baked-in random secret) cannot come back. | `tests/test_openapi_snapshot_current.py` in the default suite; refresh with `python -m gdx_dispatch.tools.openapi_snapshot --write`. Rewritten 2026-08-31: the old row named `frontend/types/api.d.ts` (generated, **nothing imported it** — deleted with its generator), a 2.8 MB `openapi.json` nothing read (deleted; `/openapi.json` is served live), and a gate script that never existed. | **enforced** |
| 5 | **Test pollution containment** — no test rows in production with the markers. | Test data ages into prod and pollutes metrics. | Named `tools/pollution_check.py` cron until 2026-09-01. **No file of that name exists in the repo**, and nothing in `core/scheduler.py` or `.github/workflows/` runs it. Need: write the check, or drop the claim. | **documented-only** |
| 6 | **Schema = ORM** — every column matches the ORM definition. | "It works in dev but the prod column type is different" surprises. | Four scanners exist as CLI tools (`tools/drift_scanner.py`, `tenant_plane_schema_drift.py`, `tenant_schema_drift_check.py`, `comment_drift_scan.py`) and `drift_scanner` has unit tests. But **no beat entry, workflow or systemd unit in this repo runs any of them nightly** — the "nightly cron" this row claimed is not visible in the tree. A cron may exist on the VPS; the repo cannot show it, which is the point. Need: schedule one here, or cite the unit that runs it. | **documented-only** |
| 7 | **Refresh-token rotation = revoke-on-first-reuse** (RFC 9700). On detected reuse outside the 30s leeway window, the *entire refresh family* for the user is revoked. | Stolen refresh tokens silently working alongside the legitimate user. | `routers/auth.py refresh()` enforces | **enforced** |

## Reading guide for new contributors

If you're about to add a new router, migration, or core service: **read this file first.** Conventions in `BUILD_RULES.md` are flexible. Invariants here are not.

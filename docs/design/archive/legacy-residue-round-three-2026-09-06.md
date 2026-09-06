# Legacy residue, round three — what the purge left behind (2026-09-06)

**Status:** `MERGED #612 #613 #614 #615` (2026-09-06, squash-merged bottom-up; not yet released). The owner-decision list at the bottom: items 1–3 were built the same day in their own PRs (each item carries its dated line); items 4–6 are left as is.
`phase-d-saas-residue.md` (S1–S32, merged through #610, released v1.116.0).
That doc stays as the record of rounds one and two; this one owns what a
code-only sweep of main at `88f8d74` still finds.

**Date:** 2026-09-06 · **Method:** term sweep over every tracked file
(backend, frontend, config, docs), then each hit read in context and
classified. Memory and earlier session notes were deliberately not used —
only the tree.

## What already exists (do not rebuild)

| Already done | Where it shows |
|---|---|
| Single-tenant middleware pins every request to the one tenant | `core/tenant.py::TenantMiddleware`; no header/subdomain resolver |
| Vendor console, platform ORM, control-plane shims, Authentik, impersonation, act-chain shells, signup page, workspace picker — all gone | #602–#610, `test_saas_surfaces_retired.py`, `test_control_plane_removed.py` |
| Fail-closed gates on `/metrics`, inbound webhooks, QB webhook | #602, #603 |
| One public origin (`GDX_PUBLIC_BASE_URL`) for every URL builder except one (see B4) | #604 |
| Well-known documents describe only what exists | #605 |
| Design docs from rounds one and two carry dated strikethroughs, not live instructions | `mobile-all-platforms-plan.md`, `comment-accuracy-audit-2026-08-12.md`, etc. |

## Findings

Classified by what the code *does*, not by which word it uses. "tenant" as
the name of the one company (`tenant_id` columns, `TenantSettings`,
`request.state.tenant`) is the app's vocabulary and is **not** residue.

### A. Present-tense docs that still teach the removed system

| # | File | What it says | Class |
|---|---|---|---|
| A1 | `frontend/src/help/articles/modules-permissions.md:63` | Test permissions by "Impersonate" — **in-app help shipped to users**; the feature was removed in #606 | user-facing |
| A2 | `docs/BUILD_RULES.md:44-80` | Status CURRENT; "Three Planes" section instructs `TenantBase.metadata.create_all()` at signup, a shared control DB with `tenants, memberships, tenant_module_grants, billing_plan, metering_usage`, and `Depends(get_control_db)` | guide |
| A3 | `docs/E2E_VERIFICATION_MASTER_CHECKLIST.md` §22, §39 heading, "Multi-Tenant Test Strategy", line 88 | Thirteen live Tenant-A-vs-Tenant-B cases (only 13 and 15 retired); a Playwright config for `tenant-a.app.com` / `tenant-b.app.com` | checklist |
| A4 | `docs/RESTORE_RUNBOOK.md` | Marked UNRELIABLE but the steps still restore `s3://gdx-backups/tenants/<slug>`, set `CONTROL_DATABASE_URL`, count `tenants` | runbook |
| A5 | `docs/design/archive/phase-d-saas-residue.md:3` | Status line says "PARTIALLY BUILT — S1–S6 built" while its own table records S26/S27 merged and the purge shipped | tracker |
| A6 | `README.md:53` | Tree labels `control/` as "control-plane models" | front page |
| A7 | `routers/auth/__init__.py:10-12` | Docstring lists gateway, login_picker, oauth2, sso, scim, signup, pats, admin_pats, pats_support as sub-modules; the directory holds `core.py` and `sso.py` | docstring |
| A8 | `core/unified_principal.py:124,158` | Casts for `Membership.tenant_id`; there is no `Membership` model | docstring |
| A9 | `gdx_dispatch/scripts/backup.sh` | Backs up "all tenant DBs + control plane DB to S3" by `SELECT slug FROM tenants`; nothing schedules it; cited by A4 and by `SECRETS_ROTATION_RUNBOOK.md:83` ("Verify backups: bash …backup.sh") | dead script | <!-- gdx_dispatch/scripts/backup.sh: deleted by PR A; link-ok -->
| A12 | `docs/SECRETS_ROTATION_RUNBOOK.md:35,37` (CURRENT) | `docker exec gdx-control-db …`; "Edit CONTROL_DB_URL and TENANT_DB_URL" — no such container (project `gdx`, service `db`) and no such vars | runbook |
| A13 | `docs/SECRETS_ROTATION.md:18`, `docs/encryption_at_rest.md:190-198`, `docs/phonecom_api.md:699` | "Update control plane encrypted credential"; a "Three-plane isolation" section naming per-tenant DBs, `gdx_control` with RLS, and a commerce plane; "goes in a control-plane row" | guides |
| A14 | `CLEANUP_BACKLOG.md:84-93` | SPIFFE entry with a stale `app.py:1408` line ref and "To turn on: …" instructions; goes with D | tracker |
| A15 | `.env.template:32` | `DATABASE_URL` example host is `control-db`; the compose service is `db` (`docker-compose.yml:61`) | template |
| A16 | `tools/qa_baseline.json:375-376` | Expected containers `docker-control-db-1`, `docker-tenant-db-1` — read by `tools/qa_tier1.py`, so the QA tier-1 check expects two containers that cannot exist | tool baseline |
| A17 | User-visible: `SettingsView.vue:514` ("the support / `cc_support_tickets` integration being unavailable"), `QbReconciliationPanel.vue:18` ("Per-tenant override is active") | UI copy |
| A10 | ~40 backend comments/docstrings saying "control plane", "control-plane DB", "lives on the control plane", "per-tenant/paved tenants", "at signup" in the present tense (`routers/ai.py`, `modules/numbering/*`, `modules/outlook/key_storage.py`, `modules/phone_com/__init__.py`, `core/api_keys.py`, `routers/jobs.py:933,959`, `routers/invoices.py:1330`, `modules/catalog_policy/service.py:92`, `core/error_handler.py:159`, `modules/error_sink/*`, `routers/admin_ai_settings.py`, `routers/outlook_oauth.py:100`, `core/email_norm.py`, `core/customer_alert_tags.py:13`, `routers/admin_customer_tags.py:5`, `models/pricing_engine.py:225,257`, `services/pricing_engine.py:452-455`, `routers/door_catalog.py:239`, `routers/pricing_admin.py:295`, `routers/onboarding.py:6`, `core/modules.py:376,466`, `core/transactional_email.py:13`, `modules/workflow/__init__.py:9`, `core/llm/key_storage.py:6`, `modules/outlook/bootstrap.py:154-162`, `modules/outlook/admin_settings_router.py:15`, `routers/phone_com_settings.py:4`, `routers/mobile_chat.py:331`, `tools/bootstrap_app.py`, `docker/entrypoint.sh:5,44`) | Present-tense descriptions of a plane that does not exist | comments |
| A11 | Frontend comments: `stores/auth.js:4` ("tenant resolved server-side via subdomain aliases"), `SettingsView.vue:1402`, `BugReportButton.vue:87-89` ("apartment-manager cockpit"), `SegmentsView.vue:389-390`, `constants/modules.js:15` ("SaaS Billing") | Same shape, client side | comments |

### B. Code that still does, or is shaped by, the removed system

| # | Where | What it does | Fix |
|---|---|---|---|
| B1 | `core/middleware/tracing.py` | Reads `request.state.acting_on_tenant_id` and stamps it on the span. **No writer exists anywhere.** | Remove the attribute, the extractor, and the tests that feed it |
| B2 | `core/tenant.py:70` | `_API_PREFIXES` defined, never read | Delete |
| B3 | `core/circuit_breaker.py:18,211-213` | `KNOWN_SERVICES` lists `db_provisioning`; no breaker uses it; the docstring example is a `/provision` endpoint | Drop the entry; new docstring example |
| B4 | `routers/supplier_invite.py:94` + `.env.template:238` + `.env.lab.example:63` | Supplier invite links built from `SIGNUP_BASE_URL`, **default `https://example.com`** — the one URL builder #604 did not reach. ⚠ Re-classified by the audit: the invite has **no SPA caller** (`api/supplier` appears only in `DeliveryLoadsheetView.vue:98`), `/api/supplier/login` and `/register` have no route in `router/index.js`, no email is sent, and prod has **0** `supplier_invitations` and **0** `supplier_accounts`. The whole supplier portal is an orphan feature | Swap the origin to `GDX_PUBLIC_BASE_URL` and drop the var (two lines); the portal itself goes on the decision list **→ The whole router left 2026-09-06 (`chore/remove-supplier-portal`, migration 088).** |
| B5 | `tools/pave_tenant_db.py` | `--all-tenants` / `--tenant <slug>` CLI over `resolve_tenant_urls()`, which always returns the one DB; a "sort gdx first" over a one-element list | One target: `DATABASE_URL` (or an explicit URL). Keep `--yes` |
| B6 | `api/public_router.py:35-55` | `get_control_db` — a second session dependency named for a plane; comment explains a "future split-DB deployment" | Rename to `get_auth_db`, keep the separate dependency (tests override `get_db`), rewrite the comment |
| B7 | `app.py:1435-1440`, `migrations/env.py:17-32`, `.env.template:25-28,310`, `docker/demo/docker-compose.demo.yml:33-35` | `CONTROL_DATABASE_URL` still read as a fallback by the startup probe and Alembic; the template ships it. Unreachable on prod: `docker-compose.yml` is an env allowlist with no `env_file`, the prod container's env carries no `CONTROL_*` name (read 2026-09-06), and `entrypoint.sh:35-37` exports `ALEMBIC_DATABASE_URL` before `exec`, so even the in-app migrate (`admin_db.py:59`) never reaches the fallback | Read `ALEMBIC_DATABASE_URL` → `DATABASE_URL` only; delete the var everywhere. Add a guard that can actually fail: a test asserting the name appears in no tracked file outside `migrations/` and `docs/design/` (the existing `test_saas_surfaces_retired.py:262` inspects five tool modules only and cannot go red for `app.py` or `env.py`) |
| B8 | `.env.template` | `TENANT_DB_PASSWORD`, `TENANT_DB_BASE_URL`, `TENANT_TEMPLATE_DB`, `SIGNUP_BYPASS_CODE`, `GDX_CONTROL_DATABASE_URL`, `STRIPE_PRICE_STARTER/PROFESSIONAL/ENTERPRISE`, `STRIPE_TRIAL_DAYS`, `STRIPE_PORTAL_RETURN_URL`, `CONTROL_DB_PASSWORD`, `GDX_SVC_KEY`, `VPS_IP` — **zero readers** in code, compose or scripts (measured). Comments describe "provisioning new tenants", "DNS during tenant signup", "control-plane endpoints", "Platform SMTP" | Delete the vars; rewrite the comments on the survivors (`GDX_FERNET_KEY`, `ADMIN_API_TOKEN`, `PLATFORM_SMTP_*`, `CLOUDFLARE_*`) |
| B9 | `docker/docker-compose.yml:114-116,131` | Passes `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, `VPS_IP`, `GDX_SVC_KEY` into the app container; `app.py:1103-1105` records that the Cloudflare pair was removed from the startup check because it served "tenant subdomains"; no code reads any of the four | Drop the four lines |
| B10 | `docker/docker-compose.staging.yml` | 43 lines: a `control-db` service and `gdx_staging_control` database; referenced by nothing | Delete | <!-- docker/docker-compose.staging.yml: deleted by PR B; link-ok -->
| B11 | `frontend/src/components/BugReportButton.vue:79-95` | **Every bug report is written twice**: an awaited POST to `/api/feedback/bug-report` (`bug_reports`) drives the toast, then an **un-awaited, error-swallowed** POST to `/api/support/bug` (`support_tickets`) as the "control-plane mirror" for a cockpit that no longer exists. Prod has **7** `bug_reports` rows and **5** `support_tickets` rows (read 2026-09-06): two mirrored writes already failed silently. Only the support row is visible to anyone (nav → `/feedback` → `/api/support/my`, `constants/modules.js:258`); `GET /api/feedback/bug-reports` has no caller | One **awaited** write to `/api/support/bug`, toast tied to its response, no `.catch` swallow. Remove the orphaned `POST /api/feedback/bug-report` + `GET /api/feedback/bug-reports` and the `BugReport` model (`/api/feedback/client-error` stays — `errorCapture.js` and `useApi.js` call it). The physical `bug_reports` table and its 7 rows stay, per invariant #2; they were never shown anywhere. `openapi_routes.txt` loses two rows. ⚠ The audit caught my first version of this row, which kept the un-awaited write — that would have made "Bug Reported" fire on a failed POST |
| B12 | `routers/admin_db.py:70` | `_CONTROL_PLANE_TABLES` — the set of Alembic-baseline tables the drift check must not flag | Rename `_ALEMBIC_BASELINE_TABLES`; same contents |
| B13 | User-visible strings: `LoginView.vue:64` "access your workspace"; `DatabaseAdminView.vue:21,125` "Migrations (control plane)"; `PhoneComIntegrationCard.vue:412-413` "Per-tenant … we never share keys across tenants" | Say what is true |
| B14 | `core/unified_principal.py:76,140` | `ActorType` still admits `service_account` (removed, `auth/core.py:836` fails closed on it) and `spiffe_workload`; `AuthKind` admits `pat`, `scim` (removed) and `spiffe`; `oauth` stays — the MCP bridge produces it. These are typing `Literal`s — narrowing them changes nothing at runtime, and the string compare at `auth/core.py:836` must stay for stale tokens | Narrow the literals and the docstring in D. Housekeeping, not a finding |

### C. The `control` package

`gdx_dispatch/control/models.py` survived the purge as the home of `Tenant`, <!-- gdx_dispatch/control/models.py moved 2026-09-06 to gdx_dispatch/core/tenant_settings.py; link-ok -->
`TenantSettings` and the three game models, on a `DeclarativeBase` that is
**the metadata Alembic autogenerates against** (`migrations/env.py:10`). It
is imported by 20 live modules, `conftest.py`, and `core/pii.py`'s base
registry, and `tools/tenant_plane_redundant_filter_scan.py` skips the
directory by name. The name is the last surviving use of "control plane" as
a *thing* rather than a comment.

Fix: move the module to `gdx_dispatch/core/tenant_settings.py` (the tenant <!-- gdx_dispatch/core/tenant_settings.py: created by PR C; link-ok -->
row, its settings row, and the other baseline tables) and delete the package.
Every importer, the scanner skip-list, `pii.py`, `conftest.py`, `env.py` and
the README tree follow — plus `ruff.toml:48` (a per-directory B008 exemption
for the package), `.doc_link_baseline:135`, `test_01_gdx_scaffold.py:246-249`,
`test_saas_surfaces_retired.py:155`, `test_lint_gates.py:37-50` (uses the
import string as a sample), and ~25 test files that import
`gdx_dispatch.control.models`. Verified before choosing the target: no
migration version imports the package (`env.py:10` is the only import; the
hits in 001/010/064 are prose), and `control/models.py` imports only
SQLAlchemy so no cycle forms through `models/__init__` → `tenant_models` →
`core.audit`/`core.pii`. Side effect to measure: importing anything under
`gdx_dispatch.models` runs the package registry, so `migrations/env.py` will
load the full ORM (and `core/database.py`'s engine object) at Alembic time.
`alembic heads`/`upgrade` in the docker-app image is the check.
**The two-metadata split stays** — collapsing
`TenantBase` into the Alembic base is a schema-management change with its own
risk and is listed under decisions.

### D. SPIFFE workload identity

`core/spiffe/` (4 modules, 1052 lines), `core/middleware/spiffe_auth_middleware.py` <!-- SPIFFE layer deleted 2026-09-06 (PR D); link-ok -->
(198 lines), the SPIFFE branch of `core/auth_dispatcher.py`, and 7 test files.
Mounted only when `SPIFFE_ENABLE` is truthy; **no compose file, env template,
or doc sets it**, no SPIRE agent exists, and the middleware's own docstring
still says "TODO: not registered". It is the zero-trust mesh identity of the
platform that was never built. Removing it narrows `get_current_principal`
to the session flow, which is the only one that has ever run.

D followers the first draft missed: `_jwt_has_spiffe_sub` at
`auth_dispatcher.py:505`, `core/spiffe/workload_caps.json`, the <!-- SPIFFE layer deleted 2026-09-06 (PR D); link-ok -->
`CLEANUP_BACKLOG.md` entry (A14). Tests: delete `test_spire_trust_bundle.py`, <!-- SPIFFE layer deleted 2026-09-06 (PR D); link-ok -->
`test_svid_validator.py`, `test_spiffe_id.py`, `test_spiffe_auth_middleware.py`, <!-- SPIFFE layer deleted 2026-09-06 (PR D); link-ok -->
`test_workload_capability_map.py`; edit `test_auth_dispatcher.py` and <!-- SPIFFE layer deleted 2026-09-06 (PR D); link-ok -->
`test_principal.py`. `SPIFFE_ENABLE` is set nowhere: not in any compose file,
`.env.demo.example`, a workflow, or the prod container's env (read 2026-09-06).

## Adversarial audit (2026-09-06, before any code)

Verdict in `critique_latest.md`. What it changed: B11 was inverted (fixed <!-- critique_latest.md: session memory file, not in the repo; link-ok -->
above — the awaited write was the one I planned to drop); B4 re-classified
from "live URL builder" to "orphan feature with a bad default"; B7's mechanism
was overstated (unreachable on prod, still worth deleting); B14 is housekeeping;
A12–A17 added. The verification section gains a guard that can fail for B7.
Prod facts read for it (all read-only): app container env names carry
`GDX_SVC_KEY` (empty), `VPS_IP` (empty), `CLOUDFLARE_ZONE_ID` and
`CLOUDFLARE_API_TOKEN` (set, unread by any code); no `CONTROL_*`, `SPIFFE_*`,
`SIGNUP_*`; `update.sh` has an `EXTRA_COMPOSE` hook and prod uses it for
`docker-compose.serverports.yml` (ports only). <!-- docker-compose.serverports.yml: untracked prod-only overlay; link-ok -->

## Packaging — four PRs, independent unless noted

| PR | Contents | Behaviour change |
|---|---|---|
| **A** `docs/legacy-residue-prose` | A1–A7, A9, A10, A12, A13; A11 except the three lines B rewrites anyway (`SettingsView.vue:1402`, `BugReportButton.vue`, `constants/modules.js:15` — a dated changelog line, left) | none (prose, docstrings, comments, one dead script). A8 goes with D, which rewrites that module |
| **B** `chore/legacy-residue-dead-code` | B1–B13 (B14 is done in D, which rewrites that module) | supplier invite links stop pointing at example.com; bug reports written once; five env vars and one compose file removed |
| **C** `chore/rename-control-package` | Section C | none (import paths) |
| **D** `chore/drop-spiffe` | Section D + B14 (`AuthKind` keeps `oauth`: the MCP bridge produces it) | `SPIFFE_ENABLE` no longer does anything (it never did outside a lab) |

B, C and D each touch `core/unified_principal.py` or `app.py`; they are cut
from main independently and rebased on merge, in that order.

## Decisions for the owner (NOT built here)

1. **`superadmin` role.** `core/roles.py` still defines a platform-level
   `super_admin` above `owner`; 30+ gates list it. Removing a role is a
   user-facing and data question (existing users, role dropdowns). Left as is.
2. **Physical leftovers on prod:** `tenant_module_grants`, `service_accounts`,
   `platform_feature_flags` (0 rows), `tenants.subscription_status`,
   `tenants.stripe_connect_account_id`. A drop migration was already decided
   in `phase-d-saas-residue.md` and blocked on the purge; it is unblocked now.
   → **Built 2026-09-06** (`chore/migration-087-drop-retired-tables`, migration 087): the three tables and two
   columns dropped; `bug_reports` (7 rows) copied into `support_tickets` first.
3. **Module `tier` (`core/modules.py`, `plugin_api/manifest.py`).** Plan
   tiers of a subscription never sold; nothing gates on them, but `tier` is
   part of the public plugin manifest contract (warn-and-strip would make
   removal safe for third-party plugins). Product call.
   → **Built 2026-09-06** (`chore/drop-module-tiers`): `tier` and `default`
   gone from MODULES and every emitter; the manifest field is accepted and
   ignored so older plugins keep loading.
4. **The two-metadata schema split** (Alembic base vs `TenantBase` +
   `create_all`). Real single-tenant simplification; real migration risk.
5. **`PLATFORM_SMTP_*` and `POWER_APPS_*` env names** are read by live code
   (password reset; Outlook seeding) and set on prod. Renaming them means a
   coordinated prod env change. Left as is; comments corrected.
6. **`tenant_models.py` / `TenantBase` / `get_tenant_db`** are the app's
   vocabulary for "this company's tables" — hundreds of call sites. Not
   residue by the classification above; renaming is a separate project.

## Verification plan

- Full 7-shard matrix on each branch and on the merged tree; every FAIL/SKIP
  named. Ruff ratchet vs baseline. `doc_link_scan --strict`.
- `openapi_routes.txt` regenerates byte-identical for A, C, D; B changes no
  routes either (the removed writes are client-side).
- Browser walk in a throwaway container: login (the new copy), Settings →
  Database Admin (the relabelled panel), Settings → Integrations → Phone.com
  card, the bug-report button (one POST in the network log), a supplier invite
  link containing the public origin. Light and dark, desktop and 390px.
- Sibling sweep, declared now: the shape is *a doc, comment, string, env var
  or symbol that describes or implements per-tenant/control-plane behaviour
  as if it were current*. Surface: every tracked file outside
  `migrations/` and `docs/design/` past-tense records. The inventory above
  is that sweep's result; the PRs will re-run the same greps before merge and
  report the residual count.

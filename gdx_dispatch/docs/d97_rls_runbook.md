# D97 — Control-plane RLS fail-closed runbook

**Filed:** an earlier session, 2026-04-25 (F4 audit found `gdx` connection role has `rolsuper=t rolbypassrls=t`; all 26 control-plane RLS policies were no-ops on prod).

**Goal:** Switch the FastAPI app's CONTROL DB connection from `gdx` (superuser) to `gdx_app` (no super, no bypassrls) so the existing migration-024/025 policies actually enforce.

**Risk:** Wrong grants → 500 on every request. Wrong env value → connection refused. Order matters.

---

## Phase 0 — Column-shape reconciliation (BLOCKER added an earlier session, must run before Phase 1)

The env-switch was attempted an earlier session 14:30 UTC + rolled back. Investigation surfaced that the existing migration-024 policies cannot work as written even with `gdx_app` enforcing them, because the columns they compare are heterogeneous:

- `memberships.tenant_id` stores **slugs** (e.g. `gdx`)
- `tenant_module_grants.tenant_id` stores **UUIDs** (e.g. `a1b2c3d4-…`)
- Both policies compare against `current_setting('app.tenant_id')` — same GUC value
- Whichever shape the app sets, **one class returns 0 rows under enforcement**

Under `gdx` (super) this is hidden because RLS is bypassed. Under `gdx_app` it would surface as broken auth.

### Open questions (need Doug input before writing code)

1. **Canonical type:** UUID or slug?
   - `tenants.id` is UUID (immutable PK). `tenants.slug` is mutable varchar used for URL routing.
   - **Recommendation: UUID.** Foreign keys should reference the immutable PK. Slug renames don't cascade through every membership/grant row. Industry default for cross-table FKs.
   - Trade-off: any external code reading memberships by slug breaks (greppable; should be ~zero non-test callers — confirm before commit).

2. **Migration strategy:** big-bang or incremental?
   - Big-bang: one alembic that ALTERs every slug-storing `tenant_id` column to UUID via `(SELECT id FROM tenants WHERE slug = old_value)` lookup.
   - Incremental: 4 migrations — (a) add `tenant_uuid` column nullable on slug-storing tables, (b) backfill via JOIN, (c) swap policies + writers to read tenant_uuid, (d) drop the old slug column + rename tenant_uuid → tenant_id.
   - **Recommendation: incremental.** Each step independently reversible; no atomic-on-prod-and-pray.

3. **Which 25 RLS-protected control-plane tables actually have the slug-shape problem?**
   - Confirmed: `memberships.tenant_id` (slug), `tenant_module_grants.tenant_id` (UUID).
   - Other 23: unknown — likely a mix. First-command below surveys them all.

### First commands when next session opens

```bash
# 1. Survey: which tables have tenant_id / company_id and what data type
ssh your-server 'docker exec docker-control-db-1 psql -U gdx -d gdx_control -c "
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE column_name IN ('"'"'tenant_id'"'"', '"'"'company_id'"'"')
  AND table_schema = '"'"'public'"'"'
ORDER BY table_name;"'

# 2. Sample shape: detect actual stored value type per table
# (separate Python script — too many tables for hand-querying; runs as
# gdx super, redacts no secrets, just prints table_name → sample_value
# with shape inferred via regex)
```

### Phase 0 deliverable

- A markdown table in this runbook listing every tenant_id-bearing control-plane table + current shape + target shape
- Doug signoff on canonical type (UUID vs slug)
- Incremental-migration sketch with file names + DDL stub

Only after Phase 0 closes do we pick up Phase 1 below (the original env-switch sequence).

---

## Phase 0 — Survey results (an earlier session, 2026-04-26)

**Canonical type signed off: UUID.** Doug 2026-04-26.

**Why UUID is forced, not just preferred:** the app already pushes a UUID into the RLS GUC — `gdx_dispatch/core/tenant.py:182` sets `request.state.tenant_id = tenant["id"]` and `tenant["id"]` is `CAST(tenants.id AS TEXT)` (the UUID PK). Every `set_session_role` call therefore executes `SET LOCAL app.tenant_id = '<uuid>'`. Slug-shaped FK columns can never satisfy that policy comparison without a column-shape change. We don't have a "pick a side" decision to make — the GUC is the side; the FKs are the deviation.

### Survey: every tenant_id-bearing column on prod `gdx_control` (38 columns / 35 tables)

Source: `information_schema.columns WHERE column_name LIKE '%tenant_id%'` against prod `docker-control-db-1` an earlier session.

**Already UUID — no work needed (1 table, 2 cols, 98 rows):**

| Table | Column | Type | Rows |
|---|---|---|---|
| `tenant_module_grants` | `tenant_id` | uuid | 98 |
| `tenant_module_grants` | `granted_by_tenant_id` | uuid | 98 |

**Slug-shaped on schema, but ALL stored values are already UUID strings — pure type-change, no backfill (1 table, 73,543 rows):**

| Table | Column | Type | Rows | Sample |
|---|---|---|---|---|
| `platform_consumer_audit` | `tenant_id` | varchar(64) | 73,543 | `886a5b78-6bff-4b19-823c-a2c16684447e` |

Action: `ALTER COLUMN tenant_id TYPE uuid USING tenant_id::uuid`. Done.

**Slug-shaped, holds slug values, requires JOIN backfill (3 tables, 24 rows):**

| Table | Column | Type | Rows | Distinct slugs |
|---|---|---|---|---|
| `memberships` | `tenant_id` | varchar(100) | 9 | gdx, gdx-old, demo-garage-co, browser-test-co |
| `installations` | `tenant_id` | varchar(100) | 7 | (one per known tenant: gdx, gdx-old, demo-garage-co, browser-test-co, example-garage-test, qa-test-value, test) |
| `tenants` | `parent_tenant_id` | varchar(100) | 8 | all NULL — type-change only |

**Slug-shaped, NULL-only data — pure type-change (2 tables, 2 rows):**

| Table | Column | Rows | Sample |
|---|---|---|---|
| `game_definitions` | `tenant_id` | 1 | NULL |
| `game_state` | `tenant_id` | 1 | NULL |

**Slug-shaped, empty tables — pure type-change (30 tables, 0 rows each):**

`audit_logs · audit_retention_policy · billing_overage_event · billing_plan · cross_tenant_share (sharer_tenant_id, sharee_tenant_id) · cross_tenant_share_acceptance (accepted_by_tenant_id) · cutover_schedule · deprecated_table_record · event_outbox · locations · mcp_execution_log · mcp_tool_execution_audit · metering_checkpoint · metering_usage · resource_field_descriptors · resource_instance · resource_type (owner_tenant_id) · sandbox_envs · shadow_migration_checkpoint · shadow_migration_drift · shadow_migration_state · shared_resources (owner_tenant_id) · shares (target_tenant_id) · ss21_admin_consent_grants · ss21_authorization_codes · ss21_oauth_tokens · ss21_webhook_subscriptions · ss31_federation_provider · sso_configs · platform_consumer_audit · tenant_health_logs`

Plus `platform_feature_flags.company_id` (varchar(64), 0 rows) — same treatment.

### Tables defined in ORM but NOT present on prod control-db

`tenant_relationships`, `cross_tier_module_grants`, `whitelabel_configs`, `reseller_*` — declared with UUID FKs in `gdx_dispatch/control/relationships.py`/`reseller.py`/`whitelabel.py` but never created in production. No prod work needed; ORM stays as-is, gets created with the right shape at next pave or first use.

### Total real backfill scope

**Twenty-four rows.** memberships (9) + installations (7) + parent_tenant_id (0 NULL) + game (1+1 NULL) + platform_consumer_audit (already UUID strings, 73,543 row type-change-via-cast) = a coffee-break-sized data migration.

### Discovered an earlier session by cross-joining with `tenants`

- **All 4 distinct `memberships.tenant_id` slugs and 7 `installations.tenant_id` slugs resolve to live (non-deleted) `tenants.slug` rows.** Backfill JOIN is safe; `tenant_uuid IS NULL` post-migration assertion will pass.
- **6 orphan rows in `platform_consumer_audit`** carry tenant_id `d59f993d-9789-463f-b84d-421a188968be` which is not in `tenants.id`. Inspection shows curl probe traffic from 2026-04-22 against a tenant subsequently hard-deleted. **Adding a `FOREIGN KEY (tenant_id) REFERENCES tenants(id)` to `platform_consumer_audit` would fail.** Recommendation: do NOT add FK on the audit table — append-only logs about deleted tenants should still survive for compliance. Type-change to uuid only; leave referential integrity off. (If FK desired, use `NOT VALID` to skip pre-existing rows.)
- **`tenants.parent_tenant_id` has zero active writers** in the codebase. Confirmed via grep: the only references are read-side in `gdx_dispatch/control/relationships.py` against `TenantRelationship.parent_tenant_id` (a different column on a different table). The column on `tenants` itself is dead code. **Migration revision: drop the column rather than migrate it.** Down migration recreates as nullable varchar for safety.
- ⚠ **GDX tenant has placeholder UUID `a1b2c3d4-e5f6-7890-abcd-ef1234567890`** — clearly hand-typed, not random. Pattern is predictable; likely escaped from a seed fixture into prod. Out of D97 scope (re-keying GDX is a multi-system blast-radius migration touching every audit row, JWT history, R2 prefix, etc.) but flagged here so a future engineer doesn't take it as evidence of a generated UUIDv4. File a separate D-item if/when this becomes load-bearing.

### Application-layer call-site impact

`grep -rn 'Membership.tenant_id\b'` finds 14 non-test sites in `gdx_dispatch/routers/scim.py`, `gdx_dispatch/routers/admin_pats.py`, `gdx_dispatch/routers/me.py`, `gdx_dispatch/routers/pats_support.py`, `gdx_dispatch/core/auth_dispatcher.py`, `gdx_dispatch/tools/backfill_users_to_identities.py`. Every one compares against `principal.tenant_id` (currently a slug from JWT `gdx_tid`) or a `Tenant.slug` join.

`Principal.tenant_id` itself is fed by the Authentik `gdx_tid` claim, sourced from `user.attributes["memberships"]` written by an out-of-tree gdx-sync job. **Authentik membership values must flip from slug to UUID at the same time as the memberships column flip**, or the `active_tenant in memberships` check in `authentik_property_mapping_gdx_tid.SANDBOX_EXPRESSION` breaks token mint.

Single string-literal slug comparison search (`'tenant_id == "literal-slug"'`) returned **0 hits** in non-test code. The 71 dynamic call sites all flow through `principal.tenant_id`, so the migration is one type-flip away from clean.

`gdx_dispatch/routers/me.py:50` does `outerjoin(Tenant, Tenant.slug == Membership.tenant_id)` — flip to `Tenant.id == Membership.tenant_id` in the same commit as the column shape change.

---

## Phase 0c — Pre-029 research findings (an earlier session, 2026-04-26)

Two parallel research passes (external Postgres/Alembic best-practice review + internal codebase sweep) before opening migration code. Doug-driven. Three prod read-only checks ran during the pass — results inline below.

### External findings (Postgres + Alembic gotchas to fold into 031)

1. **Pgbouncer cached prepared-statement plans.** After 031's column-type swap, the pool's cached plans error with `cached plan must not change result type`. Mitigation: rolling app restart after 031 deploy. The Phase 1 env-switch already restarts — sequence **031 deploy → app restart → Phase 1** to absorb this for free. (`gdx_dispatch/tools/deploy_tgd.sh` already restarts the container, so this is automatic if 031 lands as a normal deploy step.)
2. **DROP + CREATE the RLS policies explicitly in 031,** rather than relying on column-rename re-resolution. Postgres re-resolves policy expressions by name at parse-time, so it works silently — but "silent" is exactly what we don't want post-D97. Be loud: `DROP POLICY ... ; CREATE POLICY ... ;` for every policy on memberships, installations, etc.
3. **`SET LOCAL statement_timeout = 0`** + `lock_timeout` retry at the top of 031. The `platform_consumer_audit` 73K-row rewrite can hit the prod default `statement_timeout`.
4. **`VACUUM (ANALYZE) platform_consumer_audit`** immediately post-031. Heap rewrite leaves bloat + stale row-width stats; planner will mis-estimate until autovacuum catches up.
5. ✅ **Replication slots: NONE on prod control-db.** Verified an earlier session — `pg_replication_slots` returned 0 rows. No CDC / logical replication breakage at ALTER COLUMN TYPE.
6. **Take a backup post-029 / pre-031.** Cheap insurance — a rollback past 031 without it loses the backfilled `tenant_uuid` column data.
7. **Run the downgrade against lab** explicitly. Down-migrations rot silently because nobody tests them.
8. ✅ **View dependencies on platform_consumer_audit: NONE.** Verified an earlier session — `pg_depend` join returned only indexes (`ix_sca_*`) + the toast table. No views to invalidate. ALTER COLUMN TYPE will rebuild the 4 indexes (expected).

### Internal codebase findings (call sites the 14-site list missed)

9. **`.endswith("-sandbox")` in `gdx_dispatch/routers/pats.py:156` + `gdx_dispatch/routers/admin_pats.py:215`** — assumes slug shape. ✅ **Verified dead in prod an earlier session** — `SELECT slug FROM tenants WHERE slug LIKE '%-sandbox'` returns 0 rows out of 8 total tenants. Action in 031: delete the dead branches (don't bother adding `Tenant.is_sandbox` for code with no live callers).
10. **`service_accounts.allowed_tenant_slugs JSON`** at `gdx_dispatch/control/models.py:202` stores a slug array. Affects `gdx_dispatch/core/service_accounts.py:87-96`, `gdx_dispatch/tools/migrate_service_accounts.py:52-70`, `gdx_dispatch/tools/service_account_mint.py:54,98`. **This is a sibling shape-migration to D97**, not part of it. Recommend: handle as **029b** (separate alembic file in the same sequence) — add `allowed_tenant_uuids JSON`, backfill via `Tenant.id` lookup, swap readers in 031, drop old column in 032. Small footprint (one column, one table) but the readers must flip in lockstep.
11. **SCIM `group_id = f"{principal.tenant_id}:{role}"`** at `gdx_dispatch/routers/scim.py:709` — externalId shape changes at flip. SCIM is dormant in prod (no IdP wired, 0 active SCIM clients per an earlier session audit) → no client breakage. Document the externalId-shape change for whenever SCIM gets wired.
12. **`game_definitions.tenant_id` + `game_state.tenant_id`** at `gdx_dispatch/control/models.py:126,152` still annotated `Mapped[str | None]`. Both tables are empty per the survey, so no data work — just flip the annotations to `Mapped[UUID | None]` in 031.

### Internal sweep — confirmed safe (no action needed)

- Celery task signatures `tenant_id: str` already receive UUID values via `str(t.id)` from callers.
- Cache keys / log prefixes (`f"{tenant_id}:..."`, `f"flag:{key}:{tenant_id}"`) — UUID strings format identically.
- `gdx_tid` / `tenant_id` JWT claim accepts either name; SPA tokens already carry UUID.
- `AuditLog.entity_id.like(f"{tenant_id}%")` prefix matching — UUID strings still match.
- `identity_repo.get_tenant_chain(tenant_slug)` — dead post-031 because `tenants.parent_tenant_id` drops; no migration work.

### Updated migration count

Was 4 (029/030/031/032). Now **5** with 029b (service_accounts.allowed_tenant_slugs → allowed_tenant_uuids). 029 and 029b are both additive nullable, fully independent — order between them doesn't matter.

### Citations (external research)

- Crunchy Data — *When Does ALTER TABLE Require a Rewrite* (heap rewrite + index rebuild semantics)
- Squawk — `changing-column-type` lint rule (lock + timeout pattern)
- Postgres docs — ALTER POLICY (column-name re-resolution at parse-time)
- GitLab — *Avoiding downtime in migrations* (expand-contract pattern)
- pg-osc — zero-downtime schema-change tool (shadow-table + trigger pattern)
- Alembic issue #719 — autogenerate doesn't detect varchar→text/uuid type changes (hand-write all migrations)
- *Zero-downtime upgrades with Alembic + SQLAlchemy* (`postgresql_using` requirement)
- Aiven — *Exploring PostgreSQL 18 UUIDv7 support* (B-tree locality; v7 preferred for new tables, not load-bearing for D97)

---

## Phase 0a — Migration sketch (incremental, 4 alembic files)

Each file is independently deployable + reversible. Land in order; verify after each.

### `029_add_uuid_columns_control_plane.py`

Add `tenant_uuid uuid NULL` next to every slug-shaped `tenant_id` (and the four cousin columns: `sharer_tenant_id`, `sharee_tenant_id`, `accepted_by_tenant_id`, `target_tenant_id`, `owner_tenant_id`, `company_id`). No drops. App keeps reading the slug column.

```python
op.add_column("memberships", sa.Column("tenant_uuid", postgresql.UUID(as_uuid=True), nullable=True))
op.add_column("installations", sa.Column("tenant_uuid", postgresql.UUID(as_uuid=True), nullable=True))
# tenants.parent_tenant_id is dead code (zero writers) — drop in 031 instead of migrating
# ... 30+ more empty tables get the same UUID column
```

Downgrade: `DROP COLUMN tenant_uuid` on every touched table.

### `030_backfill_uuid_columns.py`

Single SQL for the only two tables with real data:

```sql
UPDATE memberships m SET tenant_uuid = t.id FROM tenants t WHERE t.slug = m.tenant_id;
UPDATE installations i SET tenant_uuid = t.id FROM tenants t WHERE t.slug = i.tenant_id;
-- platform_consumer_audit handled in 031 via direct ALTER ... USING tenant_id::uuid (no UUID column needed)
-- 30 empty tables: backfill is a no-op
```

Post-migration assertion: `SELECT count(*) FROM memberships WHERE tenant_uuid IS NULL` → 0; same for installations.

Downgrade: `UPDATE … SET tenant_uuid = NULL` (reversible, columns stay).

### `031_swap_writers_and_policies.py` *(largest commit; needs app-side coupling)*

Two-phase, atomic in the alembic transaction:

1. **Code change** ships in same commit:
   - `Membership.tenant_id` ORM column declared as `Mapped[UUID]` with FK → `tenants.id`
   - All 14 router/service call sites updated: `principal.tenant_id` carries UUID; `Membership.tenant_id == principal.tenant_id` works
   - `me.py:50` join: `Tenant.id == Membership.tenant_id`
   - **`auth_dispatcher.py:640` patched** — replace `tenant_id = tenant_state["slug"]` with `tenant_id = tenant_state["id"]` (the session-token path explicitly writes slug into Principal today; this is the runtime slug-write that must flip)
   - **PAT/SCIM tokens revoked** at cutover (`DELETE FROM access_tokens` on the relevant tenant DBs + redis denylist)
   - Authentik sync: NOOP (dormant — no users provisioned)

2. **Schema swap** in the migration:
   ```sql
   -- platform_consumer_audit: data already UUID-shape, type-cast only.
   -- 6 orphan rows reference deleted tenant d59f993d-…; do NOT add FK
   -- (audit logs survive tenant deletion for compliance).
   ALTER TABLE platform_consumer_audit ALTER COLUMN tenant_id TYPE uuid USING tenant_id::uuid;

   -- memberships, installations: rename swap (tenant_uuid → tenant_id)
   ALTER TABLE memberships DROP COLUMN tenant_id;
   ALTER TABLE memberships RENAME COLUMN tenant_uuid TO tenant_id;
   ALTER TABLE memberships ALTER COLUMN tenant_id SET NOT NULL;
   ALTER TABLE memberships ADD CONSTRAINT fk_memberships_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
   -- ... same for installations, all empty tables

   -- tenants.parent_tenant_id: dead code, drop entirely
   ALTER TABLE tenants DROP COLUMN parent_tenant_id;
   ```

3. **RLS policy refresh** — policies on memberships/installations/etc were comparing `tenant_id` against a UUID GUC and silently failing under `gdx` super; now they actually fire. Re-render via existing `control_plane_rls_targets.py` with no expression change (column is now UUID, GUC is UUID, types align).

Downgrade: opposite swap + `ALTER ... TYPE varchar(100) USING tenant_id::text`. Lossy if anything wrote between up/down — same caveat as any column-type migration.

### `032_drop_dead_uuid_alts.py`

Cleanup: any temporary `tenant_uuid` that survived 031 because the source column was already empty / the rename path didn't apply. Drop them.

**Status (an earlier session):** ✅ written + lab-applied + downgrade exercised. Lab leftover count = 0 (031 cleaned them all per design). Migration kept as a defensive fence-post against future drift. Downgrade is a no-op by design (we don't reconstruct the SHADOW_COLUMNS catalog from scratch — caller should `alembic downgrade d97_backfill_uuid_columns` if shadows are wanted back).

---

### Auth-path inventory (an earlier session deep dive — major finding)

The Authentik probe (`docker exec docker-authentik-postgres-1 psql -c "SELECT username, attributes FROM authentik_core_user"`) returned **3 system users** (AnonymousUser, akadmin, ak-outpost-…) with **empty `{}` attributes** — no `memberships`, no `active_tenant`. **The Authentik OIDC flow is dormant in production.** No GDX users ever provisioned. The `gdx_tid` claim, `MissingTenantClaim` exception path, and `authentik_property_mapping_gdx_tid` sandbox expression are dead code on prod today.

That blew open what auth is actually doing. There are **6 active dispatch paths** in `gdx_dispatch/core/auth_dispatcher.py` + `gdx_dispatch/routers/auth.py`, each with its own `Principal.tenant_id` provenance:

| # | Path | `Principal.tenant_id` source | Shape today |
|---|---|---|---|
| 1 | **SPA login** (`/auth/login`) | `request.state.tenant["id"]` (UUID from host-header lookup) → JWT claim `tenant_id` | **UUID** |
| 2 | **PAT** (Personal Access Tokens) | `Membership.tenant_id` (auth_dispatcher.py:340 explicit comment) | **slug** |
| 3 | **SCIM** (service-to-service) | `ScimPrincipal.tenant_id` from token registration row | mixed |
| 4 | **SPIFFE / mTLS** | `_spiffe_tenant_id()` derived from cert claim | varies |
| 5 | **OAuth** (3rd-party app) | `rec.tenant_id` from oauth registration, or `f"oauth-unresolved:{client_id}"` | mixed |
| 6 | **Session token** | `tenant_state["slug"]` if a tenant is resolved (auth_dispatcher.py:640) | **slug, written explicitly** |
| 7 | **Authentik OIDC** | DORMANT — no users provisioned | n/a |

**This narrows D97's blast radius dramatically.** SPA users (Path 1, the daily-driver Doug+technicians flow) already get UUIDs; their tokens don't break at cutover. The shape mismatch only affects:

- **Path 2 (PAT)** — admin tooling, automation. JWTs minted today carry slug; they break the moment Membership.tenant_id flips.
- **Path 6 (session token)** — `auth_dispatcher.py:640` *explicitly writes* `tenant_state["slug"]` into Principal. This is a runtime slug-write, not just a column-shape problem. **Must be fixed in the same commit as Membership.tenant_id flip.**
- **Path 3 (SCIM)** — depends on what registrations carry. Need a per-registration audit before cutover.

Paths 4, 5, 7 are unaffected (path 7 because dormant; 4/5 use cert/registration shapes that aren't tied to Membership).

### In-flight token risk at cutover (revised)

The original draft assumed every authenticated user gets a slug claim. That was wrong — only PAT/SCIM/session-token users do. So:

- **SPA tokens (Path 1):** ZERO impact. Tokens already carry UUID `tenant_id`.
- **PAT tokens (Path 2):** every active PAT breaks until rotated. Mitigation: revoke all `access_tokens` rows at cutover. PATs are admin-tooling-only — small population, easy to communicate.
- **Session tokens (Path 6):** affected. Same mitigation as PAT — invalidate active sessions. Re-login restores them.
- **SCIM (Path 3):** if registrations carry slug, those need per-row migration. Audit first; if all UUID-shaped already, no-op.

Recommendation: **revoke all PATs + SCIM tokens at cutover**, force one round of SPA re-login (precautionary; low cost), patch path 6 in the migration commit. No "force-expire all tokens" / `kid` rotation needed since the SPA path is already the right shape.

### Authentik writer migration: NOOP

Originally I sketched migration C2 as "flip the Authentik sync job to write UUIDs." Since the Authentik flow is dormant, **C2 doesn't exist as work**. If/when Authentik is ever wired up, the property mapping (`gdx_dispatch/tools/authentik_property_mapping_gdx_tid.py`) needs a one-line change to emit UUIDs from `user.attributes["memberships"]`, and whoever ships the (still-out-of-tree) `gdx-sync` job has to write UUIDs into that attribute. Both are future-tense; D97 doesn't gate on either.

---

## Phase 0b — Pre-deploy checklist (revised an earlier session)

**029 / 029b (additive, zero behavior change — soak-safe per Doug 2026-04-26):**
- [x] 029 applied to lab control-db — an earlier session. 34/36 cols (locations + tenant_health_logs absent on lab; guard skip).
- [x] 029b applied to lab — an earlier session. `allowed_tenant_uuids JSON YES` confirmed.
- [x] 029 + 029b downgrade exercised on lab — an earlier session. Reversible.
- [x] 029 + 029b applied to **prod** — an earlier session. 36/36 shadow cols present.

**030 (backfill, additive):**
- [x] 030 applied to lab — an earlier session. Empty memberships/installations on lab; assertions trivially held; downgrade exercised.
- [x] 030 applied to **prod** — an earlier session. memberships 9/9 backfilled, installations 7/7 backfilled, 0 unresolved.
- [x] service_accounts: prod row has JSON literal `null` (not SQL NULL); `json_typeof='array'` filter correctly skipped it. allowed_tenant_uuids remains NULL by design.

**Pre-031 prep:**
- [x] All 14 grep'd call sites refactored locally; `pytest gdx_dispatch/tests/ -q` green — an earlier session (commit `cf07b7f2`).
- [x] `auth_dispatcher.py:640` slug-write patched to write UUID — an earlier session (`cf07b7f2`).
- [x] SCIM token registrations audited (an earlier session) — `ss21_oauth_tokens` empty, PAT/SCIM derive tenant via `installations.tenant_id`, no separate work
- [x] Sandbox slug check (an earlier session) — 0 prod tenants match `%-sandbox`; `pats.py:156` + `admin_pats.py:215` `.endswith("-sandbox")` branches are dead, delete in 031
- [x] Replication slots check (an earlier session) — 0 slots on prod control-db; ALTER COLUMN TYPE safe
- [x] View dependency check on `platform_consumer_audit` (an earlier session) — only indexes + toast, no views
- [x] `service_accounts.py` + `service_account_mint.py` + `migrate_service_accounts.py` flipped to read `allowed_tenant_uuids` — an earlier session (`cf07b7f2`).
- [x] `game_definitions` / `game_state` ORM annotations flipped `str → UUID | None` — an earlier session (`cf07b7f2`).
- [x] SCIM `group_id` shape change documented (dormant; no client breakage today) — an earlier session (`cf07b7f2`).
- [x] MCP audit ORM (`McpToolExecutionAudit.tenant_id`, `McpExecutionLog.tenant_id`) + SS-28 audit ORM (`PlatformConsumerAudit.tenant_id`, `AuditRetentionPolicy.tenant_id`) flipped `String → Uuid` — an earlier session.
- [x] `tests/fixtures/tenant_trees.py` deleted + lone `parent_tenant_id` integration test removed; `tenants.parent_tenant_id` now has zero writers anywhere — an earlier session.
- [ ] Backup taken on prod (`pg_dump gdx_control`) **post-029, pre-031** (insurance for backfill rollback)
- [x] PAT/SCIM token revocation script ready — `gdx_dispatch/tools/d97_revoke_pats_scim.py`. Soft-revoke (set `revoked_at`, matches existing pattern in `admin_pats.py:446`), idempotent, dry-run by default, lab-verified an earlier session (3 synth tokens: 2 active → revoked, 1 pre-revoked unchanged, idempotent re-run clean).

**031 (load-bearing — all of the below must hold):**
- [x] Migration body opens with `SET LOCAL statement_timeout = 0` + `SET LOCAL lock_timeout = '5s'` — an earlier session.
- [x] DROP POLICY + CREATE POLICY explicit for every affected policy (no rely-on-rename) — an earlier session. Includes commerce-plane policies (cross_tenant_share_parties_read, cross_tenant_share_sharer_write, cross_tenant_share_acceptance_accepter_only) caught on lab.
- [x] Locks acquired in deterministic table order (alphabetical) to minimize deadlock risk — an earlier session.
- [x] Post-migration: `VACUUM (ANALYZE) platform_consumer_audit` runs as a one-shot — an earlier session prod (post-deploy).
- [x] App restart sequenced after 031 (pgbouncer cached-plan invalidation) — an earlier session prod (deploy_tgd.sh `up -d --force-recreate app` after `alembic upgrade head`).
- [x] 031 applied to lab — an earlier session. /health=200 post-rebuild; SPA login works (UUID path); fresh signup creates tenant + tenant_module_grants inserts work; 26 policies rendered with ::text cast.
- [x] Lab downgrade exercised — an earlier session. Round-trip down → up clean; identical post-state. Slug columns restored, FKs dropped, parent_tenant_id back, allowed_tenant_slugs JSON re-created.
- [x] Backup taken on prod (`pg_dump gdx_control`) before the 031 deploy — an earlier session (`/var/backups/gdx_control_pre_d97_phase0a_complete_20260426_203152.sql.gz`, 7.6 MB).

**Prod deploy (an earlier session, commit `c04ce682`):**
- [x] 031 + 032 applied via deploy_tgd.sh — head = `d97_drop_dead_uuid_alts`. Healthy in 12s. No DB-snapshot regressions.
- [x] Schema verified: memberships/installations/platform_consumer_audit tenant_id all uuid; parent_tenant_id + allowed_tenant_slugs dropped; 0 shadow tenant_uuid cols remaining; 36 FKs (= 31 CASCADE + 5 SET NULL); 26 RLS policies.
- [x] PAT/SCIM revocation ran clean — 7 service-account tokens revoked, 0 user PATs, idempotency verified.
- [x] Row counts intact post-deploy: memberships=9, installations=7, tenant_module_grants=98, tenants=8 (matches an earlier session baseline).
- [x] Doug-eyes verification: real SPA login on `gdx.example.com` confirmed working — Doug 2026-04-26 an earlier session.

**Phase 0a → CLOSED 2026-04-26 an earlier session.** Phase 1 (env-switch CONTROL_DATABASE_URL from `gdx` superuser → `gdx_app` no-super no-bypassrls) opens next as separate sprint scope per runbook lines 359-463.

Then Phase 1 (env switch to `gdx_app`) opens — at that point all the column shapes match the GUC, and migrations 024/025 RLS policies fire correctly for the first time.

---

## Index of artifacts (where the notes live)

| Artifact | Path | Purpose |
|---|---|---|
| Architectural rule | `CLAUDE.md` § Tenant Identity | The contract — UUID is identity, slug is routing label, with citations |
| Working plan | `gdx_dispatch/docs/d97_rls_runbook.md` (this file) | Survey, auth-path inventory, migration sketch, cutover plan |
| Lint gate | `gdx_dispatch/tools/tenant_id_shape_scan.py` | AST scan; line-insensitive baseline; pre-commit blocking |
| Baseline | `.tenant_id_shape_baseline` | 181 known violations; new code can't add to this list |
| Pre-commit hook | `.git/hooks/pre-commit` (local-only per project convention) | Wires the gate to commits |

## Open questions for next session

1. ~~SCIM registrations audit~~ — **closed an earlier session**. `ss21_oauth_tokens` is the only token table with a direct `tenant_id` column and it has 0 rows (already in the empty-tables bucket; pure type-change in 029). `access_tokens` (PAT + SCIM) has no `tenant_id` — derives via `installation_id → installations.tenant_id`, already covered by the 030 backfill of installations (7 rows). Active token population: 7 `service_account` tokens, 0 user PATs. Cutover revocation is small.
2. **Re-keying GDX off the placeholder UUID `a1b2c3d4-…`** — multi-system migration (every audit row, R2 prefix, JWT history). File as separate D-item only if/when a load-bearing case appears.
3. **Other parallel sub-architecture conflicts** — `tenant_id` vs `company_id` naming, audit shape, soft-delete patterns. The lint-gate pattern works for any of them; D97 only covered identity-shape.
4. ~~CI / pre-commit coverage gap~~ — **closed an earlier session**: both `tenant_scoping_scan` and `tenant_id_shape_scan` now run as a CI step in `.github/workflows/ci.yml` ("Tenant-isolation lint gates"). Local pre-commit hook is the fast-feedback path; CI is the durable enforcement that doesn't depend on hook installation.

---

## Phase 1 — Env-switch sequence (original Session-43 plan; runs after Phase 0)

### 1. Land the grants migration (no behaviour change yet)
*(Already done an earlier session — migrations 027 + 028 deployed. Step kept here for reference / re-runnability.)*


Migration `027_grant_gdx_app_rls_tables.py` grants `gdx_app`:
- `USAGE` on schema `public`
- `USAGE` on every existing sequence + default privilege for future sequences
- `SELECT, INSERT, UPDATE, DELETE` on the 25 RLS-protected tables (canonical list in `control_plane_rls_targets.py` + `commerce_plane_rls_targets.py`)

Idempotent (`GRANT` is re-runnable). `gdx_app` connection user does not change yet — app keeps working as `gdx`.

```bash
# verify the migration is at head locally first
.venv/bin/alembic -c gdx_dispatch/alembic.ini heads

# deploy normally — migration runs as part of /deploy
/deploy
```

After deploy, on prod:

```bash
ssh your-server "docker exec docker-control-db-1 psql -U gdx -d gdx_control -c \\
  \"SELECT count(*) FROM information_schema.role_table_grants WHERE grantee='gdx_app';\""
# Expect ≥ 100 (25 tables × 4 privileges = 100)
```

### 2. Confirm `gdx_app` role attributes

```bash
ssh your-server "docker exec docker-control-db-1 psql -U gdx -d gdx_control -c \\
  \"SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'gdx_app';\""
# Expect: gdx_app | f | f
```

If `gdx_app` doesn't have a password set:

```bash
ssh your-server "docker exec docker-control-db-1 psql -U gdx -d gdx_control -c \\
  \"ALTER ROLE gdx_app WITH LOGIN PASSWORD '<NEW_PASSWORD>';\""
```

(Pick a strong random password and store it in the same secrets store as the existing `gdx` password.)

### 3. Flip CONTROL_DATABASE_URL on docker-app-1

This is the load-bearing step. Edit the docker compose env (or whatever
controls docker-app-1 environment) and change:

```
# BEFORE
CONTROL_DATABASE_URL=postgresql://gdx:<GDX_PASS>@control-db:5432/gdx_control

# AFTER
CONTROL_DATABASE_URL=postgresql://gdx_app:<GDX_APP_PASS>@control-db:5432/gdx_control
```

Then restart docker-app-1:

```bash
ssh your-server "cd <compose-dir> && docker compose up -d app"
```

Watch the container logs for the first 30s — any `permission denied` is a missed grant; either roll back the env change or land an additional grant before continuing.

### 4. Verify fail-closed live

```bash
ssh your-server "docker exec docker-app-1 python gdx_dispatch/tools/verify_rls_failclosed.py"
# Expect:
#   connected as: gdx_app (super=False bypassrls=False)
#   PASS: all 25 RLS-protected tables returned 0 rows with no GUC AND with a bogus GUC. RLS is fail-closed.
```

Also smoke-test a real request — `GET /api/health`, log in, GET /api/jobs — they should all work. The app behaviour is unchanged because `get_control_db` already sets `app.tenant_id` per request and the policies see the real tenant.

### 5. (separate D-item, NOT in this fix) REVOKE PUBLIC SELECT

F4 audit also noted `PUBLIC` has SELECT on 188 tables in gdx_control. That's belt-AND-suspenders — RLS still gates rows, but a defensive REVOKE PUBLIC pass closes a class of "any authenticated PG role can read everything" surprises. Plan separately because a wrong REVOKE breaks unrelated tooling.

## Rollback

If anything breaks after step 3:

```bash
# Restore CONTROL_DATABASE_URL back to gdx in docker-app-1 env
# Restart
ssh your-server "cd <compose-dir> && docker compose up -d app"
```

The grants migration (step 1) is harmless to leave in place. The downgrade exists for completeness.

## Why this is a runbook and not a one-shot script

- Step 1 is reversible (REVOKE).
- Step 3 is the load-bearing change (env + restart) and needs to be observable.
- Step 4 is the verification gate — must be green before declaring done.
- Doing this all in one script would risk the env switch landing while step 1 was mid-flight.

## Tested locally

- [x] Migration 027 applies cleanly against the lab control DB.
- [ ] Verification script returns PASS against the lab DB after switching the lab app connection to `gdx_app`.
- [ ] Verification script returns PASS against PROD after Doug-driven deploy.

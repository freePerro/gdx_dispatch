# The n8n Plugin & the Plugin Event Platform (the WordPress model)

**Status:** Plan v6 — audit-hardened, re-audited, QC-passed (2026-08-17).
Sprint 1a cleared to build (GO-WITH-CAVEATS).
**North star (Doug, 2026-08-17):** people should be able to build **custom
plugins for GDX that do this kind of work** — integrations, automation,
reacting to business events. The WordPress model: the platform ships the hook
system, the ecosystem ships the features. n8n is the **flagship plugin** that
proves the model.
**Decisions (Doug):** owner consent is **required** at install for
`events`/`schedules`/`services`; plugins bringing their own container is
wanted (Gap 3, supervisor-agent pattern).

> **v4 changelog — three adversarial audits (truth-vs-code, security, ops).**
> The audits found **7 BLOCKERs** that reshape the build: the delivery core
> the plan meant to *reuse* has correctness bugs, the "six call sites" is really
> ~30, the table work is a real migration, and the plugin platform's security
> rests on network isolation that adding an untrusted-workflow container (n8n)
> breaks.
>
> **v5 changelog — a fourth re-audit of v4 itself (Doug's call).** Caught v4
> folding errors: (1) the n8n↔Postgres "reach db only" exception is
> **inexpressible in Compose and reopens the isolation** — n8n moves to its own
> isolated database (below). (2) Two of v4's "confirmed defects" were **false**
> (the `secret`-length and substring-matching claims); the **real** defect is
> that the webhook **CRUD and emit paths use different tables and are
> disconnected** — a customer-created subscription receives nothing today.
> (3) `emit_domain_event` needs an **`after_commit` listener + `begin_nested`
> savepoint**, not a line-reorder, because the choke points run inside the
> caller's transaction. (4) Consent must store the **event-list preimage** (not
> just a hash) and pair fail-closed with a **loud owner signal**. All folded.
>
> **v6 changelog — QC pass (AgentQuality, GO-WITH-CAVEATS).** (1) **Sprint 1a
> needs NO migration** — keep `events` as Text + `json.loads` in emit, enforce
> required-secret-at-create in code (`subscription_id` already exists as a
> column); encryption of the secret deferred to a later hardening pass. This
> sidesteps the create_all-vs-Alembic idempotency hazard entirely. (2) **Drop
> the natural discriminator** — `subscription_id` in the key + a `begin_nested`
> that swallows the rare duplicate `IntegrityError` (dup-emit → no-op) is
> simpler and correct. (3) **A THIRD table** — the UI Deliveries tab reads
> `webhook_delivery_logs` while async emit writes `webhook_deliveries`; unifying
> only subscriptions leaves real deliveries invisible in the UI. Reconcile both.
> (4) `deliver_webhook` must repoint to `WebhookSubscription`; NULL secret is a
> **hard worker crash** (`sign_payload` does `.encode()` unconditionally) so
> required-secret is correctness, not hardening. (5) Mirror `install_flush_guard`
> (`modules/ledger/guard.py:139-147`) for the listener — one global idempotent
> `Session`-class listener draining `session.info`; `after_commit` may only
> enqueue, never emit SQL. (6) Sprint 3 isolation test must assert **positive**
> paths too (app→db/redis/plugin-host, caddy→app/n8n), not just n8n's negatives.

## The WordPress mapping

| WordPress | GDX today | Gap |
| --- | --- | --- |
| Actions (run code on events) | ❌ nothing — manifest has no event field (`plugin_api/manifest.py:71-81`), no hook registry, plugin-host request-scoped only | **Gap 1** |
| wp-cron (scheduled plugin work) | ❌ plugin-host has no Celery/beat | **Gap 2** |
| Admin pages | ✅ declarative UI screens, host-rendered, no plugin JS (ADR-013) | — |
| Content-surface injection | ✅ `estimate_source`, catalog packs + pricing strategies (ADR-015) | — |
| Plugin install UX | ✅ register package / upload wheel; pip-reconcile + restart | directory later |
| Commercial ecosystem (paid plugins) | ✅ AGPL core + §7 plugin exception (#339) | — |
| Plugins ship their own daemons | ❌ WP has no answer either; precedents are Home Assistant / Umbrel / CasaOS | **Gap 3** |

## Sprint 1 — the emit sprint is really TWO halves (audit F1/F2/F4/F5)

The v3 claim "the emit side is the only dead wire" is **false**. The delivery
core needs a repair pass *in the same sprint* as the wiring. Split accordingly.

### 1a. Delivery-core repair (CODE-ONLY per QC — no migration)

Per the QC pass, Sprint 1a ships as **pure code + tests, zero DDL**. `events`
stays Text (parsed with `json.loads` in emit), the required secret is enforced
at create in code, `subscription_id` already exists on `WebhookDelivery`, and
secret *encryption* is deferred to a later hardening pass (it's HMAC material —
hardening, not correctness). The confirmed defects to fix:

- **THE crux: CRUD and emit use different tables — nothing a customer creates
  ever fires (BLOCKER, re-audit F4-bonus).** The user-facing CRUD + the whole
  `WebhooksView.vue` UI write `webhook_subscriptions` (`routers/webhooks.py:265,277`);
  the emit fan-out reads `webhook_endpoints` (`core/webhooks/tasks.py:47`),
  written only by `core/integrations.py:159`, `api/public_router.py:689`,
  `register_endpoint`. **The two systems are disconnected** — a subscription a
  customer makes in the UI is invisible to emit. Sprint 1's headline ("two
  subscribers both receive") is unreachable until 066 unifies these. This — not
  a type tweak — is the real unification work: **point emit at the same table
  the CRUD/UI write** (or merge the two into one), repoint
  `WebhookDelivery.endpoint_id`, and delete/redirect the dead writers.
- **Fan-out throws on the 2nd receiver (BLOCKER, truth-F1).**
  `emit_webhook`'s idempotency key is `f"{tenant_id}:{event_type}:{entity_id}"`
  (`core/webhooks/tasks.py:54`) — excludes the receiver — and
  `WebhookDelivery.idempotency_key` is `unique=True` (`core/webhooks/models.py:48`).
  Two active receivers for one event → IntegrityError on the second
  `db.flush()`, propagating into the business request (**an estimate accept
  would 500**). **Fix:** key = `{tenant}:{event}:{entity}:{receiver_id}`. The
  "occurrence discriminator" is a **stable natural value** (the status-transition
  id / an entity occurrence counter) — **never `uuid4()`/timestamp**, which
  would void the constraint entirely. Retry re-sends the *same stored key* as
  `X-Idempotency-Key` (`delivery.py:52`), so receiver-side dedupe-on-retry
  survives; we deliberately drop duplicate-*emit* suppression (documented).
- **Dispatch races its own commit → `after_commit` listener (MAJOR, truth-F2 +
  re-audit 5a).** `deliver_webhook_task.delay(id)` fires (`tasks.py:58`) *before*
  `db.commit()` (`tasks.py:60`); the worker can run before the row is visible,
  returns silently (`tasks.py:24`), stranding it `pending`/`next_retry_at=NULL`
  which the sweep skips (`tasks.py:33-37`). **But** the Sprint-1b choke points
  (e.g. `transition_invoice_status`, `service.py:235`) run **inside the caller's
  transaction and never commit** — so "dispatch after commit" is impossible at
  the call site. Correct mechanism: `emit_domain_event` stages the delivery row
  via a **`begin_nested()` SAVEPOINT** (so an insert failure can't poison the
  caller's txn) and registers an SQLAlchemy **`after_commit`** hook that fires
  the Celery task only once the business txn actually commits — phantom-free and
  race-free. Also: the retry sweep rescues `pending` rows with NULL
  `next_retry_at` older than N seconds, and the delivery task gets an explicit
  Celery retry policy (v3's "rides the Celery retry" was fiction).
- **Migration 066 realism (MAJOR, truth-F5 + re-audit 4).** Webhook tables exist
  only via boot `create_all` — **zero Alembic migrations** touch them (head
  065), and they're on `TenantBase` while Alembic's `target_metadata` is
  `control.models` (`migrations/env.py:10,16`), so **066 is hand-written, not
  autogenerated**. `create_all` runs *before* `alembic upgrade` every boot
  (`entrypoint.sh:46-54`), so 066 must be **idempotent against both states**: a
  fresh DB where `create_all` already built new-type columns, and an existing DB
  where it must ALTER — type-check before each `ALTER`. If 066 converts
  `webhook_subscriptions.events` (Text JSON-string) to a JSON column, the
  `USING events::json` cast **fails on any non-JSON row** — validate/guard bad
  rows. (Correcting v4: `webhook_subscriptions.secret` is **plaintext
  `String(200)`, no Fernet** — the encrypted secret is on `webhook_endpoints`,
  already `Text`; and emit does **proper list membership** today, not substring
  — substring only appears if we repoint emit at the Text column *without*
  converting. These are 066 hazards, not pre-existing bugs.) If unification
  starts encrypting the surviving `secret` column, `tests/test_pii_encryption_status.py`
  must update; reads tolerate mixed state via the InvalidToken passthrough
  (`core/pii.py:108-128`).
- **SSRF via redirects + secret required (security-F5/F7, truth-F9).**
  Both delivery paths validate the initial URL then hand it to `urlopen`, which
  **follows 301/302/307/308 without re-validating** (`core/webhooks/delivery.py:28-33`,
  `routers/webhooks.py:393-395`) — a 307 to `http://plugin-host:8000/internal/...`
  or `http://169.254.169.254/...` sails past the guard. **Fix:** a redirect
  handler that re-runs `validate_outbound_url` on every hop (or refuses
  redirects). Also validate at **subscription create** (`routers/webhooks.py:265`,
  no validation today), not just send. And make the surviving `secret`
  **required** (auto-mint on create if absent) so `deliver_webhook`'s
  unconditional `sign_payload(payload, endpoint.secret)` (`delivery.py:51`)
  can't crash on NULL and every PII delivery is signed.

Kill the now-dead duplicate layer (truth-F8): `core/webhook_delivery.py`
(`deliver_webhook_event`, `ping_endpoint` — the latter POSTs with no SSRF
guard) and `core/webhooks/monitor.py` become lies-in-code after unification.

### 1b. Emit at the RIGHT choke points (truth-F4 — the real inventory)

v3 said "six call sites." The verified inventory is ~30 producers with **no
shared services**. Emit via one tiny helper `emit_domain_event(event, entity_id,
payload, db)` (wraps fixed `emit_webhook` + the Sprint-2 plugin fan-out) placed
at these points — a choke point where one exists, else every enumerated site:

- **`invoice.paid` — genuine choke point.** All live payment paths converge on
  `transition_invoice_status(db, invoice, "paid")` (`modules/ledger/service.py:235`).
  Emit there once. ⚠ **Importer/backfill suppression:** the outstanding office
  Phase-2 QB backfill ($24.8k) runs through the *instrumented* record-payment
  UI → would emit `invoice.paid` for years-old invoices. Add a suppression flag
  (`emit=False` / a request-scoped "backfill" context) so backfills stay silent.
  QB *sync* writes `status="paid"` directly (`modules/quickbooks/sync.py:779/829/932`),
  bypassing the choke point — free exclusion.
- **`estimate.accepted` / `.declined` — 8 sites, partial existing helper.**
  office `routers/estimates.py:2001/2104`, portal `routers/portal.py:1184/1290`,
  public proposal `modules/proposals/router.py:~449/572`, **mobile truck-side
  `routers/mobile_quoting.py:554/692` (v3 omitted these)**. ⚠ A partial helper
  already exists — `notify_estimate_decision` is called by portal + proposals
  (`portal.py:1229/1327`, `proposals/router.py:457/572`) but **not** by office
  or mobile. A naive `record_estimate_decision` extraction that also notifies
  would **double-fire** on the two surfaces that already notify or **drop** it
  on the two that don't (re-audit 5b). Safest: add only the `emit_domain_event`
  call at each of the 8 sites (no behavior change), rather than a risky
  notify-and-emit extraction — cover all 8, touch nothing else.
- **`job.created` — ~9 producers** incl. `routers/jobs.py:880`,
  `_create_job_from_estimate` (`estimates.py:1844`, hit by office+portal),
  service calls/triggers/templates, children/follow-ups/return visits, beat
  `materialize_due_recurring_jobs`, and a **raw-SQL INSERT** at
  `api/public_router.py:296` (needs its own emit). `onboarding.py:319` importer
  excluded.
- **`job.completed` — ≥5 writers**, two of them generic PATCHes that map any
  status payload → `lifecycle_stage` (`jobs.py:1039-1045`, `public_router.py:337-348`).
  Route completions through one `mark_job_completed` helper so the PATCH paths
  can't leak uninstrumented.
- **`customer.created`** — `customers.py:349`, **lead promote `leads.py:909`
  (v3 omitted)**, `public_router.py:456`. QB/onboarding importers excluded.

Also this sprint: beat entry for `retry_failed_webhooks_task` (`core/scheduler.py`);
scoped SSRF allowance `GDX_WEBHOOK_PRIVATE_ALLOW` (exact-hostname, applied in
validate + deliver + create); versioned envelope `{event, data, occurred_at,
delivery_id}` with **explicit per-event payloads** (no `additionalProperties:
true` passthrough — security-F7; minimize `customer.created` to ids + chosen
fields).

**Sprint 1 is useful standalone:** the moment it lands, any Zapier/Make/curl
receiver gets real signed events — no n8n required.

**Sprint 1b build status (2026-08-17).** All six events now fire from their
primary sites, guarded (`emit_domain_event` never raises into the business
write) and suppressible (`suppress_domain_events()` contextvar for backfills):
- ✅ `invoice.paid` — `transition_invoice_status` (`modules/ledger/service.py`),
  the single choke point covering office record-payment, Stripe, mobile,
  deposits; QB sync bypasses it (free suppression).
- ✅ `estimate.accepted` / `.declined` — office (`routers/estimates.py`).
- ✅ `customer.created` — `routers/customers.py` (PII-minimized: id+name+type).
- ✅ `job.created` / `job.completed` — `routers/jobs.py` create + `/complete`.
- **Remaining entry points (same proven pattern, follow-up):** estimate
  decisions via portal (`routers/portal.py`), public proposals
  (`modules/proposals/router.py`), mobile (`routers/mobile_quoting.py`);
  `customer.created` via lead-promote (`routers/leads.py`) and public API
  (`api/public_router.py`); secondary `job.created`/`job.completed` producers
  (service calls, recurring, closeout, mobile, the generic status PATCH).

## Sprint 2 — platform (Gaps 1+2) with the security model fixed

The three BLOCKERs here share one root cause (security-F1/F3/F4): **the plugin
platform's entire internal security rests on network isolation, and every
`/internal/*` route has zero auth** (`plugin_host/app.py` — bare FastAPI). Adding
n8n (untrusted tenant workflows) to the flat network makes those routes directly
callable. Three controls, all required (each backstops the others):

- **`GDX_INTERNAL_TOKEN` on every `/internal/*` route.** Minted, persisted to
  `gdx_secrets`, injected into app + plugin-host (**not** n8n). plugin-host
  middleware rejects any `/internal/*` without it (401). This also retro-hardens
  the existing `/internal/restart` and `/internal/browser/credentials` routes,
  which are unauthenticated today.
- **Core enumerates consented recipients from a STORED list, not a hash
  (security-F3, truth-F3, re-audit 3).** v3's "compromised host can't
  self-grant" was security theater: under a broadcast envelope, plugin-host
  decides per-plugin delivery. Fix: core reads `plugin_consent` (core DB), finds
  the consented plugins wanting this event, and POSTs `/internal/events` with an
  explicit recipient list. **But core never imports plugin code** (only
  plugin-host loads plugins), so it can only learn a plugin's event list from
  plugin-host's `/api/plugins` catalog — the distrusted party. A hash lets you
  *verify* but not *enumerate*. So `plugin_consent` must **store the actual
  consented `(events, schedules, services)` tuple** (the fingerprint's preimage)
  — core enumerates recipients from that stored value, and uses the live catalog
  **only** to detect drift. Then "core enumerates" is literally true and
  plugin-host is fully out of the routing decision.
- **Consent fingerprint + a LOUD signal on drift (security-F6, truth-F7,
  re-audit 2d).** `plugin_consent` stores comma-joined permission *names* — it
  can't express "re-prompt when the event list changes." Fingerprint =
  hash of the serialized `(events, schedules-with-cron-strings, services)` —
  **serialized names/crons, not the callable objects**. Dispatch only when the
  live manifest fingerprint == the consented one; any drift → fail-closed until
  re-consent. ⚠ **Footgun (re-audit 2d):** a routine plugin upgrade that
  adds/reorders an event flips the fingerprint and **silently stops all that
  plugin's automation** (the dogfood "estimate → SMS" just goes quiet). So
  fail-closed **must** be paired with a loud owner-facing signal — an admin
  banner / the office bell — the instant a mismatch suppresses dispatch.
  `services` is a **separately grantable** permission (not one all-or-nothing
  grant). `fetch_permissions` + the `/api/plugins` payload must surface
  `events`/`schedules` (today they don't — `plugin_host/app.py:90-102`); the 60s
  catalog cache reshapes to carry them (truth-F6).

**Manifest additions** (`plugin_api/manifest.py`):

```python
PluginManifest(
    key="n8n", ...,
    events=("estimate.accepted", "invoice.paid", "job.*"),  # names + prefix.* + "*"
    event_handler=handle_event,        # callable(PluginEvent) -> None
    schedules=(("poll_health", "*/5 * * * *", poll_health),),
)
```

Dispatch: `emit_domain_event` → core fan-out → a Celery task POSTs the signed
envelope and recipient list to plugin-host `/internal/events` (reserved route, token-gated).
plugin-host dispatches per-plugin, try/except isolated ("degrade, don't die"),
**at-least-once / unordered / 30s budget — handlers must be idempotent** on
`delivery_id`. Schedules: one core-beat task (every minute) reads the catalog,
finds due schedules, POSTs token-gated `/internal/schedule/{key}/{name}`.

**Sprint 2 build status (2026-08-17) — the EVENT path is built + tested.**
- ✅ Manifest gains `events` / `event_handler` / `schedules`, each consent-gated
  (declaring them requires the matching permission + a valid handler/shape).
- ✅ `plugin_api/events.py`: `PluginEvent`, wildcard `event_matches` (exact /
  `prefix.*` / `*`), `capability_fingerprint` (names only).
- ✅ plugin-host: `GDX_INTERNAL_TOKEN` middleware on `/internal/*` (staged —
  enforced only when the token is set, retro-hardening restart + browser creds);
  `/internal/events` dispatch (recipient list from core, re-checks the pattern,
  per-plugin isolation); `/api/plugins` exposes events + schedule names.
- ✅ Core: consent stores the event-list **preimage + fingerprint**;
  `event_recipients()` enumerates from the stored preimage (+ a defense-in-depth
  `events`-permission check) and **fail-closes on drift** (plugin changed its
  declared events → not delivered). Drift signal is v1-honest: an ERROR log
  (→ Sentry when configured) + a throttled `plugin_consent_drift` record — an
  owner-facing banner/bell that reads it is Sprint-2b (not yet a UI signal).
  `deliver_plugin_event_task` (Celery, bounded retry on transient plugin-host
  downtime) POSTs to plugin-host with the token; `emit_domain_event` gained a
  **read-only** plugin sink (never commits the caller's txn — a bug the test
  caught) that stages a dispatch on the same `after_commit`.
  **Delivery guarantee is best-effort / at-least-once ONCE ENQUEUED** — there is
  no durable per-plugin ledger, so a broker-down-at-enqueue or retry-exhausted
  event is dropped with a log (reliability-critical automations use the durable
  tenant-webhook path; a plugin ledger is a documented open question). The WS
  `/internal/browser/ws` is token-gated INLINE (http middleware doesn't cover
  websocket scope — an audit-caught hole).
- ✅ `GDX_INTERNAL_TOKEN` minted (`mint_runtime_env.py`); existing `/internal/*`
  callers send it; consent DDL made portable (`CURRENT_TIMESTAMP`).
- 27 event/consent tests + 205-test plugin/webhook regression batch green.
- **Follow-up (Sprint 2b):** the `schedules` driver (`/internal/schedule/{key}/{name}`
  + the beat task) and the frontend consent UI for events/schedules /
  re-consent-on-drift; full runtime E2E needs a plugin-host image rebuild.

## Sprint 3 — compose (audit rewrote most of this)

### Network isolation (security-F1/F3/F4 — the highest-leverage change)

Today **no compose file has a `networks:` block** — flat bridge, everything
name-resolvable. n8n runs **untrusted tenant workflows** (HTTP/Redis/Postgres/
Code nodes) and must **not** reach `db`, `redis`, or `plugin-host`. Define
networks: a `backend` net (db, redis, app, plugin-host, celery, beat, backup)
and an `edge` net (caddy, app). n8n joins a dedicated `automation` net whose
only peers are **caddy** (ingress) and **app** (for `gdx_live_` API traffic).
**No route to backend — this is absolute** (re-audit F1: Compose network
membership is all-or-nothing, so there is no "reach db only" — joining backend
to reach Postgres would also expose Redis and plugin-host). Redis gets
`--requirepass` from a minted secret n8n can't read anyway (security-F3:
passwordless Redis is the Celery broker; n8n's Redis/Code nodes would inject
tasks into DB-credentialed workers). `REDIS_URL` gains the password, derived
into `runtime.env` by the minter (wins over compose env, same as `DATABASE_URL`).

### Caddy: generate the config, don't ship a file (ops-1/2, BLOCKER)

A static Caddyfile crash-loops the zero-env boot (empty `GDX_DOMAIN` →
invalid block → restart loop; the `{$VAR:default}` escape does **not** cover
set-but-empty, verified) and can't exist on Docker Manager (one file only).
**Fix:** extend the existing `sh -c` in the caddy `command` to *generate* a
Caddyfile then `exec caddy run --config`. Three branches:
no domain → `:80` site; domain → domain site; domain **AND explicit
`GDX_N8N_ENABLED=1`** → append the `n8n.$GDX_DOMAIN` site. The explicit env gate
(not empty-domain detection) also fixes ACME: the n8n block exists only after
its second A record does, so Caddy never burns Let's Encrypt failed-validation
limits (ops-2). Caddy must **never** `depends_on` n8n — an active service
depending on a profile-inactive one is a hard compose error (ops-6);
`reverse_proxy` resolves the upstream at request time and 502s harmlessly.

### n8n service (ops-3/4/5/6/7 — many corrections)

- **Encryption key — `N8N_ENCRYPTION_KEY_FILE`, dedicated volume (ops-3, BLOCKER).**
  `MANAGED_SECRETS`→`runtime.env` never reaches n8n (it doesn't run our
  entrypoint). n8n reads `${ENV}_FILE` for every config var, so: secrets-init
  writes the key to a **separate `gdx_n8n_secrets` volume** (NOT `gdx_secrets`
  wholesale — n8n's Code/Execute nodes would read every GDX secret), **no
  trailing newline** (n8n uses it untrimmed), and n8n mounts only that with
  `N8N_ENCRYPTION_KEY_FILE`. ⚠ **One-way door:** decide key ownership before the
  first shipped release — if v0.1 self-mints and a managed key is introduced
  later, n8n crash-loops on `Mismatching encryption keys`. Chosen: managed from
  day one (dump-only restore needs it).
- **n8n gets its OWN Postgres on the automation net (re-audit F1 — corrects
  ops-5).** ops-5's "point n8n at the shared `db`" is **rejected**: it's
  inexpressible without joining n8n to the backend net (all-or-nothing
  membership), and the stack has a single `gdx` superuser (`mint_runtime_env.py`
  only ever mints one role), so an n8n Postgres node would get **full access to
  the business database**. Instead: a dedicated `n8n-db` (`postgres:16-alpine`,
  own minted password, own `gdx_n8n_db_data` volume) **on the automation net
  only** — isolated *and* `pg_dump`-able. The backup sidecar joins automation
  too and adds one `pg_dump -h n8n-db` line. This keeps the isolation the sprint
  calls its highest-leverage change while still giving dump-based restore.
  (Rejected alt: SQLite on a volume — also isolated, but WAL-corruption on live
  copy + no `sqlite3` in the postgres sidecar image + the no-egress apk-add boot
  hang we've been burned by. The own-Postgres path is isolated *and* clean.)
- **Env block (ops-4):** `N8N_HOST=n8n.${GDX_DOMAIN}`, `N8N_PROTOCOL=https`,
  `N8N_EDITOR_BASE_URL=https://n8n.${GDX_DOMAIN}/`, webhook URL to match;
  **`N8N_RUNNERS_ENABLED=true`** (else Code nodes run un-isolated in-process —
  unacceptable next to the DB); `mem_limit: 512m` (ops-8 — n8n would otherwise
  be the only uncapped service on the box). Named volume ownership is fine
  (auto `node:node`, verified — no init container needed).
- **Profile gating in the BASE compose too (ops-7).** `profiles: ["automation"]`
  in `docker-compose.yml` (which *is* the dev compose) keeps dev untouched; the
  VPS enables via `COMPOSE_PROFILES=automation` in `.env` (honored by
  `--env-file`, which is exactly `update.sh`'s invocation — zero update.sh
  changes). n8n `depends_on: secrets-init: service_completed_successfully`
  (profile-less, always active — legal) so first boot can't race the key file.
- Gate the automation offering on **KVM 2+** in the onboarding form (ops-8 —
  KVM 1's 4 GB already grazes the ceiling; n8n makes the OOM killer the
  scheduler).

### Backup bycatch — fix a live prod bug (ops-5)

`gdx_secrets` is **not** backed up today, so the nightly `pg_dump` is
**unreadable after restore** without `MASTER_ENCRYPTION_KEY`/Fernet keys for
every `EncryptedString` column. Add a line to the sidecar copying `runtime.env`
into `/backups`. This is a pre-existing disaster-recovery hole the n8n work
surfaced — worth shipping on its own.

## Sprint 4 — the flagship: `gdx-plugin-n8n`

A real pip plugin, key `n8n`, built ONLY on the public plugin API (if it needs
private hooks, Sprint 2 isn't done):

- **`events=("*",)`** — forwards every domain event to the tenant's n8n via a
  per-tenant configured target (HMAC-signed, always).
- **`settings` screen**: n8n base URL (prefilled from `GDX_N8N_URL`), n8n API
  key, event filter.
- **`list` screen**: the tenant's n8n workflows + status, read live from n8n's
  REST API — automations visible inside GDX.
- **`schedules`**: 5-min n8n health poll (proves Gap 2; surfaces "n8n
  unreachable").
- **`help` screen**: recipes. Stretch: one-click recipe install via n8n's API.
- Inbound (n8n → GDX): existing `gdx_live_` API key + `/api/v1`; extend
  `VALID_SCOPES`/routes as recipes demand.

## Gap 3 — runtime services (a plugin that brings its own container)

The WordPress-plus endgame; a later, security-sensitive sprint (supervisor
agent, socket-proxy, digest-pinned allowlist, consent-gated). Deferred design
in the "Gap 3" appendix below; adversarial audit mandatory before it ships.
Audit pre-flagged (security-F8, ops-9): the Caddy admin API the agent reloads
must sit on a dedicated caddy↔agent network, never the shared bridge; the
image allowlist must match **digest not repo name**; volume names need strict
charset validation; the agent's reconcile-trigger channel needs the same minted
token + isolation as `/internal/*`.

## Licensing (verified 2026-08-17)

n8n Sustainable Use License: each customer's own instance on their own VPS =
compliant; we pull the official image, don't redistribute. Never
white-label/iframe n8n's UI (paid Embed license) or central-host workflows for
customers (Enterprise). GDX plugin exception (`PLUGIN-EXCEPTION.md`) already
permits proprietary plugins on the public API; `events`/`schedules`/`services`
join that API's semver'd compat contract.

## Build order (revised)

1. **Sprint 1a — delivery-core repair** (migration 066, idempotency, dispatch-
   after-commit, redirect SSRF guard, required secret, kill dead layer). Fully
   unit-testable; fixes real bugs regardless of n8n.
2. **Sprint 1b — emit wiring** at the audited choke points + importer
   suppression + envelope + beat retry. Deliverable: a signed `estimate.accepted`
   POST reaches a webhook.site URL; a QB backfill emits nothing; two subscribers
   both receive.
3. **Sprint 2 — platform**: manifest `events`/`schedules`, `GDX_INTERNAL_TOKEN`,
   `/internal/events` + schedule driver with core-enumerated recipients, consent
   fingerprint, catalog payload reshape. Contract tests.
4. **Sprint 3 — compose**: network isolation (n8n on `automation` net only),
   redis password, generated Caddyfile, n8n service with its **own isolated
   `n8n-db`**, `N8N_ENCRYPTION_KEY_FILE` on a dedicated volume, profile gating,
   secrets backup fix. Local zero-env boot test **asserting isolation**: from
   the n8n container, `redis:6379` / `plugin-host:8000` / `db:5432` must be
   unreachable, `n8n-db:5432` reachable.
5. **Sprint 4 — `gdx-plugin-n8n` v0.1**, installed via the normal admin flow.
6. **Dogfood on our VPS** — one real workflow (estimate accepted → SMS to Doug;
   also answers the after-hours office-bell question), a week.
7. **Developer guide + example-plugin update** — written against shipped code.
8. **Customer path** — staging `COMPOSE_PROFILES` test → onboarding runbook (2nd
   A record, KVM 2+ gate, "want automation?" question) → recipes doc.
9. **Gap 3** (traction-gated) + **`n8n-nodes-gdxdispatch`** npm community node.

Deploy-gated steps (6, 8's staging, and any real-VPS E2E) are **Doug-side** —
they need a release cut and live infra. Everything through Sprint 5 is
buildable + locally testable now.

## Open questions

- Event wildcard grammar: `"*"` + `"prefix.*"` (adopted) vs exact-only.
- Per-plugin *delivery ledger* (visible retries like tenant webhooks) or
  logged-failure enough for v1?
- ~~Consent UX~~ **DECIDED: required at install**; re-prompt on fingerprint
  change.
- `COMPOSE_PROFILES` through Hostinger Docker Manager (staging test) — the
  Caddy gate env (`GDX_N8N_ENABLED`) is independent of profile mechanics so the
  always-on fallback can't re-break the bare-IP boot.
- `webhook_endpoints` fate: fold into migration 066 (recommended) vs leave
  dormant.
- Gap 3 specifics (socket-proxy choice, resource ceilings, agent control
  channel) — its own audit.

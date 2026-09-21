# Cell Comms — Android texts & calls into GDX via Nomad Gateway, as a plugin

**Date:** 2026-09-18
**Status:** MERGED #752 (2026-09-20 04:22Z). Core keeps only the webhook shim (`routers/cell_gateway.py`, its compose variable, its tests); the plugin package moved to `gdx_dispatch_plugins` (its PR #TBD; core removal PR #TBD). The moved plugin declares `requires = "gdx>=1.122.0"`, so the core PR that passes `APP_VERSION` to plugin-host must ship in v1.122.0 and reach prod BEFORE the plugins repo is tagged — its storefront card lands with that tag. Store-all ingest + XML backfill in one build per Doug's ruling. Owed before prod use: AVD nomad-payload verification, browser walk, the three deploy checks in the plugin README, a local stack for the browser walk with `APP_VERSION=1.122.0` set (an unpinned `dev`/`latest` host reads as version 0 and skips a pinned plugin), and a ruling on the shim's 404 pass-through — with the secret set and the plugin not yet installed, every text/call is answered 404 with no log line and no audit row (found by the 2026-09-20 audit; the shim is unchanged, the case is pinned in `tests/test_cell_gateway.py`).
**Decision (Doug, 2026-09-20):** the package lives in `gdx_dispatch_plugins`, like every operator-installable plugin (plugin-storefront plan, archived: `gdx-plugin-eventlog` is the one in-tree dev reference). #752 built it in-tree without checking that record — nothing in core packages `plugins/`, so it was not installable from anywhere.
**Decision (Doug, 2026-09-18):** get his Android cell's texts and call log into the app; packaging preference is a plugin; forwarder choice is android-nomad-gateway (chosen after the incoming-only limitation was flagged).

---

## What already exists (do not rebuild)

- **`gdx_dispatch/modules/phone_com/`** — full ingest of business-line calls/SMS/voicemail (`phone_com_calls`, `phone_com_messages`), webhook routers, customer resolution, UI. This plan covers **only the personal Android cell**, a different line; phone_com is the *template* (column shapes, resolver semantics), never the destination.
- **The shared-secret public-webhook pattern** — `core/inbound_email_auth.py`: fail-closed (`403` when the secret env is unset in prod), constant-time compare, `X-GDX-Webhook-Secret`-style header. The cell webhook mirrors this, it does not reinvent it.
- **The plugin surface** — manifest/discovery (`plugin_api/`), `plug_<key>_*` table convention on `PluginBase.metadata` with boot-time `schema_reconcile`, `get_plugin_db` (session on the **shared** DB, so core tables like `customers` are queryable), `type: list` screens (bare JSON array, never `{items: [...]}`), and `gdx-plugin-eventlog` as the reference implementation.
- **Number→customer matching** — `phone_com/customer_resolver.py`: `normalize_e164` (phonenumbers, US default) → `HashColumn.hash_for_search` (sha256 over `SEARCH_HASH_SALT` + lowercased E.164) → `Customer.phone_hash`, LIMIT 1.

**Rival/adjacent plans checked** (grep of `docs/design/` incl. `archive/`, 2026-09-18):
- `sms-caller-identity-plan.md` (PLAN, 2026-07-28) — proposes one shared resolver across all phone surfaces, including `CustomerContact` numbers, plus retro re-resolution. Adjacent, not rival: this plugin becomes another consumer of number→customer matching. When either plan builds, cross-reference the other (both-docs rule).
- `twilio-removal-plan.md` (RELEASED v1.118.0) — why no carrier-SMS ingest lives in core routers today ("Phone.com owns SMS"). This plan deliberately keeps the new SMS surface **out of core** except the ~60-line webhook shim.

## Upstream facts (read 2026-09-18)

Source: https://github.com/we-digital/android-nomad-gateway (README; MIT; distributed as a sideloaded APK from GitHub releases — not on Play Store, which also sidesteps Google's SMS/call-log permission policy).

- Forwards **incoming SMS, incoming call events, and push notifications** as HTTP POST with a **user-defined JSON template**. Template variables — SMS: `%from%`, `%text%`, `%sentStamp%`, `%receivedStamp%`, `%sim%`; calls: `%from%`, `%contact%`, `%timestamp%`, `%duration%` (0 at ring time).
- **Custom request headers supported** → a shared-secret header works.
- HTTPS supported; failed posts retry with exponential backoff (up to 10, configurable).
- It does **not** forward outgoing SMS, outgoing calls, or pre-existing history.

**Stated plainly:** phase 1 delivers a **live incoming-only feed**, not a full two-direction call log / SMS history. Phase 2 (optional, Q2): a batch upload of SMS Backup & Restore XML (`sms.xml`/`calls.xml`, both directions, full history — format reference: https://shruggietech.github.io/sms-backup-restore-parser/xml-reference/) into the **same tables**; the dedupe key below is designed so both feeds can coexist.

## Why a core shim is unavoidable

1. Plugin routes are reachable **only** through the core proxy, which requires an authenticated user (`routers/plugins_proxy.py`; `plugin_api/context.py` 400s without the forwarded tenant header).
2. The PAT/service-account token surface was **deleted** (fails closed since 2026-08-12, `routers/auth/core.py`) — no long-lived bearer a phone can hold.
3. The `services` plugin permission promises "its own web address," but no host-side code publishing a plugin service **publicly** was found in this repo — unproven, and a whole extra container to receive one POST is the wrong trade anyway.

So: the phone cannot POST to the plugin directly. A ~60-line core webhook shim authenticates the device by shared secret and relays inward. Everything else — storage, matching, screens — is plugin.

## Architecture

```
Android cell (nomad gateway app)
  └─ POST https://gdx.teamgaragedoor.com/api/cell-gateway/webhook
     header: X-GDX-Cell-Secret            body: templated JSON (below)
        └─ core shim (new small router, public):
           verify secret (mirror inbound_email_auth: fail-closed, compare_digest),
           cap body size, then relay to
           plugin-host /api/plugins/cellcomms/ingest with the same
           server-authoritative forwarded headers plugins_proxy sets
              └─ gdx-plugin-cellcomms: validate → dedupe → store → match customer
```

Nomad payload templates (configured on the phone; documented in the plugin README):

```json
{"kind":"sms","from":"%from%","text":"%text%","sentStamp":"%sentStamp%","receivedStamp":"%receivedStamp%","sim":"%sim%"}
{"kind":"call","from":"%from%","contact":"%contact%","timestamp":"%timestamp%","duration":"%duration%"}
```

## Plugin data model (`plug_cellcomms_*`, PluginBase.metadata)

- `plug_cellcomms_messages`: `id`, `direction` (`in` now; `out` reserved for phase 2), `other_number`, `body` (Text — plaintext matches the `phone_com_messages.body` precedent), `sim`, `sent_at`, `received_at`, `customer_id` (plain `Uuid`, **no FK constraint** — keeps the plugin decoupled from core metadata, matches the spirit of phone_com's string refs), `dedupe_key` (unique: sha256 of `direction|e164|sent_ts|body`), `raw_payload` JSON, `created_at`.
- `plug_cellcomms_calls`: `id`, `direction`, `other_number`, `contact_name`, `started_at`, `duration_s`, `customer_id`, `dedupe_key` (unique: `direction|e164|started_ts`), `raw_payload`, `created_at`.

The dedupe keys are derived from message-intrinsic fields (not nomad delivery ids) precisely so a phase-2 XML backfill upserts into the same rows without duplicates.

## Customer matching

Same semantics as `phone_com/customer_resolver.py`: normalize to E.164, `sha256(SEARCH_HASH_SALT + lower(e164))`, match `Customer.phone_hash`. The plugin **copies** the ~30-line normalize+hash rather than importing `modules.phone_com` (ADR-013: a plugin importing core internals makes them public API).

- **Build-time verification (load-bearing):** confirm the plugin-host container resolves the **same `SEARCH_HASH_SALT`** as the app container on prod (`os.getenv` default is `""`; it appears in no compose file — check `runtime.env`). A mismatched salt makes every match silently miss, which looks exactly like "no customers text this phone."
- Unmatched rows get a **retro re-match pass**, event-driven: the plugin
  subscribes to `customer.created`/`customer.updated` and reruns matching on
  delivery, plus after every backfill upload, plus a manual "Re-match" button.
  (Build finding, 2026-09-18: the manifest `schedules` field is validated and
  catalogued but **no runner executes it** — a declared schedule is a silent
  no-op today, so the original nightly-cron design was swapped for events,
  which demonstrably deliver. Second finding: `customer.updated` is a known
  event name that nothing emits — the manual button covers phone-number edits
  until it does.) The caller-identity plan documents the "customer created
  after the message" hole; this closes it for cellcomms.
- If `sms-caller-identity-plan.md` ships its shared resolver first, the plugin's copy should be re-pointed at whatever `plugin_api` helper that work exposes; note it in both docs.

## UI

Plugin screens — **Texts** and **Calls** (`type: list`, server-side search),
**Import phone backup** (`type: upload`), and a **Help** screen with the phone
setup. Nav category `customers` (where the Phone.com surfaces live; the
`communications` nav entry was removed 2026-08-31 #350), icon `pi pi-mobile`.
Rows show matched customer name, else the bare number (the caller-identity
plan owns making bare numbers nameable). Endpoints return bare arrays.

**Build addition (2026-09-18):** the host renderer had no file-input screen
type — without one the backfill has no visible way in. `PluginScreen.vue` +
`usePluginScreen.js` gained a generic `upload` screen type (multipart POST,
field name `file`, same same-plugin endpoint guard as every other manifest
endpoint, renders the server's summary, optional `secondary_action` button —
used here for manual re-match). Any plugin can now declare one.

## Security / hard-rule check

- Unauthenticated reachability is **by design** and secret-gated, fail-closed (unset secret in prod ⇒ 403, exactly like inbound email). Write-only endpoint — no reads, so no ID/token enumeration surface.
- Body size cap (64 KB) on the shim; rate limiting is the app's existing
  global middleware, not a shim-local limiter (build substitution, recorded
  per 2026-09-18 audit). Never log bodies or numbers (PII — follow
  customer_resolver's length-only debug logging).
- Ingest writes `log_audit_event` with a webhook actor identity, mirroring whatever the inbound-email webhook records (verify at build) — no silent writes.
- Public repo: the phone number, the secret, and any real message content stay out of code, tests, and docs.

## Verification plan

1. **Real end-to-end without the personal phone:** sideload the nomad APK on the local Pixel 8 AVD, point its webhook at `http://10.0.2.2:<port>` (throwaway stack), then drive **real** incoming events from the emulator console — `adb emu sms send <num> <text>` and `adb emu gsm call <num>`. Nomad fires a genuine forward; assert rows, dedupe on redelivery, and customer match against a seeded customer.
2. Full test matrix (`run_tests_split.sh`, N=7, no `--network host`), skips enumerated with `-rs`; lint ratchet vs baseline.
3. Browser walk of both screens in the throwaway container, light and dark, desktop and mobile.
4. After deploy: configure the real phone against prod, receive one real text and one real call, see both rows with the right customer — that walk is the finish line.

## Build deltas vs this plan (2026-09-18)

- Shared-secret gate factored to `core/webhook_auth.py`; `inbound_email_auth`
  delegates (two consumers, one gate — no drifting copy). Behavior pinned by
  the existing inbound-comms tests, which still pass.
- `docker-compose.yml` env is an **allowlist** — `CELL_GATEWAY_WEBHOOK_SECRET`
  added there and to the customer compose, blank default (= gate off,
  fail-closed).
- Calls: a backfilled call **enriches** its ring-time webhook row (real
  duration, missed/rejected outcome) instead of skipping as a duplicate; the
  call log is the authority on outcome.
- Retro-match is event-driven (see Customer matching) — not a schedule.
- 2026-09-18 adversarial audit round 1 found (fixed, falsifier-pinned tests):
  two calls from one number inside the ±300s window enriched the same webhook
  row, erasing the missed call; and two distinct texts with unreadable stamps
  hashed to one dedupe key. Near-dup matching now ranks by |stamp delta|, a
  live row absorbs at most one backup element per import, and unparseable
  stamps fall back to the raw stamp string in the key.
- Round 2 (same day) found four more, all fixed with falsifier-pinned tests:
  (1) enrichment discarded the backup element's key, so re-uploading a
  cumulative backup re-imported every enriched call — `CellCall.backfill_key`
  now persists it; (2) the in-file duplicate defense relied on autoflush, but
  prod's session is `autoflush=False` — an in-file duplicate 500'd the whole
  import (test fixtures had masked this with autoflush=True; they now mirror
  prod); (3) per-element queries put a realistic 50k-element file at 47s
  against the core proxy's 30s timeout — the backfill now does a fixed number
  of preload SELECTs plus one per distinct number, pinned by a query-count
  test, never per-element; (4) rematch claimed to refresh customer names and
  didn't — it now links unlinked rows AND refreshes name snapshots
  (`names_refreshed` in the response), while deliberately never re-attributing
  a linked row to a different customer.
- Round 3 (same day) verified rounds 1–2 held, then caught the residual
  scaling hole: customer matching was memoized per DISTINCT number, and a
  call log is distinct-heavy (2004 SELECTs measured on 2000 distinct numbers)
  — backfill now reuses rematch's one-query whole-table customer map, zero
  queries per element or number, and the query-count test uses all-distinct
  numbers (its earlier `i % 3` version structurally couldn't fail for this).
  Also from round 3: prod nginx `client_max_body_size` must be checked
  against the 100 MB upload cap at deploy (README step 5).
- The ±300s window itself is still guesswork until a REAL nomad payload is
  observed (AVD verification step 1) — the audit is right that one live event
  pins nomad's stamp semantics; do it before trusting cross-feed dedupe on
  prod data.
- No plugin FK columns onto core tables at all (not even string refs):
  `customer_id` is a canonical dashed-UUID string, `customer_name` a display
  snapshot refreshed by rematch. Sidesteps the SQLite dashless-Uuid raw-join
  trap.

## Rollback

The shim is inert with the secret env unset (fail-closed 403). Plugin uninstall removes routes and screens. Plugin tables carry no core FKs and can be dropped independently if Doug rules the data dead (delete-means-delete).

## Ruled (Doug, 2026-09-18)

1. **Ingest scope: store all.** Every incoming text/call is kept; retro-match links rows to customers created later. Personal correspondence sits in the DB and surfaces only in the plugin screens.
2. **Backfill: build both now.** The SMS Backup & Restore XML upload (both directions + full history) ships in the same build as the live feed. The upload lands as an authenticated plugin route (multipart/raw XML through the core proxy — verify pass-through at build), parsing `sms.xml` and `calls.xml` into the same tables via the same dedupe keys; backfilled rows carry `direction` `in`/`out` as recorded by the phone.

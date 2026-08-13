# SimpleFIN Bank Feed — Build Plan

Status: **built** (branch `feat/simplefin-bank-feed`; §3 scope complete, adversarially
audited; remaining user-side step = §7)
Date: 2026-08-13

## 1. Context

The institution declined to provision a Banno external application, closing the direct
Consumer-API path the existing `bank_feeds` module was built for. Replacement:
**SimpleFIN Bridge** (bridge.simplefin.org) — $1.50/mo or $15/yr, read-only, rides MX,
which has an official Jack Henry aggregator connection into Banno. The institution does
not need to cooperate; the accountholder consents with their own online-banking login at
the bridge. The institution is confirmed present in the bridge's supported list, and the
full protocol (claim → Access URL → `GET /accounts`) has been exercised live against the
bridge's demo server.

The Banno code stays dormant, not deleted — both providers coexist behind a dispatch.

## 2. Protocol facts the design leans on

- One credential: the **Access URL** (basic-auth embedded), obtained by POSTing to the
  base64-decoded **setup token**. The claim is **one-time-use**; a 403 on claim means the
  token was already used (possibly intercepted).
- One data call: `GET {access}/accounts?version=2` returns *all* accounts + transactions
  in a single response. Params: `start-date`/`end-date` (epoch, **max 90-day window**),
  `account=` filter, `balances-only=1`. Pending txns excluded unless `pending=1`.
- Transaction `id` is **unique per account** (spec guarantee) → clean upsert key.
- Structured `errlist`: `con.auth` (user must re-auth at the bridge), `act.failed`,
  `act.missingdata` ("incomplete listing, try again later"). Spec *requires* apps to
  show these messages to the user.
- Bridge quota: **≤24 requests/day** (warn, then token disabled). Guidance: overlap fetch
  windows ~5 days; fetch at an off-peak minute (not top of the hour).
- Payload extras beyond spec minimum: `payee`, `memo`, `mcc`, `transacted_at`.
- No document/statement PDFs. No `updatedSince`-style mutation feed. No running balance.

## 3. Decided scope

### Placement & settings (Settings → Integrations card)
- SimpleFIN lives as a card on the **Settings → Integrations** tab (beside Outlook /
  Stripe), NOT inside the Bank Feeds view. The Bank Feeds tabs keep rendering the data.
- **Sync frequency** setting, hard-capped at **20 fetches/day** (headroom under the
  bridge's 24; the cap must never creep to 24).
- **Fetch-hours window** setting (only fetch between HH:MM–HH:MM), interpreted in
  **tenant-local time** (`AppSettings.timezone` / `useTenantTimezone`), converted per
  dispatch so DST follows the wall clock. The daily cap counts per local calendar day.
- **Quota ledger** on the card: today's usage (n/20), counting scheduled AND manual
  Sync-Now together; Sync Now disabled at the cap.
- **Backfill progress indicator** for the initial history pull.

### Duplicate safety
- **Posted-only ingest** — never pass `pending=1`. A pending txn can post under a
  different id (phantom-row duplicate); posted-only is dup-safe by construction with the
  `(account_id, external_transaction_id)` unique constraint + overlapping windows.
- **Reconnect is a re-link, never a blind create.** A re-auth or fresh setup token can
  present the same accounts under new external ids. On claim, match incoming accounts to
  existing feed accounts by name/mask and require user confirmation of the mapping before
  writing. Soft-disconnect (rows kept, cursors survive) as with Banno.

### Correctness & failure handling
- **Claim transactionality**: persist the claimed Access URL before any subsequent step
  can fail. 403 on claim → surface "token burned/compromised — disable it at the bridge".
- **Credential-safe HTTP**: parse the Access URL once; store host/path and the auth pair
  separately (auth encrypted at rest, Fernet like the Banno tokens); always request with
  an explicit auth parameter so no logged URL/exception/Sentry event ever contains
  credentials. Redaction test required.
- **Watermark discipline**: on `act.failed` / `act.missingdata` for an account, do NOT
  advance that account's synced-through marker (otherwise txns are skipped forever).
- **Error surfacing**: `errlist` messages shown verbatim (sanitized) via the sync-health
  banner pattern; **staleness alarm** (banner + dashboard nag) when no successful fetch
  for 48h.
- **Feed never writes books**: read-only visibility + suggestions only. Statement PDF
  imports remain THE reconcile evidence; feed and statement lines stay separate planes.

### Promoted features (in scope)
- **Daily balance-snapshot history**: each fetch returns `balance`/`available-balance`/
  `balance-date`; store a snapshot row per account per fetch-day instead of only
  overwriting the account row → cash curve for the dashboard later.
- **Monthly feed↔statement tie-out report**: after a statement import, compare statement
  lines against feed transactions for the same account/period (±1-day date tolerance —
  epoch→local-date conversion can differ from the statement's posting date). The two
  sources should agree; discrepancies are an error/fraud signal.
- **Backfill progress indicator** (also listed under the card above).

## 4. Architecture

The existing module map (verified 2026-08-13): the `provider` column on
`bank_feed_accounts` is write-only future-proofing read by nobody; Banno is hardcoded
end-to-end; the single sync chokepoint is `_sync_one_institution`
(`modules/bank_feeds/tasks.py`). No NOT NULL constraint blocks a non-OAuth provider.

Build pieces:

1. **Provider dispatch** in `_sync_one_institution`: `institution.provider` →
   client/sync adapter. New `provider` column on `banno_institutions` (needs a small
   migration — the feed tables are ORM-created with no Alembic history).
2. **`SimpleFINClient`** (~100 lines): claim-once + a single authenticated
   `GET /accounts` per sync; 90-day-window backfill loop with 5-day overlap; off-peak
   minute; timeouts/retry consistent with `BannoClient`; SSRF-validated host.
3. **Normalizer** into the existing tables (`bank_feed_accounts`,
   `bank_feed_transactions`): `posted` epoch → posted_date (tenant-local),
   `amount` string → cents, `description`/`payee`/`memo` (+`mcc` into extra/memo),
   external ids as above, `provider="simplefin"` finally written and read.
   Connection row reuses `banno_connections` with synthesized `banno_user_id`
   (e.g. the SimpleFIN `conn_id`) and `fi_host` = bridge host; Access-URL auth in
   `access_token_enc`. (FK rename deferred; documented naming debt.)
4. **Connect flow**: Integrations card dialog — link to the bridge's `/create` page,
   paste-setup-token field, claim on submit, then the re-link/confirm step (§3).
5. **Balance snapshot table** + tie-out report endpoint + card UI (settings, quota
   ledger, backfill progress, sync health).

Scheduling: the existing 5-minute beat dispatcher + `bank_feed_sync_schedule` singleton
drive cadence; the hours-window and daily-cap checks gate at dispatch time.

## 5. Constraints & non-goals

- Out of scope: pending transactions, investment `holdings`, statement/document download
  via the bridge (does not exist), any auto-creation of book records.
- History depth per institution is unknown until the first real backfill; the statement
  archive covers the past regardless.
- Whether the bridge's single institution entry accepts the business online-banking login
  is proven only by connecting at the bridge (user-side step, zero code).

## 6. Deploy notes

- VPS container egress is gated by the origin firewall — allow the bridge host from the
  app + celery containers before first sync (same drill as the Garden sandbox).
- One small migration (provider column; balance-snapshot table).
- `bank_feeds` module grant already enabled on prod.

## 7. User-side steps (no code dependency)

1. Create a bridge account ($15/yr), connect the institution via the MX widget with the
   real online-banking login + MFA (the app never sees credentials).
2. When the integration ships: generate a setup token at the bridge, paste into the
   Integrations card.

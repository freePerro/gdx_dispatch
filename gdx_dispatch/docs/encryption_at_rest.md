# Encryption at Rest — GDX / DispatchApp

**Audience:** auditors (SOC 2 / HIPAA questionnaires), engineering, ops.
**Owner:** Doug (founder) — direct questions to `owner@example.com`.
**Last reviewed:** 2026-05-12.

This document describes the encryption-at-rest controls protecting customer
data on the DispatchApp platform. It is the canonical reference for
auditor questions; everything below maps to specific code paths and operational
procedures.

---

## TL;DR

| Data class | Encryption | Algorithm | Key |
|---|---|---|---|
| **Tenant databases (host-level disk)** | Provider-dependent (see Layer 1 below) | — | — |
| **Database backups** (`/var/backups/gdx/*.dump.gpg`) | Yes | OpenPGP / RSA-4096 + AES-256 (GPG default) | `gdx-backup@example.com` — public key on VPS, private key off-machine |
| **`customers.address`** | Yes | Fernet (AES-128-CBC + HMAC-SHA256, IV per record) | HKDF-SHA256 of `MASTER_ENCRYPTION_KEY` with tenant_id salt |
| **`integration_configs.secret`** | Yes | Fernet (as above) | (as above) |
| **`webhook_endpoints.secret`** | Yes | Fernet (as above) | (as above) |
| **QuickBooks OAuth refresh + access tokens** (`qb_token_store.*_enc`) | Yes | Fernet (as above) | (as above) |
| **Customer names, emails, phones** | No (intentional) | n/a | See [What is NOT encrypted](#what-is-not-encrypted-and-why) below |
| **Outlook / phone.com / other integration tokens** | Per-integration; see source | Fernet | (as above) |

All ciphertext-at-rest uses AES-256 or AES-128 with HMAC integrity per
2026 SOC 2 guidance ([Sprinto SOC 2 Requirements 2026][soc2-sprinto],
[SOC 2 Encryption Standards][soc2-encryption]).

---

## Threat model — what the controls defend against

**In scope:**

1. **Exfiltrated backup files.** Daily pg_dump backups in
   `/var/backups/gdx/*.dump.gpg` are individually GPG-encrypted; the private
   key never sits on the VPS, so a VPS compromise that exfiltrates 30 days
   of dumps yields opaque OpenPGP blobs to the attacker.
2. **Read-only Postgres compromise.** A read-only DB credential (e.g., the
   AI read-only role `gdx_ai_readonly`) leaks ciphertext, not plaintext, for
   the four Fernet-encrypted column classes above. The plaintext only ever
   exists inside the application process holding `MASTER_ENCRYPTION_KEY`.
3. **Lost or compromised QuickBooks refresh tokens.** Refresh tokens are
   Fernet-encrypted at rest. A stolen backup file does not yield usable QB
   credentials without the Fernet key (held in the app process's env, not in
   the DB).

**Out of scope (no claim made):**

- **Raw filesystem capture of the running VPS** (e.g., hypervisor compromise
  reading the live volume) — the guest is not LUKS-encrypted; see Layer 1
  below. Field-level encryption mitigates the highest-sensitivity columns
  but plaintext columns (name, email, phone, free-form notes) would leak.
- Live RAM exfiltration from a running app process. The Fernet key is in
  the process memory by design.
- Application-layer SQL injection that runs under the privileged role. SQLi
  is mitigated by ORM-only access patterns + pre-commit scans
  (`gdx_dispatch/tools/raw_sql_on_encrypted_columns_scan.py`), not by field
  encryption.
- Compromise of Doug's workstation. The backup private key lives there;
  workstation-side controls (FDE, screen lock, OS user account) are the
  boundary.

---

## Implementation

### 1. Disk-level encryption — current state and gap

**Current state (2026-05-12 audit):** The VPS root volume (`/dev/sda1`) is
formatted as plain `ext4`, not LUKS / dm-crypt. `lsblk -f` shows
`cloudimg-rootfs` with no `crypto_LUKS` layer. Any host-level encryption
that exists is at the hypervisor / storage-fabric layer of the cloud
provider and is not visible from inside the guest. **We do not currently
make a verifiable claim of guest-level disk encryption.**

This is the highest-leverage remaining gap in the layered-control story.
Three mitigations are in place that reduce the impact of the missing layer:

1. **Field-level encryption** on the highest-sensitivity columns (Fernet,
   §3 below) means a raw disk image still yields ciphertext for those
   columns.
2. **Backup encryption** (GPG, §2 below) means the backup-exfil scenario
   doesn't leak plaintext even though the running DB files do.
3. **OS-level access controls.** `/var/lib/postgresql`, `/var/backups/gdx`,
   and `/opt/gdx_dispatch/.env` are all `0600`/`0700` `root:root` or
   container-user-owned. A compromise that yields plain-user shell does
   not reach the DB files.

**Filed as `D-disk-encryption-rebuild`** in the sprint file: rebuild the
VPS on an image with LUKS-on-data-volume, or migrate to a provider that
offers verifiable customer-managed disk encryption.

**Evidence:** `ssh your-server "lsblk -f"` — confirms current state.

### 2. Database backup encryption — GPG / OpenPGP

Activated 2026-05-12. The two backup cron jobs (lines 31–32 in root's
crontab) stream `pg_dump -Fc` output through `gpg --batch --encrypt
--recipient gdx-backup@example.com --compress-algo none` and write
`.dump.gpg` files.

- **Recipient key:** `GDX Backup <gdx-backup@example.com>`,
  fingerprint **`4C4A6F362D8751E91CC6964B50E68132B1D2F631`**, RSA-4096
  encrypt-only, expires 2031-05-12 (5y rotation cadence).
- **Public key on VPS** (in root's GPG keyring, ultimately trusted) —
  encrypt-only operations require nothing else.
- **Private key OFF the VPS**, on Doug's workstation at
  `~/.gdx-backup-keys/gdx-backup-private.asc` (mode 0600) and
  imported into `~/.gnupg/`. **A copy must live off-machine** (USB drive
  + password-manager secure note); without it, ALL encrypted backups are
  unreadable.
- **Pipefail guard:** the cron line wraps the `pg_dump | gpg` pipeline in
  `bash -c 'set -o pipefail; …'`. If `pg_dump` fails mid-stream, the cron
  exits non-zero AND does not produce a `.dump.gpg` file — preventing the
  silent-partial-encrypt class of bug.
- **No double-compression:** `--compress-algo none` because `pg_dump -Fc`
  already gzip-compresses internally; double-compressing wastes CPU.

**Historical backups:** 124 plaintext dumps that existed in
`/var/backups/gdx/` before 2026-05-12 were re-encrypted in place + plaintext
removed only after gpg packet-structure verify + size sanity. Three random
samples were downloaded + decrypted + `pg_restore --list`'d successfully.
Zero plaintext `.dump` / `.sql` / `.sql.gz` files remain.

**Restore procedure** is documented under [Operational procedures](#operational-procedures).

### 3. Field-level encryption — Fernet

Three columns on three different tables use the `EncryptedString` SQLAlchemy
TypeDecorator defined in `gdx_dispatch/core/pii.py`:

| Plane | Table | Column | Purpose |
|---|---|---|---|
| Tenant | `customers` | `address` | Customer service address (PII) |
| Tenant | `integration_configs` | `secret` | Per-tenant integration API secrets |
| Control | `webhook_endpoints` | `secret` | Webhook signing secrets |

Plus one set of manually-encrypted columns:

| Plane | Table | Columns | Purpose |
|---|---|---|---|
| Tenant | `qb_token_store` | `access_token_enc`, `refresh_token_enc` | QuickBooks OAuth tokens |

**Why manual instead of `EncryptedString` for QBToken?** The token-refresh
flow needs to distinguish "first-read of a pre-encryption legacy row that
contains plaintext" from "second-read where decryption failed for real". A
TypeDecorator can't make that distinction; the manual `_decrypt` helper in
`gdx_dispatch/modules/quickbooks/oauth.py:68` falls back to plaintext-passthrough
with a deduplicated warning, mirroring the precedent at
`gdx_dispatch.core.database._decrypt_db_url`.

**Algorithm:** [Fernet][fernet-spec] — AES-128-CBC + HMAC-SHA256, random IV
per record, version byte for migrations. Each ciphertext starts with the
ASCII bytes `gAAAAA` — the canonical Fernet token prefix. Drift detection
queries (`COUNT(*) WHERE col NOT LIKE 'gAAAAA%'`) catch any plaintext that
slips past the contract.

**Key derivation** (`gdx_dispatch/core/pii.py:13–28`):

```
HKDF(
    algorithm=SHA256,
    length=32,
    salt=$TENANT_ID,           # per-tenant salt
    info=b"gdx-pii-v1",        # version label for future rotation
).derive($MASTER_ENCRYPTION_KEY)
```

The 32-byte derived key is base64-url-encoded and passed to
`cryptography.fernet.Fernet()`. The `tenant_salt_fp` (first 8 hex of
SHA-256 of the tenant salt) is exposed at the boot gate so an auditor can
confirm a non-default salt without reading the raw env.

**Key source:** `MASTER_ENCRYPTION_KEY` is set in the VPS environment file
`/opt/gdx_dispatch/.env`, owned `root:root`, mode `0600`. It is never committed
to source. App processes read it at boot.

### 4. Tenant isolation as a complement to encryption

Three-plane isolation (`ARCHITECTURAL_STATE.md`) means:

- **Tenant plane** — per-tenant Postgres database. Isolation is the
  connection itself; cross-tenant queries are physically impossible.
- **Control plane** — single `gdx_control` DB with RLS policies referencing
  `current_setting('app.tenant_id')`. Every `tenant_id` column is `NOT
  NULL` with `WITH CHECK` policies preventing forged writes.
- **Commerce plane** — RLS keyed on either `supplier_tenant_id` or
  `dealer_tenant_id`.

Encryption is *additive* to isolation, not a replacement. RLS keeps tenant A
from reading tenant B's plaintext via SQL; field encryption keeps a
ciphertext leak (backup theft, raw filesystem access) from yielding
plaintext.

### 5. AI-access guardrails

Three independent layers gate AI access ([CLAUDE.md][claude-md] §"AI Access
— Triple-Layer Safety"):

1. **Tool layer.** Narrow typed functions only; no free SQL to AI.
2. **Postgres role layer.** Read-only AI uses `gdx_ai_readonly`
   (`SELECT`-only). Write-capable AI uses `gdx_ai_write` with explicit
   column grants — never `ALL PRIVILEGES`.
3. **RLS layer.** Same RLS policies as human users; AI sees only its
   tenant's rows.

Encrypted columns flow through the same ORM path AI tools call, so AI
output sees plaintext only for the columns its role + tool exposes. A
write-capable AI cannot smuggle ciphertext to a different tenant because
RLS `WITH CHECK` blocks the write.

---

## What is NOT encrypted, and why

| Column | Why plaintext |
|---|---|
| `customers.name` | Search predicate (LIKE `%doe%`, trigram index). Sidecar-search architectures (tsvector + Bloom filter) exist but cost weeks for a use case auditors do not flag and customers do not contract for. Disk-level + RLS + access controls are the boundary. |
| `customers.email` | Same as above; also used as unique key for the QuickBooks-duplicate-detect logic. |
| `customers.phone` | Same; also exposed to QB sync and SMS reminders. |
| `invoices.line_items.description` | Free-form business text; volume too high (~10× customer count) for field encryption without query-shape regressions. |
| `jobs.notes`, `jobs.address` (where stored at the job level rather than customer level) | Same shape — business logic frequently filters/sorts on these. |

The S122-1b incident (2026-05-12 morning) is the canonical lesson for why
encryption on a column whose readers are heterogeneous is *more* dangerous
than no encryption: raw-SQL routers had been bypassing
`process_result_value` for months, customer pages rendered ciphertext as
soon as the boot gate flipped, and a 45-minute prod outage followed. The
columns we *do* encrypt today have an audited, ORM-only read surface
pinned by `gdx_dispatch/tests/test_pii_typedecorator_raw_sql.py`. Expanding the
scope requires the same audit; we won't do it casually.

A future contract requiring encrypted PII search will be handled by a
trigram-Bloom-filter sidecar (Shiftan 2024, 10–20 ms / 1M rows) rather
than CipherStash Proxy, which is over-engineered for our scale. Decision
recorded as `D-S122-9-customer-search-encryption` in the sprint file.

---

## Key management

### Lifecycle

| Step | Owner | Cadence | Procedure |
|---|---|---|---|
| Generation | Doug | One-time / on rotation | GPG batch-key (backup) / cryptographically random base64-32 (`MASTER_ENCRYPTION_KEY`) |
| Distribution | Doug | One-time | VPS `.env` (Fernet key) / public key import (GPG) |
| Storage | Doug | Continuous | VPS env file `0600 root:root` (Fernet) / `~/.gnupg/` + USB backup (GPG private) |
| Rotation | Doug | 12 months (Fernet), 5 years (GPG) | See [Rotation procedure](#fernet-key-rotation) |
| Revocation | Doug | On compromise | Revocation cert at `~/.gdx-backup-keys/gdx-backup-revoke.asc` (GPG) / new key + re-encrypt sweep (Fernet) |
| Destruction | Doug | On retirement | `shred` of old key files |

### Fernet key rotation

The `EncryptedString` TypeDecorator and the manual `_encrypt`/`_decrypt`
helpers in QB OAuth handle one active key at a time. Rotating requires a
re-encryption sweep, which today is gated on the
`sprint_encryption_rollout_proper.md` plan. Because no live customer
contract has demanded rotation since the keys were generated, this is
deferred and not on a periodic schedule.

If a key is suspected compromised, the runbook is:

1. Generate a new `MASTER_ENCRYPTION_KEY`.
2. Set `MASTER_ENCRYPTION_KEY_OLD=$OLD` and `MASTER_ENCRYPTION_KEY=$NEW`
   in the app env temporarily.
3. Run the re-encrypt tool (`gdx_dispatch/tools/rotate_pii_keys.py` — to be
   written; pattern documented at
   `ai-queue/plans/sprint_encryption_rollout_proper.md`).
4. Drop the OLD env var. Restart.

### GPG backup key rotation

Replace the keypair when:

- Approaching the 2031-05-12 expiry (annual reminder in calendar).
- Doug's workstation is replaced.
- The private key is suspected compromised (immediate).

Procedure:

1. Generate new keypair on Doug's workstation (same batch params as the
   2026-05-12 generation; see
   `memory/reference_backup_encryption.md`).
2. Export public key, import on VPS root keyring, set ultimate trust.
3. Update cron lines 31/32 to use the new recipient email.
4. Optionally re-encrypt existing `.dump.gpg` files under the new key
   (decrypt + re-encrypt loop).
5. Publish the revocation cert for the old key.

---

## Operational procedures

### Backup restore (the procedure that proves the design works)

```bash
# 1. SCP encrypted dump from VPS to Doug's workstation
scp your-server:/var/backups/gdx/gdx_xdg_YYYYMMDD.dump.gpg ./

# 2. Decrypt (private key required)
gpg --batch --decrypt gdx_xdg_YYYYMMDD.dump.gpg > gdx_xdg_YYYYMMDD.dump

# 3. Verify TOC integrity
pg_restore --list gdx_xdg_YYYYMMDD.dump | head

# 4. Restore into a target DB
pg_restore -d <target_db> --clean --if-exists --no-owner --no-privileges gdx_xdg_YYYYMMDD.dump
```

This procedure was exercised against three random historical backups on
2026-05-12 (immediately after the encryption sweep) and yielded valid
`PostgreSQL custom database dump v1.15-0` files with intact TOC entries.

### Decrypt drill (recommended cadence: monthly)

Pick one random `.dump.gpg`, run steps 1–3 of the restore procedure, log
the result. Catches: key file drift on workstation, GPG binary version
incompatibilities, accidental key revocation. Today this is manual;
automating to a quarterly cron is filed under
`D-S122-9-decrypt-drill-cron`.

### Drift detection

- **Boot gate** (`gdx_dispatch/app.py:1262-1294`): on every app start, scans
  `EncryptedString`-typed columns and reports `tenant_salt_fp` +
  encrypted-column inventory to the structured log. A mismatch is
  loud-fail.
- **PII allowlist** (`gdx_dispatch/core/pii.py:encryption_status()`): single source
  of truth for boot gate, SOC 2 evidence collector, and the
  `tenant_schema_drift_check.py` test. Pre-S122-1c the four surfaces drifted
  apart; today they all consume `encryption_status()`.
- **Raw-SQL bypass scan**: pre-commit hook
  (`gdx_dispatch/tools/raw_sql_on_encrypted_columns_scan.py`) blocks any new
  `text("INSERT INTO customers …")` or `text("SELECT … FROM customers")`
  that touches an `EncryptedString` column. The S122-1b incident root
  cause; gate exists to ensure it cannot recur silently.

### Evidence locations for SOC 2 questionnaires

| Auditor question | Evidence path |
|---|---|
| "Is data at rest encrypted?" | This document |
| "What algorithm?" | This document table + `gdx_dispatch/core/pii.py:18` |
| "Show the encrypted-column inventory" | Run `python -c "from gdx_dispatch.core.pii import encryption_status; print(encryption_status())"` against prod env |
| "Show backups are encrypted" | `ssh your-server "ls /var/backups/gdx/*.dump.gpg | head"` and `file /var/backups/gdx/gdx_xdg_*.dump.gpg` (returns `PGP RSA encrypted session key`) |
| "Show the restore procedure works" | This document § Backup restore; last drill 2026-05-12 |
| "Who has key access?" | Doug, single principal. `~/.gnupg/` + `/opt/gdx_dispatch/.env` are root:root 0600. |
| "Key rotation policy?" | This document § Key management |

---

## Change log

| Date | Change | Driver |
|---|---|---|
| 2026-05-12 | GPG-encrypted database backups activated; 124 historical plaintext dumps re-encrypted in place. | S122-9 pick-list item (one remaining (d)-class gap per threat model) |
| 2026-05-12 | This document created; Layer 1 (disk encryption) corrected to reflect actual state (`ext4`, not LUKS) and `D-disk-encryption-rebuild` filed. | S122-9 pick-list item 2 |
| 2026-05-12 | `customers.address`, `integration_configs.secret`, `webhook_endpoints.secret` activated under `EncryptedString` (S122-1c, slice 3). | Threat-model research |
| 2026-05-12 | S122-1b boot-gate activation rolled back same day (raw-SQL bypass class on `Customer.{name,email,phone}`); Option A rewrite shipped (those four columns flipped to `Text`). | S122-1b → S122-1c |
| 2026-04 | `MASTER_ENCRYPTION_KEY` boot gate added; refuse-to-boot if unset on prod. | S122-1 |
| pre-2026-04 | Fernet `EncryptedString` TypeDecorator defined but inert (key never set on prod). | Design theater; corrected by S122-1 |

---

## References

- [SOC 2 Requirements 2026 (Sprinto)][soc2-sprinto]
- [SOC 2 Encryption Requirements (SecurityDocs)][soc2-encryption]
- [Fernet specification][fernet-spec]
- [PostgreSQL Row-Level Security overview][rls-cockroach]
- [Nango — QuickBooks OAuth invalid_grant and refresh-token race conditions][nango-qb]
- [CLAUDE.md — GDX development instructions and architecture summary][claude-md]
- `gdx_dispatch/core/pii.py` — implementation
- `gdx_dispatch/modules/quickbooks/oauth.py` — manual encrypted columns
- `ARCHITECTURAL_STATE.md` — three-plane isolation reference
- `memory/reference_backup_encryption.md` — key locations + restore cheat-sheet

[soc2-sprinto]: https://sprinto.com/blog/soc-2-requirements/
[soc2-encryption]: https://security-docs.com/blog/soc2-encryption-standards
[fernet-spec]: https://github.com/fernet/spec/blob/master/Spec.md
[rls-cockroach]: https://www.cockroachlabs.com/docs/stable/row-level-security
[nango-qb]: https://www.nango.dev/blog/quickbooks-oauth-refresh-token-invalid-grant
[claude-md]: ../../CLAUDE.md

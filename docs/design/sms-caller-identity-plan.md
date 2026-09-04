# Naming the Number — Caller Identity for SMS, Calls, Voicemail & Fax

**Date:** 2026-07-28 · **Status:** PLAN — awaiting Doug's go-ahead
**Symptom:** SMS threads show a bare phone number with no name. Same for the Calls list, voicemails and faxes.
**Decision (Doug, 2026-07-28):** naming a number can either **link it to a customer** (as the account, or as a person at that account) **or record it as a standalone individual** with no customer. Fix applies to **all phone surfaces**, not SMS alone.

---

## Why the number is bare today

One resolver, one column, one moment:

- [customer_resolver.py:64-80](../../gdx_dispatch/modules/phone_com/customer_resolver.py#L64-L80) — `match_phone_to_customer` normalizes to E.164, hashes, and queries **`Customer.phone_hash` only**, `LIMIT 1`.
- [upserts.py:167-170, 291-296, 383-388](../../gdx_dispatch/modules/phone_com/upserts.py#L291-L296) — that resolver runs **only at ingest**, and only `if row.customer_id is None`.
- [router.py:699-708](../../gdx_dispatch/modules/phone_com/router.py#L699-L708) → [PhoneComMessagesView.vue:29](../../gdx_dispatch/frontend/src/views/PhoneComMessagesView.vue#L29) — display is `customer_name || other_party_number || '—'`.

So a number goes unnamed whenever **any** of these is true:

| Cause | Example |
|---|---|
| The person isn't the account's one phone number | Wife texts from her cell; the account holds the husband's |
| The person isn't a customer at all | Parts counter, a vendor rep, a tech's personal cell |
| The customer was created *after* the message arrived | Cold-lead texts, you create the account next day — **the old messages stay unlinked forever**, nothing re-resolves |
| `Customer.phone` is blank or unformatted-unparseable | `phone_hash` never got written |

`customers` holds **exactly one phone**. That is the structural half of the bug. The other half is that resolution is a one-shot at ingest with no retro pass.

The `CustomerContact` model already anticipates this fix, in its own docstring ([tenant_models.py:225-229](../../gdx_dispatch/models/tenant_models.py#L225-L229)):

> No `*_hash` sidecars, deliberately: Customer's exist so the email poller and the Phone.com resolver can find a customer from an inbound address/number. **Wiring those lookups to also search contacts is a real improvement and a separate change.**

This is that change.

---

## The shape

**Three identity sources, most-specific wins.** One resolver function, used by ingest *and* by every list endpoint.

```
1. CustomerContact  (a person at an account)   → "Sarah Mills · Mills Residence"
2. Customer         (the account itself)       → "Mills Residence"
3. PhoneDirectory   (a name, no account)       → "Midland parts counter"
4. Phone.com CNAM   (raw_payload, calls only)  → "MILLS S"          [read-only hint, never stored as an identity]
5. bare number
```

Rules:
- A number has **one** identity. Naming it as an individual clears any customer link; linking it to a customer clears the directory row. The writer enforces this — no two-sources-disagree state.
- CNAM is displayed muted/italic as a *hint* on unnamed numbers only. It's carrier data, often wrong, and it is never written into the directory.

### Schema

**A. `customer_contacts.phone_hash`** — new `String(64)`, indexed, nullable. Plus a `@validates("phone")` hook on `CustomerContact` that mirrors `Customer._set_hashes` ([tenant_models.py:194-206](../../gdx_dispatch/models/tenant_models.py#L194-L206)): normalize to E.164 first, *then* hash, so both sides of the comparison agree. Without the normalize step a contact saved as `(612) 555-1234` never matches an inbound `+16125551234`.

**B. New `phone_directory` table** (phone_com module, tenant plane):

| column | type | note |
|---|---|---|
| `id` | uuid PK | |
| `phone_e164` | String(40), unique | canonical form |
| `phone_hash` | String(64), indexed | sha256 of the E.164, same `HashColumn.hash_for_search` as everywhere else |
| `display_name` | String(200) | what shows in the thread list |
| `label` | String(120), null | free text — "vendor", "supplier", "spam", "my brother" |
| `notes` | Text, null | |
| `is_blocked_hint` | Boolean | lets "spam" identities render muted; **does not** block anything (blocking already lives in `blocked-calls`) |
| `created_by` / `created_at` / `updated_at` | | |

Deliberately **not** a `customer_id` column: a directory row is by definition the no-customer case, and a nullable FK there would immediately create the two-sources-of-truth state rule #1 exists to prevent.

**C. No new columns on `phone_com_messages` / `_calls` / `_voicemails` / `_faxes`.** `customer_id` already exists on all four and already drives downstream linkage (outbound DID stickiness, send attribution). The *person* name is resolved by hash at read time in one batched query — cheaper than four migrations and it can never go stale against a renamed contact.

### Resolution API (backend)

```python
# modules/phone_com/identity.py  (new)

@dataclass
class PhoneIdentity:
    e164: str
    display_name: str | None      # "Sarah Mills"
    account_name: str | None      # "Mills Residence"  (None for a directory individual)
    customer_id: UUID | None
    contact_id: str | None
    source: str                   # contact | customer | directory | cnam | none
    label: str | None

def resolve_identity(db, e164) -> PhoneIdentity          # single
def resolve_identities(db, e164s: list[str]) -> dict[str, PhoneIdentity]   # batched
```

`resolve_identities` is **three queries total, regardless of page size** — customers by `phone_hash IN (...)`, live contacts by `phone_hash IN (...)` (`deleted_at IS NULL`), directory by `phone_hash IN (...)`. The current thread list already does an N+1 (`tenant_db.get(Customer, ...)` per row, [router.py:701-703](../../gdx_dispatch/modules/phone_com/router.py#L701-L703)); this replaces it and makes the page *faster*, not slower.

`match_caller_id` keeps its signature and becomes a thin wrapper that now also finds a customer **via a contact**, so ingest starts linking spouses and property managers on its own.

### Write API

```
GET    /api/phone-com/identity?number=+1612XXXXXXX     → current PhoneIdentity (prefills the dialog)
PUT    /api/phone-com/identity                         → set it
DELETE /api/phone-com/identity?number=+1612XXXXXXX     → un-name it
```

`PUT` body, one endpoint / three modes:

| mode | body | effect |
|---|---|---|
| `customer` | `{number, customer_id}` | "this number **is** the account." Links only. Does **not** overwrite `Customer.phone` unless it's blank *and* the caller passes `set_as_primary: true`. |
| `contact` | `{number, customer_id, name, label?}` | creates (or updates) a `CustomerContact` on that account with this phone. This is the spouse / property-manager / tenant case. |
| `individual` | `{number, name, label?, notes?}` | upserts a `phone_directory` row. No customer. |

Every `PUT`/`DELETE` runs the **backfill** (below), is idempotent, and writes an audit row (`phone_identity_set` / `_cleared`) — naming a number is a customer-data edit and should be traceable to a user.

Creating a *new* customer from a thread is **not** a fourth mode: the dialog links out to the existing `/customers/new?phone=…` path the Cold Leads page already uses ([PhoneComColdLeadsView.vue:72-76](../../gdx_dispatch/frontend/src/views/PhoneComColdLeadsView.vue#L72-L76)), then re-opens on return with the number prefilled.

### Backfill — the part that makes it feel fixed

The single highest-value behavior here: **naming a number rewrites its history.** On every identity write, stamp `customer_id` on all existing rows matching that number across `phone_com_messages` (both `from_number` and `to_number`), `_calls`, `_faxes`, and voicemails via their call. Four bounded `UPDATE`s inside the request; a number with thousands of rows is not a realistic case for a garage-door shop, and if it becomes one this moves to a Celery task.

Without this, you name the number and the thread you're staring at is *still* unnamed — which is exactly today's Cold Leads "Create customer" behavior and why it doesn't feel like it works.

Directory (individual) names need no stamping — they resolve by hash at read time.

**Also needed once:** a `tools/backfill_customer_contact_phone_hash.py`, mirroring the (since deleted, 2026-09-03 — it walked a control-plane tenants table) [backfill_customer_phone_hash.py](../../gdx_dispatch/tools/backfill_customer_phone_hash.py), to hash contacts that already exist (mobile has been writing them since migration 030).

---

## Frontend

**`PhoneComMessagesView.vue`** (the only consumer of `/messages/threads`):

- Thread row: `display_name` bold, `account_name` muted beneath it, number as the fallback. On an unnamed row, the number stays primary with a small **`+ Name`** affordance on hover/focus (always visible on touch).
- Thread pane header: name + account chip, with **Name / Edit** opening the dialog.
- **`PhoneIdentityDialog.vue`** (new, shared): number at the top; customer search (reuses `GET /api/customers/search`, which already matches on stripped phone digits, [customers.py:415-420](../../gdx_dispatch/routers/customers.py#L415-L420)); a "this is a person at that account" name+label pair; a "**Not a customer — just a name**" toggle for the individual mode; and Unname.
- Same dialog wired into **`PhoneComCallsView.vue`**, **`PhoneComColdLeadsView.vue`** (inline "Name this" beside "Create customer"), and **`PhoneComFaxesView.vue`**.

Cold Leads shrinks as a side effect — every named number leaves the list, which is the honest measure of whether this worked.

Dark **and** light mode both checked per the contrast gate; verified headed in a real browser per house rule.

---

## Tests

Backend (`tests/test_phone_com_identity.py` + additions to `test_phone_com_customer_resolver.py`):
- precedence: contact beats customer beats directory; CNAM never overrides a stored identity
- `resolve_identities` is 3 queries for 50 numbers (no N+1) — assert via query counter
- contact `phone_hash` is written on insert **and** on update, and is the hash of the *normalized* E.164 (`(612) 555-1234` matches `+16125551234`)
- soft-deleted contacts (`deleted_at`) never resolve
- backfill stamps historical messages/calls/faxes/voicemails, both directions
- mode switching: `individual` → `customer` deletes the directory row; `customer` → `individual` clears `customer_id`; `DELETE` clears both
- `set_as_primary` never overwrites a non-blank `Customer.phone`
- unparseable number → 400, not a silent no-op
- permission gating (`nav.office`) + audit row written
- **PII:** no raw number in any log line (existing convention, [customer_resolver.py:38-40](../../gdx_dispatch/modules/phone_com/customer_resolver.py#L38-L40))

Frontend (vitest): thread row renders name/account/number in the right precedence; `+ Name` appears only when unnamed; dialog emits the right payload per mode.

---

## Migrations & deploy

- **`customer_contacts.phone_hash`** → migration `040_customer_contact_phone_hash.py`. `customer_contacts` **is** in Alembic (created by [030_customer_contacts.py](../../gdx_dispatch/migrations/versions/030_customer_contacts.py)), so this is a guarded `ADD COLUMN IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`, same `to_regclass` shape as [037](../../gdx_dispatch/migrations/versions/037_notification_delete_sms_read.py).
- **`phone_directory`** → phone_com tables are built by `create_all` at boot, **not** in `baseline_squashed.sql`. A brand-new table needs no migration — but ⚠ per the known plugin-table-drift pattern, **verify on prod after deploy that the table actually exists** rather than assuming.
- Contact-hash backfill tool run once post-deploy.

## Risks / known sharp edges

1. **Two customers, one number.** `match_phone_to_customer` does `.limit(1)` with no `ORDER BY` — today that's nondeterministic, and a shared landline picks a random account. The explicit link must win and must be deterministic; add an `ORDER BY created_at` to the implicit path so at minimum it stops flip-flopping.
2. **`company_id` is NOT NULL on `customer_contacts`** — the identity writer must set it to the tenant id, as the mobile router does ([mobile.py:3738-3748](../../gdx_dispatch/routers/mobile.py#L3738-L3748)).
3. **No raw SQL on these tables** — `phone_hash` is `@validates`-maintained; a raw `UPDATE` writes the value and skips the hash, and the person silently stops matching forever. `tools/raw_sql_on_encrypted_columns_scan.py` already lint-gates this family.
4. **Desktop has no contacts UI at all** — `CustomerContact` CRUD exists only on the mobile job screen. This plan makes the identity dialog the first desktop writer. A proper Contacts section on `CustomerDetailView` is the natural follow-on and is **out of scope here**.
5. **`other_party` derivation** in the thread list is a direction heuristic ([router.py:696-699](../../gdx_dispatch/modules/phone_com/router.py#L696-L699)) — correct today, but it's the input to identity lookup, so it's now load-bearing. Worth an explicit test.

## Sequence

| Step | Size | Ships value |
|---|---|---|
| 1. `phone_hash` on contacts + resolver searches contacts + backfill tool | S | Yes — spouses/PMs start matching with zero UI |
| 2. `identity.py` + batched `resolve_identities` wired into threads/calls/faxes | M | Yes — kills the N+1, surfaces contact names |
| 3. `phone_directory` + the three identity endpoints + history backfill | M | Yes — the actual "name this number" |
| 4. `PhoneIdentityDialog.vue` + wiring into the four views | M | Yes — reachable by the office |

Steps 1–2 are shippable on their own and reduce the unnamed count before any UI exists. 3–4 are the feature.

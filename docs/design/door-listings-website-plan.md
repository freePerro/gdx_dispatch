# Door listings → garagedoorxperts.com

**Goal:** list doors we have in stock, used doors, and doors we can get quickly at a
discount, entered from inside the app, shown on the public website.

**Three submission sources, one approval gate:** office enters and publishes directly;
technicians submit from the field; account customers submit from the portal *only* when
that feature is switched on for the company **and** for that customer (default off for
both). Nothing a tech or customer submits reaches the public site until the office
authorizes it.

Status: **PARTIALLY BUILT** (verified on main 2026-08-21). Phase 1 shipped
(migration 040, `modules/door_listings/`, `routers/door_listings.py`,
`DoorListingsView.vue`); Phase 2 shipped (`listings:read` scope at
`core/api_keys.py:165`, `api/public_router.py:720 GET /listings`); Phase 3
shipped (`MobileDoorListingsView`); Phase 4 shipped
(`app_settings.customer_listings_enabled` + `portal.py:844` customer
submission behind `customer_may_submit`).
**Not built: Phase 5.** No per-door public detail endpoint (only the list),
no `Product`/`Offer` JSON-LD, no "I'm interested" → `/api/lead` wiring, and
no auto-hide-sold-after-N-days (`sold_at` exists; nothing reads it to hide).
Part of Phase 5 lives in the separate website repo, not here.

---

## 1. What already exists (researched, not assumed)

### The website

`/home/doug/Desktop/garagedoorxperts-site` → GitHub `freePerro/garagedoorxperts.com` @ `main`.

- **Astro 5 in SSR mode** (`output: 'server'`, `@astrojs/node` standalone) — renders per
  request on a Node server, so it can fetch live data from the app at page-render time. No
  CMS, no rebuild-on-publish, no static regeneration pipeline.
- **Hostinger** Node.js hosting, auto-deploy on push to `main`, fronted by **Cloudflare**
  (the README already warns the edge serves stale HTML after a deploy).
- **`/used-doors` already exists** (`src/pages/used-doors.astro`), already in desktop and
  mobile nav, currently static "inventory changes all the time — give us a call" copy.
- **The app-integration seam is built and proven in prod.** `src/pages/api/lead.ts` POSTs
  to `/api/v1/landing-leads` with a server-only `X-API-Key` read from `process.env` at
  runtime. We reuse that pattern in the opposite direction.

### The app

- **Public API v1** — `api/public_router.py`, prefix `/api/v1`, `X-API-Key` auth, per-scope
  enforcement via `scope_required()`. Scope allowlist is `VALID_SCOPES` at
  `core/api_keys.py:158`; keys minted with `python -m gdx_dispatch.tools.create_api_key`.
- **Tech mobile console** — `routers/mobile.py`, standard `get_current_user` JWT. Already
  has a job-photo capture endpoint, so field photo capture is a known-good pattern.
- **Customer portal** — `routers/portal.py`, `get_current_portal_customer` returns a
  `PortalPrincipal(user_id, customer_id, role="customer")` from a customer-scoped JWT.
  `portal_booking` and `portal_message` are existing customer→app **write** precedents, so
  a customer-submitted listing is a well-trodden shape, not a new trust boundary.
- **Company settings** — `AppSettings` is a **wide-column** table (`tenant_models.py:41`),
  not key/value. A company toggle is a new column plus an entry in the `patch_settings`
  allowlist (`routers/settings.py:173`), exactly like the existing `debug_logging_enabled`.
- **Permissions** — `require_permission("key")` from `core/modules.py:486`. Admin/owner
  always pass. See §4 for the trap.
- Migrations: Alembic, `migrations/versions/`, latest `039_*`. Ours is `040`.

### Two traps found

**(a) A new permission key does not reach existing roles.** The resolver honors each
tenant's `TenantRole` **snapshot verbatim** — so a snapshotted non-admin builtin role only
gains a new key when its snapshot gains it. This is documented in
`tools/add_leads_perms_to_role_snapshots.py`, written for exactly this reason during the
leads authz sweep. Critically, **that sweep excluded `technician`**. So gating field
submission on a fresh `listings.submit` key without a paired additive snapshot migration
means every tech gets a 403 and it looks like the feature is broken. Ours must include the
migration, and it must include `technician`.

**(b) Mobile photos are written to ephemeral storage and never served.**
`routers/mobile.py:3307` writes to `MOBILE_UPLOAD_DIR`, which `.env.template:241` sets to
`/tmp/gdx_mobile_uploads` — **not** a Docker volume, unlike `/app/uploads` which is the
persisted external volume `docker_gdx_uploads` (`docker-compose.yml:111`). The resulting
`photo_url = /mobile/uploads/job_photos/...` is not mounted or served anywhere in the
codebase. So tech-captured listing photos **must not** reuse the mobile photo path: they'd
vanish on container restart and 404 in the meantime. Listing photos go to `UPLOAD_DIR`.
(The pre-existing mobile job-photo bug is out of scope here, but it's real and worth its
own ticket.)

---

## 2. Data model

New tenant-scoped `door_listings` + `door_listing_photos`. **Not** an extension of
`InventoryItem`: a used door is a single physical unit with photos, marketing copy, and a
sale price — not a stocked SKU with a reorder point. Overloading `InventoryItem` drags PO
receiving, `apply_stock_delta`, and stock-adjustment audit rows into a page whose only job
is to sell one door once.

```text
door_listings
  id, company_id
  listing_type      used | in_stock | quick_ship
  title, slug, description
  status            draft | pending_review | published | rejected | sold | archived
  condition         new | like_new | good | fair
  width_in, height_in
  material, color, insulation_r, has_windows, brand, model
  qty
  featured          office pin — floats to the top of its ownership group
  price, compare_at_price, price_display (fixed | call_for_price)

  -- provenance / moderation
  source            office | tech | customer
  submitted_by_user_id      nullable → users.id     (office + tech)
  submitted_by_customer_id  nullable → customers.id (portal)
  submitted_at
  reviewed_by_user_id, reviewed_at, rejection_reason

  source_job_id     nullable — the tear-out it came from
  published_at, sold_at, created_at, updated_at, deleted_at

door_listing_photos
  id, listing_id, company_id, filename, sort_order, created_at
```

Listing types: `used` = a physical door on the lot (`qty` usually 1); `in_stock` = new, on
the shelf; `quick_ship` = **not** a physical unit, a price story ("we can get this fast at
a discount") — which is why `compare_at_price` and `price_display` exist, since sometimes
the honest answer is "call for price" rather than a number we'd have to defend.

---

## 3. Status model — the heart of it

```text
  office creates ─────────────► draft ──────────► published ──► sold
                                              ▲
  tech submits ───┐                           │ approve
                  ├──► pending_review ────────┤
  customer submits┘                           │ reject
                                              ▼
                                          rejected
```

Invariants, enforced in the router and covered by tests:

- **Only `listings.publish` can move a row into `published`** (§4). Not a PATCH
  on `status`, not a create-with-status. One explicit approve endpoint, one audit row.
- Tech and customer creates are **forced** to `pending_review` server-side, ignoring any
  client-supplied status. The submitter can edit their own row only while it is
  `pending_review` or `rejected`; once published it is the office's.
- Public read returns `published` only, filtered in the query — a `pending_review` door
  must be unreachable even with a valid API key.
- `rejected` keeps `rejection_reason` so the tech sees *why* in the mobile console. A
  rejection with no reason trains people to stop submitting.
- `sold` keeps the row and its history but drops it from the site immediately.

Approvals need to be **fast** or the queue rots: a pending count badge in the sidebar, and
approve/reject inline from the grid without opening a drawer.

---

## 4. Permissions — one real key for the publish boundary

**Revised twice.** Draft 1 invented three keys plus a snapshot-backfill
migration. The first audit called that complexity-over-correctness and pointed
at existing keys instead, so draft 2 gated approval on `inventory.write` +
`nav.office`. **The second audit (against the built code) proved draft 2 was
insecure** — and it was right:

`core/permissions.py:130-138` documents nav.* as *"nav-visibility ONLY (no API
route enforces these)"*, and those keys are editable per-role in the Roles UI.
`technician` is not in `PLATFORM_LOCKED_ROLES`. So an admin adding office
navigation to the technician role — a change that looks purely cosmetic — would
have silently handed every technician the ability to publish to the public
internet. A boundary whose argument expires the first time someone edits a role
is not a boundary.

**Shipped:**

| Action | Gate | Why |
| --- | --- | --- |
| Submit / edit own | `inventory.write` | technicians already hold it (`permissions.py:182`) — nothing to backfill |
| Read the grid | `inventory.read` | same key the nav entry uses, so the page never shows to someone it would 403 |
| **Publish / reject / sold** | **`listings.publish`** | a real authz key that exists for exactly this and nothing else hands out |

`listings.publish` is new, but it needs no snapshot migration: admin/owner
resolve against live `BUILTIN_ROLES`, so they hold it the moment the code
deploys. Every other role must be granted it deliberately — and fail-closed is
the correct default for "can put this on the public internet". If Doug wants
dispatchers approving, that is one checkbox in the Roles UI.

A test pins the *shape* of this, not just today's perm lists: `listings.publish`
must not be in the technician role, must not become reachable by adding
`nav.office`/`nav.admin` to it, and must not appear in any non-admin builtin role.

## 5. The customer-submission toggles

Two independent switches, **AND**-ed, both defaulting off:

1. **Company-wide** — new `AppSettings.customer_listings_enabled` bool column, default
   `False`, `server_default` false, added to the `patch_settings` allowlist and the Settings
   UI. Off means the portal endpoint 404s for everyone, regardless of per-customer flags.
2. **Per-customer** — new `customers.can_submit_listings` bool column, default `False`.
   Toggle on the customer detail page.

Resolution is a single helper used by every portal listing route:

```python
def customer_may_submit_listings(db, customer_id) -> bool:
    return settings.customer_listings_enabled and customer.can_submit_listings
```

Company-off is the master kill switch — one click turns the whole feature off across every
customer without touching per-customer state, so flipping it back on restores exactly the
prior grants rather than re-opening it for everybody. The portal UI hides the entry point
when the helper is false, and the endpoint enforces it again server-side (hiding a button
is not access control).

**One thing worth settling before Phase 3 rather than after:** a customer-submitted door is
a *third party's* door. It's worth being deliberate about whether GDX is reselling it,
brokering it, or just hosting the ad — because that decides whether those listings need
distinct treatment on the public page (a "customer-owned" label, no GDX warranty implied,
maybe contact routed differently) and who handles the money. The build below works either
way; it only changes what the card says. Flagging it, not blocking on it.

---

## 6. Photos

The public site must show door photos and the app has **no public image URL** — every
serve path is behind JWT, there's no S3 and no CDN. Three options considered; the
recommendation is the third:

1. Serve images unauthenticated from the app. Simplest, but opens a brand-new
   unauthenticated file-serving surface on the dispatch host to solve a marketing problem.
2. Push images into the website repo on publish. No — the app is not a build system.
3. **Astro SSR proxies the bytes.** ← recommended.

**Revised after the audit — images must NOT ride the API key.** `core/api_keys.py:376-387`
caps each key at **60 requests/minute**. A page of 20 listings is 20 image requests per
uncached visitor on the same key; that trips the cap and returns 429 *JSON* where an
`<img>` expects bytes. Worse, the §7 fallback only guards the JSON fetch, so the page would
render perfectly with every photo broken. Keyed images were the wrong design.

Instead, **published listing photos are served unauthenticated from the app** at
`GET /public/door-listings/{photo_id}.jpg`:

- The SQL joins photo → listing and requires `status='published'`; a draft or pending photo
  404s. The route takes a **UUID only** — the filename comes from the DB, never from the
  request, so there is no path component to traverse.
- No PII: EXIF (including phone GPS) is stripped by the Pillow re-encode, and the content is
  an advertisement we are actively trying to show the public.
- It is outside `/api`, so it never touches the key bucket or the rate limiter.

The Astro side still proxies, at `src/pages/used-doors/photo/[id].jpg.ts` — **note the
literal `.jpg` extension**, because Cloudflare does not cache extensionless dynamic paths
by default and origin `Cache-Control` alone will not make it. With the extension, CF's
static rules apply and `Cache-Control: public, max-age=86400, immutable` sticks. Proxying
keeps images same-origin for SEO and Core Web Vitals; the proxy needs no key, so a
missing/rotated key breaks the listings JSON only — never the photos.

All uploads — office, tech, and portal — go through **one** endpoint writing to
`UPLOAD_DIR/{tenant}/door_listings/{listing_id}/`, reusing the Pillow resize + MIME
allowlist + size cap from `routers/uploads.py`, plus a ~1200 px web derivative (the
existing 2048 px resize is sized for job evidence, not a marketing grid). Explicitly **not**
`MOBILE_UPLOAD_DIR` — see trap (b).

Customer- and tech-uploaded images are untrusted input reaching a public page, so the MIME
allowlist and the Pillow re-encode are load-bearing, not incidental: re-encoding strips
EXIF (including GPS from a phone photo taken at someone's house) and neutralizes
polyglot-file tricks. Cap photos per listing (~8).

---

## 7. Public API + website

**App, new scope `listings:read`** added to `VALID_SCOPES`:

| Route | Purpose |
| --- | --- |
| `GET /api/v1/listings` | published only, JSON |
| `GET /api/v1/listings/{id}/photos/{photo_id}` | image bytes, published only |

**Website** — `used-doors.astro` becomes SSR-dynamic: server-side fetch with a 5 s
`AbortController` timeout (the pattern already in `api/lead.ts`), rendering a card grid with
photo, title, size, condition, price or "Call for price", and a `Used` / `In stock` /
`Quick ship` badge.

### 7.1 Ordering — GDX stock first

Ordering is computed **server-side in `/api/v1/listings`**, not in the Astro template, so
the rule lives in one place and the website can't drift from it:

1. `owner_rank` — GDX-owned (`source` in `office`, `tech`) = 0; customer-owned
   (`source = customer`) = 1. **Our doors always sit above consignment.**
2. `featured` desc — office pin, floats a door to the top of its own group.
3. `published_at` desc — freshest first.
4. `id` — deterministic final tiebreak so pagination is stable.

Taking "the gdx stuff" as *GDX-owned vs. customer-owned* (the previous section's open
question), since that's the distinction that was on the table. If it meant *new/in-stock
before used*, that's a one-line change to step 1 — say so and it moves.

Ordering is applied **after** filtering, so a visitor who filters to "Used" still sees GDX
used doors above customer used doors. The rank is never a reason to hide anything.

### 7.2 Filter

Chips for **All / Used / In stock / Quick ship**, driven by a `?type=` query param and
**rendered server-side**, not client-only JS — so each filter view is crawlable,
shareable, and works without JavaScript. `rel="canonical"` on every filtered view points at
the bare `/used-doors` to avoid duplicate-content dilution.

The URL stays `/used-doors` — it's already indexed and already in the nav, so renaming
costs SEO for no gain. The *heading* broadens ("Doors for sale — used, in stock, and quick
ship") so new inventory isn't undersold by a URL we're keeping for other reasons.

**The existing static copy stays as the empty/fallback state.** If the app is down, times
out, or returns zero rows, the page renders exactly what it renders today. The marketing
site must never 500 or show a blank shelf because dispatch is restarting — hard
requirement, not a nicety.

Page `Cache-Control: public, s-maxage=120, stale-while-revalidate=600`, so Cloudflare serves
fast without pinning sold doors on the page for hours.

### 7.3 Env-var fragility

Per `website-leads-status`, a push to the site's `main` has previously regenerated the
preload shim and dropped the dotenv loader, silently 401ing the lead form. Phase 2 adds two
vars to that same mechanism, and its failure mode is invisible: the page renders the static
fallback and looks perfect while the lead form dies alongside it.

Mitigations: (a) the listings fetch logs a loud, distinct `[listings] config missing` on
absent env, mirroring `api/lead.ts`; (b) **post-deploy verification must exercise the lead
form too**, not just `/used-doors` — they share the loader, so the listing page is an early
warning for the form; (c) photos need no key (§6), so a key failure degrades to "no doors
listed", never "broken images".

New Hostinger env vars: `GDX_LISTINGS_URL` and a **separate** `GDX_LISTINGS_API_KEY` scoped
to `listings:read` only — not the existing lead-form key, since a read key and a write key
have no reason to share a blast radius. Never `PUBLIC_`-prefixed, for the reason the README
already spells out.

---

## 8. UI surfaces

- **Office** — `DoorListingsView.vue` at `/door-listings`, registered in
  `router/index.js` near `/inventory`. Grid with status chips, a **Pending review** filter
  defaulting to the top, inline approve/reject, create/edit drawer, drag-order photos, and
  a Publish toggle that shows exactly what the public sees. Sidebar badge with the pending
  count.
- **Tech (mobile)** — a "List this door" action in the mobile console: camera capture,
  size, condition, a note, submit. Deliberately minimal — a tech standing at a tear-out
  should be done in under a minute. They see their own submissions and any rejection
  reason. Pricing is *not* a tech field; the office sets price at approval.
- **Customer (portal)** — same minimal form behind the §5 gate, plus a list of their own
  submissions and statuses.

⚠️ **No hard-delete button behind `useDestructiveConfirm`** — that composable currently
auto-accepts without rendering (issue #215), so a misclick would delete silently. Archive
via `status`; keep the row.

---

## 9. Phasing

| Phase | Scope | Outcome |
| --- | --- | --- |
| **1** | Migration `040`, model, office CRUD + approve/reject, photo upload, `DoorListingsView.vue`, tests | Office can enter and manage doors. Nothing public yet. |
| **2** | `listings:read` scope, public read endpoints, Astro page + image proxy, new key + env vars | **Doors appear on garagedoorxperts.com.** |
| **3** | Tech mobile submission → pending queue | Field capture at the tear-out. |
| **4** | Company + per-customer toggles, portal submission form | Customer listings, off by default. |
| **5** | Per-door detail pages + `Product`/`Offer` JSON-LD, "I'm interested" → existing `/api/lead` → Leads inbox, auto-hide sold after N days | SEO + closes the loop. |

Phases 3 and 4 both depend on the approval queue from Phase 1, which is why the queue is
built first even though only the office uses it initially — it is the thing that makes
untrusted submission safe, and retrofitting it later would mean either a period where tech
submissions publish unreviewed, or throwing away the direct-publish UI.

Phase 5's "interested" button is the highest-value cheap win: it reuses the shipped lead
pipeline end to end, so a door on the site becomes a row in the app with no new plumbing.
The JSON-LD matters more than it sounds — "used garage doors [city]" is a search people
actually make, and today we rank with a page that lists nothing.

Deploy: app via `/GDXrelease` + `/gdxproductionupdate` (prod is v1.30.0); website by push to
`main`, then purge `/used-doors` in Cloudflare.

---

## 10. Decided / still open

**Decided — `/used-doors` stays, with a type filter and GDX-first ordering** (§7.1, §7.2).
One page, one URL, no redirect, no lost SEO. This also means the Phase 2 route name is
settled and nothing downstream is waiting on it.

**Still open — how customer-owned doors present publicly** (§5). They'll appear on the same
page, ranked below GDX stock, which is the ordering half of the answer. The remaining half
is whether a consignment door needs a visible "customer-owned" label, different contact
routing, or an explicit no-GDX-warranty note. Only affects card copy; doesn't block any
phase, and doesn't come due until Phase 4.

## 11. Risks

- **Permission snapshots** — trap (a). The single most likely way this ships broken.
- **Ephemeral mobile storage** — trap (b). Listing photos must use `UPLOAD_DIR`.
- **Cloudflare staleness** — has already bitten this site once, per the README.
- **App downtime becomes website degradation** — bounded by the §7 fallback, but real.
- **The approval queue rotting** — if approvals are slow, techs stop submitting and the
  feature dies quietly. The pending badge and inline approve are the mitigation.
- **Honesty of the shelf** — a sold door left published is worse than no page at all.
  `sold` has to be one click from the grid or it won't happen.
- **Untrusted images on a public page** — mitigated by the allowlist + Pillow re-encode
  (which also strips EXIF GPS), per-listing photo cap, and the fact that nothing reaches
  the public without office approval.
- **Listing photos are local disk** — they ride the `docker_gdx_uploads` volume; worth
  confirming that volume is inside the backup set before the site depends on it.

---

## 12. Adversarial audit — what changed

Audited before writing code. Five load-bearing claims were checked against the source; the
app-side ones held, **two of my own claims were wrong**, and two unexamined risks were real.

**Confirmed true**

- Permission snapshots honor the snapshot verbatim (`core/modules.py:495-499`), `technician`
  exists (`permissions.py:175`), the leads tool did exclude it. The *diagnosis* was right —
  the *prescription* (a new snapshot migration) was over-built. See §4.
- `MOBILE_UPLOAD_DIR` is ephemeral `/tmp` (`.env.template:241`), `/app/uploads` is the
  external volume `docker_gdx_uploads` (`docker-compose.yml:111,236-238`), and
  `mobile.py:3313`'s `photo_url` is served by nothing.
- No route collision: `api/public_router.py:131` is the sole owner of `/api/v1`.

**Wrong, and corrected**

- **`create_orm_tables()` runs BEFORE alembic** (`docker/entrypoint.sh:28-36`). New *tables*
  are created by `create_all` on boot; only *columns on existing tables* need migration 040.
  A plain `op.create_table("door_listings")` would hit "relation already exists" and, under
  `set -e`, **crash-loop the container**. Migration 040 therefore uses the house idiom: raw
  `CREATE TABLE IF NOT EXISTS` (per `030_customer_contacts.py:29-45`) and `DO $$ … ADD COLUMN
  IF NOT EXISTS` (per `039_vendor_statement_source.py`). This was the single most dangerous
  gap in the first draft.
- **There is no `/api` auth gate.** I cited one as precedent. The real cause of the
  `/api/proposals/{token}` 401 is **route shadowing** — an authenticated
  `/api/proposals/{proposal_id}` registers first and swallows the token route. Right lesson,
  wrong mechanism: what matters is registration order, and `public_v1_router` is included
  near-last (`app.py:1884`), so it loses every collision. Checked; ours is clear.

**Newly found risks**

- **60 req/min per API key** (`core/api_keys.py:376-387`), far tighter than the 600 in
  `rate_limiter.py`. Serving N thumbnails per page render through the keyed endpoint would
  trip it and return 429 JSON where an `<img>` expects bytes — and the §7 fallback only
  covers the JSON fetch, so the page would look fine with every image broken. **This killed
  the keyed-image design; see §6.**
- **`/app/uploads` is in `/backup full` but NOT in the pre-deploy snapshot** — `update.sh`
  is `pg_dump` only. A rollback restores listing *rows* without their *photos*.
- **Hostinger env-var fragility** — per `website-leads-status`, a push to the site's `main`
  has previously regenerated the preload shim and dropped the dotenv loader, silently
  401ing the lead form. Phase 2 adds two more vars to that same mechanism, and its failure
  is invisible (the page renders the static fallback and looks perfect while the lead form
  dies alongside it). Mitigation in §7.3.

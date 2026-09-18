# gdx-plugin-cellcomms — personal-cell texts & calls in GDX

**Status:** BUILT — PR #752 open (2026-09-18). The cell-comms nomad-gateway plan
(in the design records) holds the reasoning; update both status lines when
this merges/releases.

Texts and calls from the owner's personal Android cell, alongside the
Phone.com business line. Two feeds, one pair of tables
(`plug_cellcomms_messages`, `plug_cellcomms_calls`):

- **Live feed (incoming only):** [android-nomad-gateway]
  (https://github.com/we-digital/android-nomad-gateway) on the phone POSTs
  each incoming SMS / call to the core webhook
  `POST /api/cell-gateway/webhook`, which checks `X-GDX-Cell-Secret` against
  `CELL_GATEWAY_WEBHOOK_SECRET` (fail-closed) and relays to this plugin's
  `/ingest`.
- **Backfill (both directions + history):** upload an SMS Backup & Restore
  XML file (`sms-*.xml` / `calls-*.xml`) on the plugin's *Import phone
  backup* screen. Dedupe keys are message-intrinsic, so the two feeds never
  double-store an event; a backfilled call *enriches* its ring-time webhook
  row with real duration/outcome. `<mms>` elements are counted and skipped.

Numbers are matched to customers by the same normalize-E.164 → salted-hash →
`customers.phone_hash` rule core uses (`matching.py` documents the parity
contract and its test). A `customer.created` event re-links earlier unmatched
rows on the spot.

## Server setup

1. Install the plugin (owner → Plugins admin), consent to the `events`
   permission.
2. Set `CELL_GATEWAY_WEBHOOK_SECRET` in the stack's `.env` (any long random
   string) and recreate the `app` container. Blank secret = webhook off
   (403), by design.
3. Verify: `curl -X POST https://<domain>/api/cell-gateway/webhook -H
   "X-GDX-Cell-Secret: <secret>" -H "content-type: application/json" -d
   '{"kind":"sms","from":"+15551230000","text":"ping","sentStamp":"0"}'`
   → `{"status":"ok"...}`, and the row appears on the Texts screen.
4. Deploy check (load-bearing): `app` and `plugin-host` must resolve the same
   `SEARCH_HASH_SALT`, or customer matching silently misses — compare
   `docker exec <ctr> printenv SEARCH_HASH_SALT` on both.
5. Deploy check: the prod nginx vhost's `client_max_body_size` must admit the
   backup upload (code caps at 100 MB; the demo conf pins 50M and the prod
   vhost lives on the VPS, not in this repo). A too-small limit 413s at nginx
   before the app ever sees the file.

## Phone setup

The in-app **Help** screen carries the same steps, phrased for the operator.

1. Sideload android-nomad-gateway (APK from its GitHub releases — it is not
   on the Play Store; that is also what lets it hold SMS/call-log permissions).
2. Create two forwarding rules to `https://<domain>/api/cell-gateway/webhook`
   with header `X-GDX-Cell-Secret: <secret>`:
   - SMS: `{"kind":"sms","from":"%from%","text":"%text%","sentStamp":"%sentStamp%","receivedStamp":"%receivedStamp%","sim":"%sim%"}`
   - Call: `{"kind":"call","from":"%from%","contact":"%contact%","timestamp":"%timestamp%","duration":"%duration%"}`
3. Exempt the app from battery optimization / allow autostart, or Android
   kills the forwarder overnight.
4. For history + outgoing: install SMS Backup & Restore, run a backup of
   texts and calls, and upload both XML files on the Import screen. Repeat
   whenever fresh outgoing history is wanted — re-uploads are idempotent.

## Privacy scope (ruled by the owner, 2026-09-18)

Store-all: every incoming event is kept, matched or not, and surfaces only on
these screens. Message bodies and numbers never go to logs or audit details.

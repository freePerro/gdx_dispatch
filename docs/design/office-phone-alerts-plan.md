# Office Phone Alerts — bell notifications on the Android app, no Firebase

**Date:** 2026-10-04
**Status:** PARTIALLY BUILT (written 2026-10-04 on `feat/mobile-android-capacitor`;
committed for review 2026-10-14 on `feat/mobile-android-app`) — backend (migration 122 — written as 105, renumbered to 121 on 2026-10-13 and to 122 on 2026-10-14 when main took 121 — token/revoke/feed endpoints, inbound-text
bell, 1-minute Phone.com poll), native foreground service + plugin + boot
receiver, and the SPA toggle/bridge are written and walked once on a physical
Pixel against a throwaway container. Not built or not proven: a phone that is
NOT battery-exempt under Doze, behaviour over days (token re-mint, service
restarts after OEM kills), and anything on prod (not deployed). Supersedes the FCM half of Phase 4 in
`mobile-android-capacitor-plan.md` (that phase is marked built; the FCM path
it describes was never wired — see "What already exists").
**Owner Domain:** `mobile-tech` (native + SPA), `communications` (bell, Phone.com ingest)

## 1. The ask

Doug, 2026-10-04: "will text messages that get sent to phone.com pop up a
notification saying message received?" — then: "the office gets the
notifications", and "why can't our current notifications trigger something in
the apk that says hey this event happened?" Decision: a native foreground
service, not Firebase.

**Done means:** an office user who turned on phone alerts in the Android app
gets an Android system notification within seconds of a bell notification
being written (a new text: within about a minute of it reaching Phone.com) — including a new inbound customer text — with the app closed
and the phone locked. Tapping it opens the right screen.

## 2. What already exists (do not rebuild)

Verified against code 2026-10-04 on `feat/mobile-android-capacitor`:

| Piece | Where | State |
|---|---|---|
| Office bell rows | `core/office_notifications.py` `notify_office()` → `Notification` (`user_id NULL` = broadcast) | Live; used for payments, estimate decisions, leads, bounces, scheduled-SMS failures |
| Bell feed | `routers/notifications.py` `/api/notifications{,/count}` (query `user_id = me OR NULL`) | Live; SPA polls every 60s |
| Category → page | `frontend/src/components/NotificationsDrawer.vue` `_destinationFor()` | Live, mobile-aware |
| Inbound SMS ingest | `modules/phone_com/upserts.py` `upsert_message()`; callers: 10-min poll `sync.py:84`, webhook `webhook_router.py:58` | Live; **rings nothing** on a new inbound text |
| Native foreground service template | `android/.../location/GpsForegroundService.java` + `GpsForegroundPlugin.java`, registered in `MainActivity` | Live (GPS while clocked in) |
| Web Push | `core/push_subscriptions.py` (VAPID) | Live for browsers/PWA; never reaches the Capacitor WebView |
| FCM | `src/lib/nativePush.js` | **Dead**: no caller, no `google-services.json`, no server sender, token sent nowhere | <!-- link-ok: names a file that is absent by design -->

## 3. Design

### 3a. Inbound text → bell (backend)
`upsert_message()` records whether it **inserted** the row. After its commit,
an insert with `direction == "in"` whose `sent_at`/`received_at` is within the
last 30 minutes calls `notify_office(category="sms", title="New text",
message="<customer name or …last-4>: <first 140 chars>")`.

- Insert-only → the poll and the webhook can never both ring for one message
  (the loser of a race hits the UNIQUE index and rolls back, never inserting).
- Recency gate → the first poll after deploy (48h window) and the nightly
  resync do not ring for backlog.
- Best-effort, never raises, same contract as every other `notify_office` caller.

### 3b. Device alert token (backend)
The phone's service cannot reuse the app session: refresh tokens rotate with
replay detection, so a second consumer would log the app out. It gets its own
credential — an **opaque random token** (not a JWT), stored as its SHA-256 in a
new table `device_alert_registrations` (migration 122). Reasons (from /audit):
a JWT signed with the login key would pass `_resolve_request_user` on every
`require_permission` route; a key derived from `JWT_SECRET` is empty on RS256
installs; a Redis revoke list is lost to `allkeys-lru` eviction. A random
token that only the feed looks up has none of those holes, and the table lets
an admin see and revoke devices.

- `POST /api/notifications/device-alerts/token` — logged-in user with
  `nav.office` (owner/admin pass). Returns the token once; stores the hash.
  Audit: `device_alerts_enabled`.
- `POST /api/notifications/device-alerts/revoke` — authenticated by the device
  token itself (works after the session expired). Sets `revoked_at`.
  Audit: `device_alerts_disabled`.
- `GET /api/notifications/device-alerts/feed?after=<cursor>&wait=<s≤25>` —
  device token only. Gates on every request: token row exists, not revoked,
  younger than 90 days; user active; live `nav.office`. Returns unread,
  undeleted bell rows for that user (`user_id = me OR NULL`) with
  `created_at > cursor − 2 min` (max 20) and the next cursor; the phone
  dedupes by id, so a row committed late inside the overlap is not lost.
  A full response (20 rows) returns a keyset cursor `p:<created_at>|<id>`
  instead, so a burst bigger than one response (a missed-texts backlog)
  arrives over the next polls rather than losing its tail — found by the
  2026-10-04 /audit, which lost 5 of 25 with the plain time cursor.
  With no cursor it returns zero items and the current cursor, so enabling
  never floods. Long-poll: an async handler checks every 3s with a
  short-lived session (no DB connection or worker thread held across the
  wait). The path is excluded from `SlowEndpointMiddleware`. Updates
  `last_seen_at` at most once a minute.

### 3b-2. Faster text pickup
The Phone.com webhook never delivered (2026-07-23 note), so texts arrive on
the 10-minute poll. Doug chose a 1-minute poll: a new beat entry
`phone-com-messages-fast` runs `sync_recent_messages(window_hours=1)` every
minute; the 10-minute 48-hour entry stays as the backstop. A 48-hour window
every minute would multiply Phone.com API load by ten.

### 3c. Native service (Android, Java — matches the GPS service)
`alerts/OfficeAlertsService` (foreground, type `specialUse` with a `PROPERTY_SPECIAL_USE_FGS_SUBTYPE`; `dataSync` is capped at 6h/day on Android 15, `remoteMessaging` is a poor fit) +
`alerts/OfficeAlertsPlugin` (`start`, `stop`, `status`, `openBatterySettings`,
pending-tap route) + `alerts/BootReceiver`.

- Loop: long-poll the feed → post one notification per item on a
  high-importance "Office alerts" channel (lock screen: `VISIBILITY_PRIVATE`
  with a public version "New GDX notification") → persist cursor.
- Ongoing notification on a separate low-importance channel ("Phone alerts
  are on").
- 401 → stop, clear credentials, post one "Phone alerts turned off — open
  GDX to turn them back on". Network errors → backoff 5s → 60s. A 403
  (module off, not office) backs off the same way and turns off only once
  403s span an hour with no other HTTP answer between them (network errors do
  not reset that clock): the server's module and permission checks answer a
  database error as "off", and the first build turned every polling phone off
  on one blip (/audit, 2026-10-14).
- Tap → opens `MainActivity` with the item's category; the plugin hands it to
  JS, which routes through the same `_destinationFor()` the bell uses.
- Server URL, token, cursor in app-private SharedPreferences.
- Restart on boot and on app update when enabled (`BOOT_COMPLETED`,
  `MY_PACKAGE_REPLACED`); `START_STICKY`.
- Doze: a foreground service keeps network in Doze, but timers defer. No
  permanent wake lock — a 10 s partial wake lock only while handling a
  response; a `NetworkCallback.onAvailable` retries at once after a drop.
  The toggle asks for unrestricted battery use.
- HTTP read timeout 45 s against the 25 s server hold.

### 3d. SPA
- `src/lib/officeAlerts.js` wrapper (no-op off native).
- Toggle "Alerts on this phone" — office users only (`nav.office`), shown on
  the mobile SMS page header and in the mobile menu. Turning on: notification
  permission → mint token → start service → prompt to allow unrestricted
  battery use (explains why).
- App start (native, signed in, enabled): make sure the service runs;
  re-mint the token when older than 7 days and revoke the old one. This and
  the owner check below run from `useOfficeAlertsKeeper`, called by
  `AppLayout` so it runs at every screen width, one call at a time. The user
  id comes from the access token: a cold-start session restore sets the token
  but not `auth.user` (/audit 2026-10-14). With no user id it neither resumes
  nor mints. The restore runs once per launch as one shared promise that every
  navigation awaits — a cold start from a notification tap navigates twice,
  and with a plain "tried" flag the second landed a signed-in user on the
  login page — and it re-reads `/api/users/me`, since the stored
  password-change hold went with sessionStorage (/audit 2026-10-14). Every change (toggle, logout, owner check, re-mint) runs through
  one queue; a mint gives up after 20 s so it cannot hold a logout behind it,
  and a mint that answers after that is revoked (native `revokeToken`, since
  the device-token header would need a CORS preflight from the WebView).
  No re-mint while notifications are blocked, and a start refused at the
  notification prompt revokes the token it was handed — either way a token
  would otherwise sit live and unused for 90 days. Below Android 13 there is
  no prompt, so blocked notifications there mean the old token runs to its
  90-day end with no re-mint; the service keeps polling but nothing shows.
- Logout chosen from the menu: stop the service and revoke the token. The
  revoke runs after the app session is cleared — the device token
  authorizes its own revoke, so it needs no session; offline, it is best
  effort. An automatic logout (expired session, any 401, the idle
  timer) leaves alerts on — the device token outlives the session by design,
  and prod logged 4,185 anonymous logouts in 30 days (/audit 2026-10-14).
- Whose alerts: the mint response carries `user_id`, kept on the phone. When
  a different user signs in, the app stops the service and revokes with
  reason `user_changed`, so the next person never inherits the last one's
  alerts after an automatic logout (/audit 2026-10-14). A phone enabled by
  an earlier build has no recorded owner and turns alerts off once. The
  native revoke builds its JSON by hand and keeps only `a-z` and `_` in the
  reason; the first version also dropped `_`, so `user_changed` was refused.
- Server-side revoke (`core/auth_revoke.revoke_device_alerts`, added after
  the 2026-10-04 /audit): a password change, either password reset, an admin
  "revoke all sessions", and an admin-revoked refresh token (`/auth/refresh`
  denylist hit) revoke every phone of that user. Replay detection does not:
  on prod it fired 9 times in 90 days on benign races, 7 of them the owner's
  (/audit 2026-10-14). Lock-out, delete and losing
  `nav.office` need no revoke — the feed refuses them per request. A logout
  on a *different* device does not touch the phone.

## 4. Rejected alternatives
- **FCM.** The Firebase project is baked into the APK; other self-hosted
  installs would need Doug's key, a Doug-run relay, or their own APK build.
  Can be layered on later as an optional channel.
- **UnifiedPush.** Server side is free (it speaks the Web Push we already
  send), but there is no maintained Capacitor plugin and users must install a
  distributor app.
- **WorkManager polling.** 15-minute floor, inexact under Doze.
- **Reusing the refresh cookie natively.** Rotation + replay detection.
- **A stateless JWT + Redis revoke list.** Rejected by /audit: login-key
  reuse, empty secret on RS256 installs, LRU eviction resurrects revoked
  tokens. Replaced by the table above.

## 5. Not in scope
- Fixing the Phone.com webhook (separate session; lead: the callback must
  answer `{"test":1}` with 200/201, and subscription tags are ANDed).
- Techs. The feed refuses non-office users.
- Per-category mute.

## 6. Verification
- Backend: pytest for 3a (insert vs update, in vs out, stale vs recent, never
  raises) and 3b (mint gate, device token refused on normal routes, revoke,
  deactivated user, tech refused, cursor overlap, empty first call, SQLite
  naive datetimes).
- SPA: vitest for the wrapper and toggle.
- Device: debug APK on the Pixel against a throwaway container — enable, lock
  the phone, `adb shell dumpsys deviceidle force-idle`, write an inbound text through `upsert_message`, see the
  notification, tap it, land on `/mobile/sms`; logout stops the service and the
  old token gets 401.

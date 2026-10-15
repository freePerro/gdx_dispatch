# GDX Planner — a second Android app for the planner, with reminder alerts

**Date:** 2026-10-14
**Status:** PARTIALLY BUILT — PR A (backend) is built on `feat/planner-reminders`; PR B (SPA) and PR C (Android) are not built. Three stacked PRs are described below: A backend, B SPA, C Android. Revised on 2026-10-14 after `/audit` (critique_e3b0c44298fc). The revision covers the keeper mount, the reminder recipient, the status set, the `created_at` format, tap routing, and dropping the manifest split. PR A's own `/audit` (critique_07d5a8e06230) added two rules, both in §3 PR A item 2: re-sending the same instant changes nothing, and a reminder must carry a UTC offset.
**Owner Domain:** `jobs-dispatch` (planner, reminders), `mobile-tech` (Android flavor, alerts service), `frontend-shell` (boot, router)

## 1. The ask

Doug, 2026-10-14: "I would like a second android app that is just linked to
the planner something clean and quick to make notes and see what needs to be
done without the distraction of the main app. we could call it gdx planner."
He then chose "b", a real second Capacitor app rather than an installable web
app. "It needs to be able to push alerts to the phone so we can see
reminders. I would like the whole planner deal with plans and threads in it."

**Done means:**

- An office user installs **GDX Planner** (`com.gdx.planner`) beside GDX
  Dispatch, points it at the server and signs in.
- It opens straight to the planner: Today plus its note, My Tasks, Plans and
  Messages, with a quick note always one tap away. There is no main-app
  navigation.
- A task can carry a **reminder time**, set from GDX Planner or from the
  office desktop planner.
- When that time arrives, the phone of **the person who set the reminder**
  shows an Android notification, even with the app closed and the screen
  locked.
- Tapping the notification opens GDX Planner on the Tasks tab.

**Who it is for:** office and admin accounts, the people who can reach the
planner today. Technicians are redirected away from the planner by the
`isTech` block in `router/index.js`, and GDX Planner does not change that. A
tech who signs in sees a plain "GDX Planner is for office accounts" screen
with a Sign out button. Opening the planner to techs is a separate decision
for Doug.

## 2. What already exists (do not rebuild)

Verified against code on 2026-10-14 (`origin/main` 992f73f3):

- **The whole planner.** `routers/planner.py` covers tasks, Today and the day
  note, plans with steps, and threads with messages. `MobilePlannerView.vue`
  already renders all four tabs at phone size. `QuickCaptureSheet.vue` is the
  quick note; today it opens from `AppBottomNav`'s FAB.
- **Phone alerts without Firebase.**
  - Backend: `routers/device_alerts.py`, which handles token mint and revoke
    plus a 25-second long-poll feed over `notifications`.
  - Android: `OfficeAlertsService.java`, a foreground service that turns each
    new bell row into an Android notification, with `OfficeAlertsPlugin` and
    `BootReceiver`.
  - SPA: `lib/officeAlerts.js`, `useOfficeAlertsKeeper` and
    `OfficeAlertsToggle.vue`.
  - `office-phone-alerts-plan.md` records the decision: a native foreground
    service, not Firebase. **GDX Planner reuses all of this and adds no FCM.**
    The unwired `nativePush.js` FCM helper stays unwired.
- **A once-a-minute beat cadence.** `core/scheduler.py` already runs
  per-minute `crontab` entries, for example the scheduled-SMS drain on
  `priority:high`. Prod runs `gdx-celery-beat-1`, checked 2026-10-14.
- **A server URL set at runtime.** `serverConfig.js` and
  `MobileServerConfigDialog` handle it. Capacitor Preferences are per app, so
  GDX Planner asks for its own server URL and no URL is baked in at build
  time.
- **Product flavors** are the documented Capacitor way to ship two apps from
  one Android project
  (<https://capacitorjs.com/docs/guides/environment-specific-configurations>,
  read 2026-10-14). `capacitor.config.json` keeps one `appId`, and each
  flavor's Gradle `applicationId` overrides it for that app.

**Not present:** any reminder time on a task. `PlannerTask.due_date` is
date-only in both UIs. The nearest thing is the daily email digest
(`tasks/planner_digest.py`), which goes to a single configured address.

## 3. Design

### PR A — backend: reminders and a planner feed scope

1. **Migration 125** adds three nullable columns to `planner_tasks`:
   `remind_at` (DateTime tz), `remind_user_id` (String(36)) and
   `reminder_sent_at` (DateTime tz). It is additive only, and existing rows
   get NULL, which means no reminder. The downgrade drops all three columns.
   It must run on SQLite and Postgres.
2. **API.**
   - Task create and PATCH accept `remind_at`, it can be cleared, and every
     task payload returns it.
   - Setting `remind_at` stamps `remind_user_id` with **the user who set
     it**. "Remind me" means me: a reminder on a task assigned to someone
     else still buzzes the person who asked for it (audit finding 3).
   - Setting or changing `remind_at` clears `reminder_sent_at`, so moving a
     reminder later makes it fire again. Clearing `remind_at` clears all
     three fields. Re-sending the **same** instant changes nothing — it
     neither takes the reminder over nor fires a sent one again — because an
     edit dialog saves the whole form (PR A audit, critique_07d5a8e06230).
   - `remind_at` must be an ISO date-time **with an offset**. A bare date or a
     naive time is a 422: a phone's local "09:00" read as UTC would buzz five
     hours early. PR B sends `Date.toISOString()`.
   - All of this goes through the existing audit calls
     (`planner_task_created` and `planner_task_updated`).
3. **Beat task `planner_reminders`**, every minute on `priority:high`.
   - It selects tasks where `remind_at <= now`, `reminder_sent_at IS NULL`
     and `status != 'done'`. The API's only statuses are `todo`,
     `in-progress` and `done`; there is no `cancelled`.
   - For each task it writes a `Notification` with:
     - `tenant_id = task.company_id`
     - `user_id = remind_user_id or created_by`
     - `category = "planner_reminder"`
     - title "Reminder: <task title>", and the due date in the message when
       there is one
     - `created_at = datetime.now(timezone.utc).isoformat()`, the same format
       `notify_office` writes
   - The timestamp format matters. The feed compares `created_at` as
     **text**, so a database-side `now()` stamp (`2026-10-15 04:25:55+00`)
     would sort below the `T`-format cursor and never reach a phone (audit
     finding 4).
   - It stamps `reminder_sent_at` in the same transaction. A guarded UPDATE
     (`... WHERE reminder_sent_at IS NULL`) makes two overlapping beats send
     only once.
   - It audits `planner_reminder_sent` with a system actor. This is not a
     money action.
4. **Feed scope.**
   - `GET /api/notifications/device-alerts/feed` takes `scope=planner`. That
     scope returns only rows addressed to the token's user with a `planner_`
     category, and excludes broadcast office rows.
   - The default (office) scope now **excludes** `planner_` rows. A reminder
     therefore buzzes the phone once, from GDX Planner, and not again from
     GDX Dispatch's office alerts.
   - The reminder still shows in the web bell drawer, which is a separate
     endpoint.
   - Prod has zero `planner_` rows today, so the office feed loses nothing
     that exists.
5. **`notificationDestinations.js`:** `planner_reminder` goes to `/planner` on
   desktop and `/mobile/planner?tab=tasks` on mobile.

Demo has no Celery beat container (checked 2026-10-14), so reminders never
fire there, and a demo walk proves the UI only. Firing is verified in a
throwaway container by calling the task directly, and on prod.

### PR B — SPA: reminder field and planner app mode

1. **Reminder field.** The task create and edit forms get a "Remind me" date
   and time picker, in `MobilePlannerView.vue` and in the desktop planner's
   task dialog. The value is saved as an ISO instant. A task card shows a
   bell plus the time when a reminder is set.
2. **App mode detection.** On native, `main.js` awaits `App.getInfo()` from
   `@capacitor/app`, already a dependency, before it installs the router.
   - `id === "com.gdx.planner"` sets `appMode = 'planner'`.
   - Web and GDX Dispatch keep `appMode = 'dispatch'`.
   - One web bundle ships in both APKs, because `cap sync` copies `dist` into
     `src/main/assets`, which both flavors share.
3. **Planner shell.** A new route `/planner-app` (`meta.noShell`) renders a
   new `PlannerAppView` page:
   - a slim header with "GDX Planner", an alerts toggle, and a menu with
     Server and Sign out
   - the existing `MobilePlannerView` body underneath
   - a floating "+ Note" button that opens `QuickCaptureSheet`
   - nothing that links to the rest of the app
4. **Router in planner mode.**
   - `/` and every route outside an allowlist (`/planner-app`, login,
     password change) redirect to `/planner-app`, keeping the query.
   - Login lands on `/planner-app`.
   - A tech gets the office-accounts screen.
   - Links from a task to a customer or job are shown as plain text, so they
     cannot lead into the main app with no shell around it.
5. **Alerts in planner mode.**
   - The problem: `useOfficeAlertsKeeper()` is mounted only in
     `AppLayout.vue`, and `noShell` routes skip `AppLayout`. Without the
     keeper, GDX Planner would lose three things (audit finding 1):
     - the weekly token re-mint, so after 90 days the server returns 401 and
       alerts stop for good
     - the restart after the phone maker's battery manager kills the service
     - the stop when a different user signs in
   - `PlannerAppView` therefore mounts the keeper as well.
   - `lib/officeAlerts.js` derives `scope` from `appMode` itself on every
     `start()` call, both the first start and each re-mint. No caller passes
     it, so no path can start the planner app on the office feed.
6. **Tap routing.** `MobilePlannerView` reads `?tab=` (`today`, `tasks`,
   `plans`, `messages`) on mount and on route change. In planner mode, the
   redirect to `/planner-app` keeps the query, so a tap on a
   `planner_reminder` lands on Tasks.

### PR C — Android: the `planner` flavor

1. **Flavors.** `android/app/build.gradle` gets `flavorDimensions "app"` with
   two flavors: `dispatch` (`applicationId com.gdx.dispatch`, unchanged) and
   `planner` (`applicationId com.gdx.planner`). `namespace` stays
   `com.gdx.dispatch`, so `R`, `MainActivity` and the alerts classes compile
   once for both.
2. **Flavor resources.** The planner flavor's `res/` sets `app_name` and
   `title_activity_main` to "GDX Planner", sets its own URL scheme, and has
   its own launcher icon: a planner glyph on the GDX navy, so the two apps
   can be told apart on a home screen.
3. **No manifest split in v1.** Both flavors share the main manifest.
   - The Capacitor plugin libraries (geolocation, camera, push) merge their
     own permissions into both APKs anyway, so a split would not give the
     planner a clean manifest.
   - It would add the risk of changing GDX Dispatch's merged manifest, for
     nothing a user would see (audit finding 2).
   - Android asks for a runtime permission only when code requests it, and
     GDX Planner never calls the GPS, camera or barcode paths.
   - A trimmed planner manifest matters only for a Play listing, which is out
     of scope (§5).
4. **`OfficeAlertsService` changes.** It reads `scope` from the plugin's
   `start()`, stores it in prefs and appends `&scope=` to the feed URL. It
   uses `R.string.app_name` in place of the hard-coded "GDX Dispatch".
   `MainActivity` stays shared: the GPS plugin class compiles into the
   planner build, but nothing there calls it.
5. **README.** Build and install commands for both flavors:
   `assembleDispatchDebug`, `assemblePlannerDebug`, and
   `npx cap run android --flavor planner`.

## 4. Risks and how each is checked

- **Renaming the build variant breaks the existing release recipe.**
  `assembleRelease` becomes `assembleDispatchRelease`. PR C updates the
  README and `scripts/`. Check: grep for `assembleRelease` and
  `bundleRelease`.
- **Doze delays the long-poll.** This is the known open item in
  `office-phone-alerts-plan.md`, and GDX Planner inherits it. On a phone that
  is not exempt from battery optimisation, a reminder can arrive late. The
  existing toggle's battery-exemption prompt covers this.
- **A reminder fires twice.** One cause is two beats overlapping; the guarded
  UPDATE handles it. The other is both apps polling; the feed scope handles
  it. Both get tests.
- **A tech's token on the planner scope.** The feed's `_gate` already refuses
  non-office users with a 403, and that does not change.

## 5. Out of scope (for Doug to rule on later)

- Push alerts for new thread messages and task assignments. Once reminders
  are proven, each is easy to add as another `planner_` category.
- Opening the planner, and GDX Planner, to technicians.
- A Play Store listing and signing for `com.gdx.planner`.

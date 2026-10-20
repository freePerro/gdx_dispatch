# Mobile Android Native App (Capacitor Shell & Google Play Distribution)

**Date:** 2026-10-04  
**Owner Domain:** `mobile-tech` (see `.claude/agents/mobile-tech.md`)  
**Status:** PHASES 1–5 BUILT & VERIFIED — Capacitor initialized in `gdx_dispatch/frontend/`, Android native project configured in `gdx_dispatch/frontend/android/`, Server URL storage + options gear dialog built, live connectivity verified on Pixel 8 emulator. Hardware capabilities (Camera, Persistent Foreground GPS, Push Notifications — **FCM half NOT built, see Phase 4**, Google Play Location Disclosure, Barcode Scanning, Network Awareness, and Quick PIN Unlock) built, verified, and compiling into both debug APK and production App Bundle (`app-release.aab`).

---

## 1. Context & Motivation

GDX Dispatch is a single-tenant, self-hosted field-service management platform. Technicians use mobile phones in vans, garages, and customer driveways.

The web app already has 13+ mobile-tailored touch views (`MobileTodayView.vue`, `MobileJobDetailView.vue`, `MobileTimeclockView.vue`, etc.), an offline IndexedDB store (`offlineDb.js`), and Web Push support (`sw.js`).

Packaging the frontend with **Capacitor** creates an installable Android APK / Google Play App Bundle (`.aab`) while preserving 100% of the existing Vue 3 codebase.

### Why Capacitor (and not TWA or Pure Native)?
1. **Self-Hosted Flexibility:** A Trusted Web Activity (TWA) mandates static domain `assetlinks.json` verification. In a self-hosted single-tenant product where contractors host on custom domains (`dispatch.client.com`), a TWA cannot dynamically switch hosts without breaking Android sandbox verification. Capacitor packages web assets directly inside the APK, allowing dynamic server configuration. <!-- link-ok: names a file that is absent by design -->
2. **Local assets:** Loading web assets from local app storage means the app shell launches in metal pole barns and dead zones. As built (2026-10-14), a *cold* start there does not reach `offlineDb.js`: the session is restored from the refresh cookie once per launch, which needs the server, so an offline cold start lands on the login page.
3. **Hardware Access:** Enables native background location services (tracking while clocked in), direct camera capture for job closeouts, barcode/QR scanning for parts, and push notifications via FCM.
4. **Zero Licensing Cost:** Capacitor is 100% free and open-source under the MIT license.

---

## 2. Directory Structure & Tracking

- `gdx_dispatch/frontend/capacitor.config.json` — Core configuration (appId: `com.gdx.dispatch`, appName: `GDX Dispatch`, webDir: `dist`, CapacitorHttp enabled).
- `gdx_dispatch/frontend/android/` — The native Android Studio / Gradle project.
- `gdx_dispatch/frontend/android/README.md` — Explains build steps and invariants to other contributors/agents.
- `gdx_dispatch/frontend/android/scripts/generate-keystore.sh` — Keystore generation tool for release signing.
- `gdx_dispatch/frontend/src/lib/serverConfig.js` — Server URL management, persistent storage, and ambient fetch interceptor.
- `gdx_dispatch/frontend/src/lib/nativeBridge.js` — Android hardware back button and lifecycle bridge.
- `gdx_dispatch/frontend/src/lib/nativeCamera.js` — Native camera and gallery picker with seamless web `<input type="file">` fallback.
- `gdx_dispatch/frontend/src/lib/nativeGps.js` — Android Foreground Service bridge, disclosure state, and high-accuracy geolocation helper.
- `gdx_dispatch/frontend/src/lib/nativePush.js` — Native push notification channels, permissions, and FCM token capture.
- `gdx_dispatch/frontend/src/lib/nativeBarcode.js` — Google ML Kit barcode and QR code scanner.
- `gdx_dispatch/frontend/src/lib/nativeAuth.js` — Salted SHA-256 4-digit quick PIN authentication.
- `gdx_dispatch/frontend/android/app/src/main/java/com/gdx/dispatch/location/GpsForegroundService.java` — Native Android Foreground Service with ongoing notification and wake lock.
- `gdx_dispatch/frontend/android/app/src/main/java/com/gdx/dispatch/location/GpsForegroundPlugin.java` — Capacitor plugin exposing foreground service start/stop/isRunning to JS.
- `gdx_dispatch/frontend/src/components/MobileServerConfigDialog.vue` — Server Settings dialog with presets and `/health` test button.
- `gdx_dispatch/frontend/src/components/MobileLocationDisclosureDialog.vue` — Google Play compliant prominent location disclosure dialog.
- `gdx_dispatch/frontend/src/components/MobilePinUnlockDialog.vue` — 4-digit quick PIN touch keypad unlock modal.

---

## 3. Developer Commands

```bash
# In gdx_dispatch/frontend:
npm run cap:build   # Runs npm run build and copies assets into the Android native project
npm run cap:sync    # Syncs native plugins and web assets
npm run cap:open    # Opens the native project in Android Studio

# Compiling Android debug APK (from android directory):
JAVA_HOME=/home/doug/android-studio/jbr ANDROID_HOME=/home/doug/Android/Sdk ./gradlew assembleDebug

# Compiling Android production App Bundle (.aab) for Google Play:
JAVA_HOME=/home/doug/android-studio/jbr ANDROID_HOME=/home/doug/Android/Sdk ./gradlew bundleRelease
```

---

## 4. Implementation Roadmap

- [x] **Phase 1: Capacitor Core & Android Project Baseline**
  - Install `@capacitor/core`, `@capacitor/cli`, `@capacitor/android`.
  - Initialize configuration with dark background matching GDX theme.
  - Generate `android/` native container.
  - Compile and verify `app-debug.apk` using local JDK 21 and Android SDK Build-Tools 35.

- [x] **Phase 2: Mobile Bridge Adapters (Server URL & Hardware Back Button)**
  - Configurable server URL store (`serverConfig.js`) persisted via `@capacitor/preferences` + `localStorage`.
  - Ambient `window.fetch` interceptor prefixing relative `/api/...`, `/auth/...`, `/portal/...` endpoints to configured server URL while preserving local assets on `https://localhost/`.
  - Options gear button in `LoginView.vue` and "More Modules" drawer in `AppBottomNav.vue`.
  - Modal dialog (`MobileServerConfigDialog.vue`) with input, presets ("Public Demo", "Local Dev"), live `/health` connection test, and save action.
  - Build-time default server (GDXA-1993, 2026-10-19): set `VITE_DEFAULT_SERVER_URL` when running `vite build` for the APK (e.g. `VITE_DEFAULT_SERVER_URL=https://dispatch.yourshop.example npm run build`, then `npx cap sync android`). It goes first in the presets as "This shop", and the native app uses it when no URL is saved in Preferences/localStorage. A URL the tech saved still wins, and clearing it falls back to the default. Leave it unset and nothing changes. The web build ignores it: the web app always talks to the server that served it.
  - Android hardware back-button listener (`nativeBridge.js`) hooked into Vue Router with modal dismissal.
  - Verified on running Pixel 8 emulator with live connection test to `http://10.0.2.2:8001/health` and verified persistence across app restarts.

- [x] **Phase 3: Native Hardware Capabilities (Camera & Persistent GPS)**
  - Installed `@capacitor/camera` and `@capacitor/geolocation`.
  - Built `nativeCamera.js` bridging camera/gallery to standard `File`/`Blob` pipelines (IndexedDB, Dexie, FormData) with web fallback.
  - Wired native camera into `MobileReceiptCapture.vue` and `MobileJobDetailView.vue`.
  - Implemented Android Foreground Service (`GpsForegroundService.java`) with ongoing notification (`FOREGROUND_SERVICE_TYPE_LOCATION`) and partial wake lock.
  - Created native plugin (`GpsForegroundPlugin.java`) registered in `MainActivity.java`.
  - Updated `AndroidManifest.xml` with permissions (`CAMERA`, `READ_MEDIA_IMAGES`, `ACCESS_FINE_LOCATION`, `FOREGROUND_SERVICE_LOCATION`, `POST_NOTIFICATIONS`, `WAKE_LOCK`).
  - Integrated with `useGpsBreadcrumb.js` so starting GPS automatically brings up the foreground service, and clocking out or 403 response cleanly tears it down.
  - Unit tests in `nativeCamera.spec.js`, `nativeGps.spec.js`, `MobileReceiptCapture.spec.js`, and `useGpsBreadcrumb.spec.js`.

- [x] **Phase 4: Google Play Release Packaging & FCM Push Notifications** — *partly: release packaging is built; FCM push is NOT (correction 2026-10-04)*
  - Installed `@capacitor/push-notifications` with Android notification channel setup (`dispatch_alerts`) and deep-link routing. **Correction 2026-10-04:** `src/lib/nativePush.js` has no caller, there is no `google-services.json`, and no server sender exists — FCM never worked. Superseded by `office-phone-alerts-plan.md` (native foreground service, no Firebase). <!-- link-ok: names a file that is absent by design -->
  - Created `google-services.json.example` template and conditional Gradle compilation.
  - Implemented Google Play Prominent Location Disclosure & Consent dialog (`MobileLocationDisclosureDialog.vue`) and gated `useGpsBreadcrumb.js` tracking on affirmative consent.
  - Configured environment-driven signing in `android/app/build.gradle` and interactive keystore generator (`scripts/generate-keystore.sh`).
  - Compiled and verified production App Bundle (`app-release.aab`, 8.4 MB).

- [x] **Phase 5: Barcode Scanner, Network State & Quick PIN Authentication**
  - Installed `@capacitor-mlkit/barcode-scanning` with `nativeBarcode.js` and integrated barcode scan button in `MobileInventoryView.vue` for parts/SKU lookup.
  - Installed `@capacitor/network` and integrated Android `ConnectivityManager` status into `useOnlineState.js`.
  - Built 4-digit quick PIN authentication (`nativeAuth.js`) and touch keypad modal (`MobilePinUnlockDialog.vue`) for rapid truck/garage unlock.
  - Comprehensive unit test suites (46 suites, 444 tests passing).

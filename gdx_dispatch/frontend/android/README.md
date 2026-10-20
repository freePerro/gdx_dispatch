# GDX Dispatch — Native Android Shell (Capacitor)

> **Owner Domain:** `mobile-tech` (see `.claude/agents/mobile-tech.md`)  
> **Status:** Active (Production / Google Play integration)

---

## 1. Purpose & Architecture

This directory contains the native **Android Studio project** that wraps the GDX Dispatch Vue 3 SPA frontend (`dist/`) into an installable native Android application distributed via the Google Play Store and private APKs.

### Why Capacitor?
1. **Self-Hosted Field Service:** Unlike multi-tenant SaaS, GDX Dispatch is a single-tenant, self-hosted platform. Capacitor packages the web application assets locally inside the app bundle, allowing technicians to configure their company's server endpoint (or connect to default deployments) without requiring static domain Digital Asset Links.
2. **Local assets:** Technicians frequently operate in rural dead zones, basements, and metal pole barns. The HTML/JS ships inside the APK, so the app shell opens without a network. A signed-in session survives being backgrounded, and Dexie IndexedDB (`gdx_dispatch/frontend/src/lib/offlineDb.js`) and the offline mutation outbox (`useOfflineSync.js`) keep working there. A **cold start** is different (checked 2026-10-14): the app loses sessionStorage on every close and restores the session from the refresh cookie once per launch. With no signal that restore fails, and the tech lands on the login page until they sign in online.
3. **Hardware Access:** Grants access to Android hardware features:
   - Direct camera photo capture for work order closeout receipts and garage door condition documentation.
   - Foreground service background GPS tracking while clocked in (for dispatch board accuracy).
   - Google Play compliant prominent location disclosure dialog.
   - Android hardware back button and lifecycle integration.
   - Office phone alerts as Android notifications, from a native foreground service (`OfficeAlertsService`); no Firebase.

---

## 2. Directory Structure

- `app/` — Main Android application module. It builds two apps from one web
  bundle, as product flavors in `app/build.gradle`: `dispatch` (GDX Dispatch,
  `com.gdx.dispatch`) and `planner` (GDX Planner, `com.gdx.planner`, an
  office-only planner with task-reminder alerts; `src/lib/appMode.js` picks the
  mode from the application id at startup).
  - `src/planner/res/` — GDX Planner's name, icon and alert-channel text,
    layered over `src/main/res/`.
  - `src/main/AndroidManifest.xml` — Android permissions, application metadata, and main activity declaration.
  - `src/main/java/com/gdx/dispatch/MainActivity.java` — Native activity entrypoint.
  - `src/main/java/com/gdx/dispatch/location/` — Foreground GPS service and plugin.
  - `src/main/res/` — Native app icons, splash screens, and Android theme XMLs.
- `scripts/` — Build and signing helper scripts:
  - `scripts/generate-keystore.sh` — Interactive keystore generation tool.
- `build.gradle` & `app/build.gradle` — Gradle build configuration targeting Android API 34/35.
- `gradlew` — Gradle wrapper script.

---

## 3. Developer Workflow

### Updating Web Code in the Android Project
Whenever changes are made to `gdx_dispatch/frontend/`
```bash
cd gdx_dispatch/frontend
npm run build
npx cap sync android
```
*(Or run the combined script: `npm run cap:build`)*

### Compiling Debug APKs
Using the environment's JDK 21 and Android SDK:
```bash
cd gdx_dispatch/frontend/android
JAVA_HOME=/home/doug/android-studio/jbr ANDROID_HOME=/home/doug/Android/Sdk ./gradlew assembleDispatchDebug
JAVA_HOME=/home/doug/android-studio/jbr ANDROID_HOME=/home/doug/Android/Sdk ./gradlew assemblePlannerDebug
```
`./gradlew assembleDebug` builds both. Output APKs:
- `app/build/outputs/apk/dispatch/debug/app-dispatch-debug.apk`
- `app/build/outputs/apk/planner/debug/app-planner-debug.apk`

The two install side by side; each has its own sign-in and its own alerts.

### Launching on Emulator
To run on the local `Pixel_8` AVD:
```bash
/home/doug/Android/Sdk/emulator/emulator -avd Pixel_8 &
adb wait-for-device
adb install -r app/build/outputs/apk/dispatch/debug/app-dispatch-debug.apk
adb shell am start -n com.gdx.dispatch/com.gdx.dispatch.MainActivity
# GDX Planner (the activity class keeps the shared com.gdx.dispatch namespace):
adb install -r app/build/outputs/apk/planner/debug/app-planner-debug.apk
adb shell am start -n com.gdx.planner/com.gdx.dispatch.MainActivity
```

---

## 4. Production Release & Google Play Signing

### 1. Generating Release Keystore
Run the helper script:
```bash
cd gdx_dispatch/frontend/android
./scripts/generate-keystore.sh release.jks gdx-dispatch
```
Keep `release.jks` in a secure location (never commit keystores to git).

### 2. Building Signed Production App Bundle (.aab)
Set the environment variables before building:
```bash
export GDX_KEYSTORE_PATH=/path/to/release.jks
export GDX_KEYSTORE_PASSWORD=your_keystore_password
export GDX_KEY_ALIAS=gdx-dispatch
export GDX_KEY_PASSWORD=your_key_password

JAVA_HOME=/home/doug/android-studio/jbr ANDROID_HOME=/home/doug/Android/Sdk ./gradlew bundleRelease
```
`bundleRelease` builds both apps (`bundleDispatchRelease` or
`bundlePlannerRelease` builds one). The Google Play App Bundles are output at:
- `gdx_dispatch/frontend/android/app/build/outputs/bundle/dispatchRelease/app-dispatch-release.aab`
- `gdx_dispatch/frontend/android/app/build/outputs/bundle/plannerRelease/app-planner-release.aab`

GDX Planner is a separate Play Console app (`com.gdx.planner`).

---

## 5. Invariants for Agents & Contributors

- **DO NOT delete this directory:** This project is intentionally tracked to provide reproducible Android builds for field technicians and Google Play releases.
- **Do not commit `.apk`, `.aab`, `.jks`, `.keystore`, `.gradle/`, or `build/`:** These are ignored by `android/.gitignore`.
- **Preserve `com.gdx.dispatch` package ID:** This matches the Google Play Console application identifier. GDX Planner's `com.gdx.planner` is set only on its flavor; the Java namespace stays `com.gdx.dispatch` for both.

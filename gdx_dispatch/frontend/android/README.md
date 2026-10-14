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
   - Native push notifications (Firebase Cloud Messaging).

---

## 2. Directory Structure

- `app/` — Main Android application module:
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

### Compiling Debug APK
Using the environment's JDK 21 and Android SDK:
```bash
cd gdx_dispatch/frontend/android
JAVA_HOME=/home/doug/android-studio/jbr ANDROID_HOME=/home/doug/Android/Sdk ./gradlew assembleDebug
```
Output APK: `gdx_dispatch/frontend/android/app/build/outputs/apk/debug/app-debug.apk`

### Launching on Emulator
To run on the local `Pixel_8` AVD:
```bash
/home/doug/Android/Sdk/emulator/emulator -avd Pixel_8 &
adb wait-for-device
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n com.gdx.dispatch/com.gdx.dispatch.MainActivity
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
The Google Play App Bundle will be output at:
`gdx_dispatch/frontend/android/app/build/outputs/bundle/release/app-release.aab`

---

## 5. Invariants for Agents & Contributors

- **DO NOT delete this directory:** This project is intentionally tracked to provide reproducible Android builds for field technicians and Google Play releases.
- **Do not commit `.apk`, `.aab`, `.jks`, `.keystore`, `.gradle/`, or `build/`:** These are ignored by `android/.gitignore`.
- **Preserve `com.gdx.dispatch` package ID:** This matches the Google Play Console application identifier.

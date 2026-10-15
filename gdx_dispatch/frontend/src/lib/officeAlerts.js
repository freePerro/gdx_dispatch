/**
 * officeAlerts.js — "Alerts on this phone" for office users in the Android app.
 *
 * Owner Domain: mobile-tech (see .claude/agents/mobile-tech.md)
 *
 * The native OfficeAlerts plugin runs
 * a foreground service that long-polls /api/notifications/device-alerts/feed
 * with an opaque device token and posts each new bell row as an Android
 * notification. This module mints that token with the signed-in session,
 * hands it to the service, re-mints it weekly, revokes it on a logout the
 * user chose or when someone else signs in on the phone, and routes a tapped
 * notification to the page the bell would open.
 *
 * Every export is a no-op off native Android.
 */

import { Capacitor, registerPlugin } from '@capacitor/core';
import { getServerUrl } from './serverConfig';
import { destinationFor } from './notificationDestinations';
import { isPlannerApp } from './appMode';

const OfficeAlerts = registerPlugin('OfficeAlerts');

const TOKEN_PATH = '/api/notifications/device-alerts/token';
// Whose alerts this phone shows. An automatic logout leaves alerts on, so the
// next person to sign in must not inherit them (/audit 2026-10-14).
export const OWNER_KEY = 'gdx_office_alerts_owner';

function _sameUser(a, b) {
  const norm = (v) => String(v || '').replace(/-/g, '').toLowerCase();
  return norm(a) !== '' && norm(a) === norm(b);
}
// The server refuses a token after 90 days; a weekly re-mint keeps a phone
// that is opened now and then from ever reaching that.
export const ROTATE_AFTER_MS = 7 * 24 * 60 * 60 * 1000;
// Changes run one at a time, so a mint that never answers must not hold the
// queue (a chosen logout waits behind it).
export const MINT_TIMEOUT_MS = 20_000;

// One change at a time: an owner check, a re-mint, the toggle and a logout
// could otherwise interleave — a check landing between a fresh token's save
// and its owner's would revoke that token as someone else's (/audit
// 2026-10-14). Every exported action that changes alerts goes through this.
let _queue = Promise.resolve();
function _serial(fn) {
  const run = _queue.then(fn);
  _queue = run.catch(() => {});
  return run;
}

const OFF = Object.freeze({
  enabled: false,
  running: false,
  notificationsAllowed: false,
  batteryUnrestricted: false,
  tokenSavedAt: 0,
});

export function isOfficeAlertsAvailable() {
  return Capacitor.isNativePlatform() && Capacitor.getPlatform() === 'android';
}

function _serverUrl() {
  // A native build talks to the configured server; never the WebView origin.
  return getServerUrl() || '';
}

function _deviceLabel() {
  try {
    const ua = navigator.userAgent || '';
    const m = ua.match(/Android [\d.]+; ([^;)]+)/);
    return (m ? m[1].trim() : 'Android phone').slice(0, 200);
  } catch {
    return 'Android phone';
  }
}

export async function getOfficeAlertsStatus() {
  if (!isOfficeAlertsAvailable()) return { ...OFF };
  try {
    return { ...OFF, ...(await OfficeAlerts.status()) };
  } catch (err) {
    console.warn('Office alerts status failed:', err);
    return { ...OFF };
  }
}

async function _mintAndStart(api) {
  const serverUrl = _serverUrl();
  if (!serverUrl) throw new Error('Set the server address first (More → Server Settings).');
  const mint = api.post(TOKEN_PATH, { device_label: _deviceLabel() });
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      // The server may still answer, with a token nothing will use: revoke it.
      mint.then(({ token: late } = {}) => late && OfficeAlerts.revokeToken({ serverUrl, token: late }))
        .catch(() => {});
      reject(new Error('The server did not answer. Try again.'));
    }, MINT_TIMEOUT_MS);
  });
  const { token, user_id: owner } = await Promise.race([mint, timeout])
    .finally(() => clearTimeout(timer));
  // The owner is recorded before the service has the token, never after.
  try { localStorage.setItem(OWNER_KEY, String(owner || '')); } catch { /* ignore */ }
  // The plugin revokes the token this one replaces. GDX Planner's feed carries
  // the user's own planner reminders; GDX Dispatch's the office's alerts.
  const scope = isPlannerApp() ? 'planner' : 'office';
  return { ...OFF, ...(await OfficeAlerts.start({ serverUrl, token, scope })) };
}

/**
 * Turn alerts on: notification permission (asked by the plugin) → mint a
 * device token with the session → start the service. `api` is a client with
 * `post(url, body)` that returns parsed JSON (createApiClient / useApi).
 */
export async function enableOfficeAlerts(api) {
  if (!isOfficeAlertsAvailable()) return { ...OFF };
  return _serial(() => _mintAndStart(api));
}

/** Turn alerts off and revoke this phone's token on the server. */
export async function disableOfficeAlerts(reason = 'disabled') {
  if (!isOfficeAlertsAvailable()) return { ...OFF };
  return _serial(() => _disable(reason));
}

async function _disable(reason) {
  try { localStorage.removeItem(OWNER_KEY); } catch { /* ignore */ }
  try {
    return { ...OFF, ...(await OfficeAlerts.stop({ revoke: true, reason })) };
  } catch (err) {
    console.warn('Could not stop office alerts:', err);
    return { ...OFF };
  }
}

/** Ask Android to let the service run unrestricted (Doze, app standby). */
export async function openBatterySettings() {
  if (!isOfficeAlertsAvailable()) return { ...OFF };
  return { ...OFF, ...(await OfficeAlerts.openBatterySettings()) };
}

/**
 * Any signed-in user: if this phone shows someone else's alerts, turn them
 * off and revoke them. Alerts with no recorded owner count as someone else's.
 */
export function checkOfficeAlertsOwner(userId) {
  return _serial(() => _checkOwner(userId));
}

async function _checkOwner(userId) {
  const status = await getOfficeAlertsStatus();
  if (!status.enabled || !userId) return status;
  let owner = '';
  try { owner = localStorage.getItem(OWNER_KEY) || ''; } catch { /* ignore */ }
  if (_sameUser(owner, userId)) return status;
  return _disable('user_changed');
}

/**
 * On app start with a signed-in office user: if alerts were on, make sure the
 * service runs, re-minting the token once it is a week old. Never turns
 * alerts on by itself and never throws. Without knowing who is signed in it
 * neither resumes nor mints: those would carry the last owner's alerts over.
 */
export function ensureOfficeAlerts(api, opts = {}) {
  return _serial(() => _ensure(api, opts));
}

async function _ensure(api, { now = Date.now(), userId } = {}) {
  if (!userId) return getOfficeAlertsStatus();
  const status = await _checkOwner(userId);
  if (!status.enabled) return status;
  try {
    const serverUrl = _serverUrl();
    // A different server is a different install: this token means nothing there.
    if (serverUrl && status.serverUrl && serverUrl !== status.serverUrl) {
      return await _disable('disabled');
    }
    if (!status.tokenSavedAt || now - status.tokenSavedAt > ROTATE_AFTER_MS) {
      // Notifications blocked since alerts went on: a re-mint would raise a
      // permission prompt nobody asked for, and a refusal orphans the token.
      if (!status.notificationsAllowed) return status;
      return await _mintAndStart(api);
    }
    if (!status.running) {
      // The service may have been killed; starting it again is idempotent.
      return { ...OFF, ...(await OfficeAlerts.resume()) };
    }
    return status;
  } catch (err) {
    console.warn('Office alerts check failed:', err);
    return status;
  }
}

/**
 * The app-wide keeper calls this when the signed-in user changes, and once
 * per office user: someone else's alerts go off; an office user's are kept
 * running (`office: true`).
 */
export function syncOfficeAlerts(api, { userId, office = false, now } = {}) {
  return office ? ensureOfficeAlerts(api, { userId, now }) : checkOfficeAlertsOwner(userId);
}

let _bridgeInstalled = false;

/**
 * Route notification taps and stop alerts on logout. Call once at startup.
 * A tap that cold-starts the app is retained by the plugin until this
 * listener attaches, so it is not lost. `ready` is the app-mode detection
 * main.js waits on before installing the router: a tap is routed only once
 * the app knows it is GDX Planner, or a cold-start tap would land on the
 * dispatch app's planner page and the router would skip its first guard.
 */
export function installOfficeAlertsBridge(router, ready = Promise.resolve()) {
  if (_bridgeInstalled || !isOfficeAlertsAvailable()) return;
  _bridgeInstalled = true;
  const go = async (category) => {
    await ready.catch(() => {});
    const dest = destinationFor(category, { mobile: true }) || '/mobile';
    // A second tap to the page already showing is a duplicate navigation the
    // router drops, so the tap is also announced for a page that is open.
    router.push(dest).catch(() => {}).finally(() => {
      window.dispatchEvent(new CustomEvent('gdx:alert-tap', { detail: { category, dest } }));
    });
  };
  try {
    OfficeAlerts.addListener('alertTap', ({ category } = {}) => go(category));
  } catch (err) {
    console.warn('Office alerts tap listener failed:', err);
  }
  // Signing out from the menu stops the phone showing the office's
  // notifications too; the plugin revokes with the device token itself. An
  // automatic logout (expired session, any 401, idle timer) leaves alerts on:
  // the device token outlives the session by design, and prod logs thousands
  // of automatic logouts a month (/audit 2026-10-14).
  window.addEventListener('gdx:auth-logout', (e) => {
    if (e?.detail?.chosen) disableOfficeAlerts('logout');
  });
}

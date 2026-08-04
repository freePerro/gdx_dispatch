// Phase 1.5 E1+E2 — frontend service-worker registration + Web Push
// subscription flow. Exported helpers:
//
//   registerServiceWorker()        — idempotent SW install at /sw.js
//   getCurrentPermission()         — Notification.permission shorthand
//   subscribeToPush(api)           — full subscribe flow:
//                                     1. ensure SW registered
//                                     2. fetch tenant's VAPID public key
//                                     3. ask Notification.requestPermission()
//                                     4. PushManager.subscribe()
//                                     5. POST /api/push/v2/subscribe
//   unsubscribeFromPush(api)       — reverses subscribe, posts /unsubscribe
//
// Pure JS (no Vue reactive state) so it's unit-testable; the calling
// component owns the loading/error UI.

export function isPushSupported() {
  return (
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window
  );
}

export function getCurrentPermission() {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported';
  return Notification.permission;  // 'default' | 'granted' | 'denied'
}

export async function registerServiceWorker(swUrl = '/sw.js') {
  if (!isPushSupported()) return null;
  try {
    const existing = await navigator.serviceWorker.getRegistration(swUrl);
    if (existing) return existing;
    return await navigator.serviceWorker.register(swUrl);
  } catch (err) {
    console.warn('[push] sw register failed:', err);
    return null;
  }
}

// Per the Web Push spec the applicationServerKey is a Uint8Array of the
// raw VAPID public key. The backend serves the standard urlBase64-encoded
// form; the browser needs it decoded.
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

export async function subscribeToPush(api) {
  if (!isPushSupported()) {
    return { ok: false, reason: 'unsupported' };
  }

  const reg = await registerServiceWorker();
  if (!reg) return { ok: false, reason: 'sw_register_failed' };

  // Permission flow — must be triggered by a user gesture, so the caller
  // should invoke this from a click handler.
  let perm = Notification.permission;
  if (perm === 'default') {
    perm = await Notification.requestPermission();
  }
  if (perm !== 'granted') {
    return { ok: false, reason: 'permission_denied', permission: perm };
  }

  let vapid;
  try {
    const r = await api.get('/api/push/v2/vapid-public');
    vapid = r?.public_key || '';
  } catch {
    vapid = '';
  }
  if (!vapid) {
    return { ok: false, reason: 'no_vapid_key' };
  }

  let sub;
  try {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapid),
    });
  } catch (err) {
    console.warn('[push] PushManager.subscribe failed:', err);
    return { ok: false, reason: 'subscribe_failed' };
  }

  const json = sub.toJSON();
  const keys = json.keys || {};
  try {
    await api.post('/api/push/v2/subscribe', {
      endpoint: json.endpoint,
      p256dh: keys.p256dh,
      auth: keys.auth,
      user_agent: navigator.userAgent,
    });
  } catch (err) {
    console.warn('[push] backend subscribe failed:', err);
    // Roll back the browser subscription so we don't end up with a
    // "subscribed in browser, unknown to server" zombie.
    try { await sub.unsubscribe(); } catch { /* swallow */ }
    return { ok: false, reason: 'backend_failed' };
  }

  return { ok: true, endpoint: json.endpoint };
}

export async function unsubscribeFromPush(api) {
  if (!isPushSupported()) return { ok: false, reason: 'unsupported' };
  const reg = await navigator.serviceWorker.getRegistration('/sw.js');
  if (!reg) return { ok: false, reason: 'no_registration' };
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return { ok: false, reason: 'no_subscription' };
  const endpoint = sub.endpoint;
  try {
    await sub.unsubscribe();
  } catch (err) {
    console.warn('[push] browser unsubscribe failed:', err);
  }
  try {
    await api.delete('/api/push/v2/unsubscribe', { endpoint });
  } catch (err) {
    console.warn('[push] backend unsubscribe failed:', err);
  }
  return { ok: true, endpoint };
}

// 2026-08-04: heal the granted-but-unsubscribed state. Permission is a
// browser-level grant; the subscription is a separate object that can be
// missing (VAPID keys configured AFTER the user granted — the prod v1.37.0
// state — or the push service expired the subscription). subscribe() needs
// no user gesture once permission is already granted, so this is safe to
// run on app open. Returns { ok, healed } — healed=true only when a new
// subscription was actually created.
export async function ensureSubscribed(api) {
  if (!isPushSupported()) return { ok: false, healed: false, reason: 'unsupported' };
  if (getCurrentPermission() !== 'granted') {
    return { ok: false, healed: false, reason: 'not_granted' };
  }
  const reg = await registerServiceWorker();
  if (!reg) return { ok: false, healed: false, reason: 'sw_register_failed' };
  let existing = null;
  try {
    existing = await reg.pushManager.getSubscription();
  } catch { /* treat as missing */ }
  if (existing) return { ok: true, healed: false };
  const r = await subscribeToPush(api);
  return { ok: r.ok, healed: r.ok, reason: r.reason };
}

// Fetch the tenant's VAPID public key; empty string = push not configured
// server-side (the CTA should not render — tapping it is a dead end).
export async function fetchVapidPublicKey(api) {
  try {
    const r = await api.get('/api/push/v2/vapid-public', { suppressErrorToast: true });
    return r?.public_key || '';
  } catch {
    return '';
  }
}

// Exported only for tests.
export const _internal = { urlBase64ToUint8Array };

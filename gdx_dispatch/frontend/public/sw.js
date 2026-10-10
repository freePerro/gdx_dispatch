/* GDX service worker — Phase 1.5 (sprint_tech_mobile) E1.
 *
 * Single responsibility right now: receive Web Push messages and surface
 * them as native browser notifications, with a click handler that focuses
 * (or opens) the right GDX URL. Caching / offline support is deliberately
 * NOT in scope — that lands in Sprint 3 (Phase 3.1 offline mode), and
 * adding it here without a strategy would silently freeze the app at
 * stale bundles after a deploy.
 */

self.addEventListener('install', (event) => {
  // No precache — skip waiting so an updated SW takes over on next nav.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  if (!event.data) return;
  let payload = {};
  try {
    payload = event.data.json();
  } catch (_e) {
    payload = { title: 'GDX', body: event.data.text() };
  }
  const title = payload.title || 'GDX';
  const opts = {
    body: payload.body || '',
    icon: payload.icon || '/icons/icon-192.png',
    badge: payload.badge || '/icons/icon-192.png',
    data: { url: payload.url || '/dashboard', payload: payload.data || null },
    tag: payload.tag || undefined,
    renotify: payload.renotify || false,
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});

// How long a tab gets to acknowledge the route handoff before the worker falls
// back to navigate(). A tab still running a pre-deploy bundle has no listener
// and never answers. The SPA refuses a handoff past HANDOFF_ACCEPT_MS, so a
// tab that wakes late does not route while the worker is navigating it too.
const HANDOFF_ACK_MS = 1500;
const HANDOFF_ACCEPT_MS = 1000;

// Same-origin path only; anything else becomes the dashboard.
function notificationTarget(raw) {
  try {
    const u = new URL(raw || '/dashboard', self.location.origin);
    if (u.origin !== self.location.origin) return '/dashboard';
    return u.pathname + u.search + u.hash;
  } catch (_e) {
    return '/dashboard';
  }
}

// The tab the user is looking at, else one that is visible, else any.
function pickClient(wins) {
  return (
    wins.find((w) => w.focused === true) ||
    wins.find((w) => w.visibilityState === 'visible') ||
    wins[0] ||
    null
  );
}

// Ask the SPA to router.push(url) instead of reloading the tab, so its stores
// and session survive. Resolves true only when the SPA acknowledges.
function handOff(client, url) {
  return new Promise((resolve) => {
    let channel;
    try {
      channel = new MessageChannel();
    } catch (_e) {
      resolve(false);
      return;
    }
    let timer = null;
    const settle = (ok) => {
      clearTimeout(timer);
      channel.port1.onmessage = null;
      channel.port1.close();
      resolve(ok);
    };
    timer = setTimeout(() => settle(false), HANDOFF_ACK_MS);
    channel.port1.onmessage = (e) => settle(e.data?.ok === true);
    try {
      client.postMessage(
        { type: 'notification-click', url, acceptBefore: Date.now() + HANDOFF_ACCEPT_MS },
        [channel.port2],
      );
    } catch (_e) {
      settle(false);
    }
  });
}

async function openNotificationTarget(rawUrl) {
  const target = notificationTarget(rawUrl);
  const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const client = pickClient(wins);
  if (!client) {
    if (self.clients.openWindow) await self.clients.openWindow(target);
    return;
  }
  try {
    await client.focus();
  } catch (_e) { /* focus can be refused; the handoff still works */ }
  if (await handOff(client, target)) return;
  // No SPA listener answered: a full navigation is the fallback. navigate()
  // rejects for an uncontrolled client, so a new window is the last resort.
  try {
    if (typeof client.navigate !== 'function') throw new Error('no navigate');
    await client.navigate(target);
  } catch (_e) {
    if (self.clients.openWindow) await self.clients.openWindow(target);
  }
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(openNotificationTarget(event.notification?.data?.url));
});

self.addEventListener('pushsubscriptionchange', (event) => {
  // The browser auto-rotated the endpoint. Re-subscribe and POST the
  // new keys to /api/push/v2/subscribe. Without this, push silently
  // stops working after the rotation.
  event.waitUntil((async () => {
    try {
      const newSub = await self.registration.pushManager.subscribe(
        event.oldSubscription?.options || { userVisibleOnly: true },
      );
      const json = newSub.toJSON();
      const keys = json.keys || {};
      // No JWT in the SW — best we can do is tag the request and let the
      // backend reconcile with whatever auth is on the client. Frontend
      // checks /api/push/v2/me on next load and re-subscribes if absent.
      await fetch('/api/push/v2/subscribe', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          endpoint: json.endpoint,
          p256dh: keys.p256dh,
          auth: keys.auth,
        }),
      });
    } catch (_e) {
      /* swallow — frontend reconciles on next session */
    }
  })());
});

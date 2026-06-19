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
    icon: payload.icon || '/static/icon-192.png',
    badge: payload.badge || '/static/icon-192.png',
    data: { url: payload.url || '/dashboard', payload: payload.data || null },
    tag: payload.tag || undefined,
    renotify: payload.renotify || false,
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification?.data?.url || '/dashboard';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      // If a GDX tab is already open, focus it and navigate.
      for (const w of wins) {
        if ('focus' in w) {
          w.focus();
          if ('navigate' in w) {
            try { w.navigate(target); } catch (_e) { /* old browsers */ }
          }
          return;
        }
      }
      // Otherwise open a new tab.
      if (self.clients.openWindow) {
        return self.clients.openWindow(target);
      }
    }),
  );
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

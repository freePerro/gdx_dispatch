// GDX Service Worker — kill-switch.
//
// This file exists ONLY to dismantle any previously-installed GDX service
// worker and wipe its caches. A prior deploy shipped a cache-first SW that
// pre-cached the HTML shell, stranding returning visitors on stale Vue
// chunks after every frontend rebuild (ForgotPasswordView-kX2FGQDn.css 404
// on 2026-04-11 is the incident that prompted this).
//
// 2026-08-03: the Vue frontend now DOES ship a real SW (frontend/public/sw.js
// — Web Push display only, no fetch handler). core/pwa.py serves that one
// when a dist build exists; this kill-switch is only served from backend-only
// images with no frontend build, where the right move is still to dismantle
// whatever SW a browser has installed.
//
// On next visit, the browser fetches /sw.js, byte-compares it to the
// installed version, sees it has changed, and installs this kill-switch.
// activate() then deletes every cache, unregisters the SW, and force-reloads
// any open tabs so they get fresh HTML + assets from the network.
//
// Do not add fetch handlers, precache lists, or background sync here. When a
// real PWA is needed for field offline work, rebuild it with vite-plugin-pwa
// / Workbox — not by extending this file.

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    try {
      const cacheNames = await caches.keys();
      await Promise.all(cacheNames.map((name) => caches.delete(name)));
    } catch (_) {
      // If caches API is unavailable or throws, proceed with unregister anyway.
    }
    try {
      await self.registration.unregister();
    } catch (_) {
      // Unregister is best-effort; the browser will retry on next visit.
    }
    try {
      const windowClients = await self.clients.matchAll({ type: 'window' });
      windowClients.forEach((client) => {
        if ('navigate' in client) {
          client.navigate(client.url);
        }
      });
    } catch (_) {
      // If clients API fails, the user's next hard reload finishes the job.
    }
  })());
});

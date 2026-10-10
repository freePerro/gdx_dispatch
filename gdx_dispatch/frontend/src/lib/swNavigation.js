// Receives the service worker's notification-click route handoff (public/sw.js)
// and routes inside the SPA, so a tapped notification moves the tab like a
// sidebar click rather than reloading it. The worker falls back to navigate()
// when no ack comes. A routed view still unmounts: an unsaved form survives
// only through useFormDraft, or a beforeunload guard (see below).

// A same-origin in-app path, or null. Rejects absolute and protocol-relative
// URLs, and anything that resolves off this origin.
export function safeAppPath(url, origin = window.location.origin) {
  if (typeof url !== 'string' || !url.startsWith('/') || url.startsWith('//') || url.startsWith('/\\')) {
    return null;
  }
  try {
    const u = new URL(url, origin);
    if (u.origin !== origin) return null;
    return u.pathname + u.search + u.hash;
  } catch (_e) {
    return null;
  }
}

// True when a beforeunload guard (e.g. SettingsView's pending module toggles)
// would warn. router.push never fires beforeunload, so such a page declines the
// handoff and the worker's navigate() raises the browser's own leave prompt.
export function pageWouldWarnOnLeave() {
  if (typeof window === 'undefined') return false;
  const probe = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(probe);
  return probe.defaultPrevented;
}

export function handleSwMessage(event, router, origin = window.location.origin, now = Date.now()) {
  const data = event?.data;
  if (!data || data.type !== 'notification-click') return false;
  if (event.origin && event.origin !== origin) return false;
  const port = event.ports?.[0];
  const path = safeAppPath(data.url, origin);
  // Past acceptBefore the worker has given up on us and is navigating the tab
  // itself; routing as well would move it twice.
  const late = typeof data.acceptBefore === 'number' && now > data.acceptBefore;
  if (!path || late || pageWouldWarnOnLeave()) {
    port?.postMessage({ ok: false });
    return false;
  }
  // Ack before routing: the worker only needs to know the SPA took it.
  port?.postMessage({ ok: true });
  router.push(path).catch(() => { /* a router guard refused it; the tab stays put */ });
  return true;
}

export function installSwNavigation(router) {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return null;
  const listener = (event) => handleSwMessage(event, router);
  navigator.serviceWorker.addEventListener('message', listener);
  return () => navigator.serviceWorker.removeEventListener('message', listener);
}

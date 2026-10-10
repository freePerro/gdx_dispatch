// GDXA-406: a tapped notification hands its route to the SPA (router.push)
// instead of navigate()-ing whichever tab matchAll listed first, which reloaded
// it and lost any unsaved form. The worker runs here against a stub `self`.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { handleSwMessage, installSwNavigation, safeAppPath } from '../lib/swNavigation';

const SW_SRC = readFileSync(join(__dirname, '..', '..', 'public', 'sw.js'), 'utf8');
const ORIGIN = 'https://gdx.example';

function loadWorker(wins) {
  const handlers = {};
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting: () => {},
    clients: {
      matchAll: vi.fn(async () => wins),
      openWindow: vi.fn(async () => null),
      claim: async () => {},
    },
  };
  new Function('self', SW_SRC)(self);
  const click = async (url) => {
    let done;
    handlers.notificationclick({
      notification: { close: vi.fn(), data: { url } },
      waitUntil: (p) => { done = p; },
    });
    await done;
  };
  return { self, click };
}

// ack: true/false answers on the port; null never answers (pre-deploy bundle).
function client({ focused = false, visibilityState = 'hidden', ack = true, navigate = 'ok' } = {}) {
  return {
    focused,
    visibilityState,
    focus: vi.fn(async () => {}),
    postMessage: vi.fn((msg, ports) => {
      if (ack !== null) ports[0].postMessage({ ok: ack });
    }),
    navigate: vi.fn(async () => {
      if (navigate === 'reject') throw new TypeError('not controlled');
    }),
  };
}

afterEach(() => vi.useRealTimers());

describe('sw.js notificationclick', () => {
  // A conforming matchAll lists the most recently focused tab first, so this
  // order is defensive; the visible-tab fallback below is the case that matters.
  it('prefers the focused tab over an earlier-listed one', async () => {
    const first = client();
    const focused = client({ focused: true });
    const { click } = loadWorker([first, focused]);
    await click('/jobs/7');
    expect(focused.postMessage).toHaveBeenCalledWith(
      { type: 'notification-click', url: '/jobs/7', acceptBefore: expect.any(Number) },
      expect.any(Array),
    );
    expect(first.postMessage).not.toHaveBeenCalled();
    expect(first.focus).not.toHaveBeenCalled();
  });

  it('falls back to a visible tab, then to the first', async () => {
    const hidden = client();
    const visible = client({ visibilityState: 'visible' });
    await loadWorker([hidden, visible]).click('/a');
    expect(visible.postMessage).toHaveBeenCalled();
    expect(hidden.postMessage).not.toHaveBeenCalled();

    const only = client();
    await loadWorker([only, client()]).click('/b');
    expect(only.postMessage).toHaveBeenCalled();
  });

  it('an acknowledged handoff never navigates the tab', async () => {
    const tab = client({ focused: true });
    const { self, click } = loadWorker([tab]);
    await click('/inbox');
    expect(tab.focus).toHaveBeenCalled();
    expect(tab.navigate).not.toHaveBeenCalled();
    expect(self.clients.openWindow).not.toHaveBeenCalled();
  });

  it('no ack (old bundle) falls back to navigate() only after the SPA deadline has passed', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    const t0 = Date.now();
    const tab = client({ focused: true, ack: null });
    const { self, click } = loadWorker([tab]);
    const p = click('/inbox');
    await vi.advanceTimersByTimeAsync(0);
    const acceptWindow = tab.postMessage.mock.calls[0][0].acceptBefore - t0;
    expect(acceptWindow).toBeGreaterThan(0);
    // A tab may still accept up to acceptBefore; the worker must not have
    // navigated yet, or a late tab would be moved twice.
    await vi.advanceTimersByTimeAsync(acceptWindow);
    expect(tab.navigate).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1500 - acceptWindow);
    await p;
    expect(tab.navigate).toHaveBeenCalledWith('/inbox');
    expect(self.clients.openWindow).not.toHaveBeenCalled();
  });

  it('a rejected navigate() (uncontrolled tab) opens a window instead of doing nothing', async () => {
    const tab = client({ focused: true, ack: false, navigate: 'reject' });
    const { self, click } = loadWorker([tab]);
    await click('/inbox');
    expect(tab.navigate).toHaveBeenCalled();
    expect(self.clients.openWindow).toHaveBeenCalledWith('/inbox');
  });

  it('no tab open: opens one; an off-origin url becomes the dashboard', async () => {
    const { self, click } = loadWorker([]);
    await click('https://evil.example/x');
    expect(self.clients.openWindow).toHaveBeenCalledWith('/dashboard');
    await click('/jobs?tab=open#n');
    expect(self.clients.openWindow).toHaveBeenLastCalledWith('/jobs?tab=open#n');
  });
});

describe('SPA notification-click listener', () => {
  const router = () => ({ push: vi.fn(async () => {}) });
  const msg = (url, extra = {}) => {
    const port = { postMessage: vi.fn() };
    return { event: { origin: ORIGIN, data: { type: 'notification-click', url }, ports: [port], ...extra }, port };
  };

  it('pushes a same-origin path and acks', () => {
    const r = router();
    const { event, port } = msg('/jobs/7?x=1#y');
    expect(handleSwMessage(event, r, ORIGIN)).toBe(true);
    expect(r.push).toHaveBeenCalledWith('/jobs/7?x=1#y');
    expect(port.postMessage).toHaveBeenCalledWith({ ok: true });
  });

  it.each([
    'https://evil.example/x',
    '//evil.example/x',
    '/\\evil.example',
    'javascript:alert(1)',
    'jobs/7',
    '',
    42,
    null,
  ])('rejects %p without routing', (url) => {
    const r = router();
    const { event, port } = msg(url);
    expect(handleSwMessage(event, r, ORIGIN)).toBe(false);
    expect(r.push).not.toHaveBeenCalled();
    expect(port.postMessage).toHaveBeenCalledWith({ ok: false });
  });

  it('ignores other message types and foreign-origin senders', () => {
    const r = router();
    expect(handleSwMessage({ origin: ORIGIN, data: { type: 'other', url: '/a' }, ports: [] }, r, ORIGIN)).toBe(false);
    expect(handleSwMessage(msg('/a', { origin: 'https://evil.example' }).event, r, ORIGIN)).toBe(false);
    expect(r.push).not.toHaveBeenCalled();
  });

  it('a page whose beforeunload guard would warn declines, so navigate() prompts', () => {
    const guard = (e) => e.preventDefault();
    window.addEventListener('beforeunload', guard);
    try {
      const r = router();
      const { event, port } = msg('/a');
      expect(handleSwMessage(event, r, ORIGIN)).toBe(false);
      expect(r.push).not.toHaveBeenCalled();
      expect(port.postMessage).toHaveBeenCalledWith({ ok: false });
    } finally {
      window.removeEventListener('beforeunload', guard);
    }
    expect(handleSwMessage(msg('/a').event, router(), ORIGIN)).toBe(true);
  });

  it('a push refused by a router guard does not throw', async () => {
    const r = { push: vi.fn(() => Promise.reject(new Error('aborted'))) };
    expect(handleSwMessage(msg('/a').event, r, ORIGIN)).toBe(true);
    await Promise.resolve();
  });

  it('refuses a handoff that arrives after the worker gave up on it', () => {
    const r = router();
    const { event, port } = msg('/a');
    event.data.acceptBefore = 1000;
    expect(handleSwMessage(event, r, ORIGIN, 1001)).toBe(false);
    expect(r.push).not.toHaveBeenCalled();
    expect(port.postMessage).toHaveBeenCalledWith({ ok: false });
    expect(handleSwMessage(msg('/a').event, r, ORIGIN, 1001)).toBe(true);
  });

  it('installSwNavigation listens on navigator.serviceWorker, and uninstalls', () => {
    const listeners = new Set();
    const container = {
      addEventListener: (t, fn) => t === 'message' && listeners.add(fn),
      removeEventListener: (t, fn) => listeners.delete(fn),
    };
    Object.defineProperty(navigator, 'serviceWorker', { value: container, configurable: true });
    try {
      const r = router();
      const off = installSwNavigation(r);
      const { event } = msg('/jobs/1', { origin: window.location.origin });
      listeners.forEach((fn) => fn(event));
      expect(r.push).toHaveBeenCalledWith('/jobs/1');
      off();
      expect(listeners.size).toBe(0);
    } finally {
      delete navigator.serviceWorker;
    }
  });

  it('safeAppPath keeps path, query and hash only', () => {
    expect(safeAppPath('/a/../b?q=1#h', ORIGIN)).toBe('/b?q=1#h');
  });
});

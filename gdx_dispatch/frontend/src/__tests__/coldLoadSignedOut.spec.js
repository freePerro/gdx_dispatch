/**
 * A signed-out cold load makes no authenticated API call (GDXA-1023).
 *
 * main.js mounts the app without awaiting router.isReady(), so the first
 * render sees START_LOCATION — no matched record, no meta. App.vue used to read
 * that as "not noShell" and mount AppLayout for one tick; AppTopbar's
 * notifications poll then fired /api/notifications/count and drew a 401 before
 * the guard landed on /login. This boots the real App with the real router the
 * same way main.js does and records every request it makes.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import { createPinia } from 'pinia';
import PrimeVue from 'primevue/config';
import ToastService from 'primevue/toastservice';
import ConfirmationService from 'primevue/confirmationservice';
import Tooltip from 'primevue/tooltip';
import App from '../App.vue';
import { createAppRouter } from '../router';

// Endpoints a signed-out page may call: they answer without a token.
const PUBLIC_API = ['/api/settings/branding'];

let requested;
let shellAtMount;
let app;
let host;

function urlOf(input) {
  const raw = typeof input === 'string' ? input : input?.url ?? String(input);
  return new URL(raw, window.location.origin).pathname;
}

async function coldLoad(path, { extraRoute } = {}) {
  window.history.replaceState(null, '', path);
  app = createApp(App);
  const router = createAppRouter();
  if (extraRoute) router.addRoute(extraRoute);
  app.use(createPinia());
  app.use(router);
  app.use(PrimeVue, { unstyled: true });
  app.use(ToastService);
  app.use(ConfirmationService);
  app.directive('tooltip', Tooltip);
  host = document.createElement('div');
  document.body.appendChild(host);
  // Mount BEFORE the router is ready, exactly as main.js does.
  app.mount(host);
  shellAtMount = Boolean(host.querySelector('.app-topbar'));
  await router.isReady().catch(() => {});
  for (let i = 0; i < 5; i += 1) await nextTick();
  await new Promise((r) => setTimeout(r, 0));
  return router;
}

describe('signed-out cold load', () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    requested = [];
    vi.stubGlobal('fetch', vi.fn(async (input) => {
      requested.push(urlOf(input));
      return { ok: false, status: 401, json: async () => ({}), text: async () => '' };
    }));
  });

  afterEach(() => {
    app?.unmount();
    host?.remove();
    vi.unstubAllGlobals();
  });

  it.each(['/dashboard', '/login', '/'])('%s lands on /login without calling a private /api endpoint', async (path) => {
    const router = await coldLoad(path);
    expect(router.currentRoute.value.path).toBe('/login');
    const privateCalls = requested.filter((p) => p.startsWith('/api/') && !PUBLIC_API.includes(p));
    expect(privateCalls).toEqual([]);
  });

  it('does not render the shell before the first navigation resolves', async () => {
    await coldLoad('/dashboard');
    expect(shellAtMount).toBe(false);
    expect(host.querySelector('.app-topbar')).toBeNull();
  });

  it('falls back to the shell when the first navigation fails, still with no private call', async () => {
    // A lazy chunk that will not load (weak signal) after router.onError's
    // one-shot reload is spent: the route never resolves. A blank page here
    // would leave no way out; the shell must come back, and its auth gates
    // must still keep a signed-out session off the private endpoints.
    sessionStorage.setItem('gdx:chunk-reload-at', String(Date.now()));
    await coldLoad('/chunk-fails', {
      extraRoute: {
        path: '/chunk-fails',
        meta: { public: true },
        component: () => Promise.reject(new Error('Failed to fetch dynamically imported module')),
      },
    });
    expect(host.querySelector('.app-topbar')).not.toBeNull();
    const privateCalls = requested.filter((p) => p.startsWith('/api/') && !PUBLIC_API.includes(p));
    expect(privateCalls).toEqual([]);
  });

  it('still mounts the shell and starts the bell poll once signed in', async () => {
    // The other half of the contract: deferring the shell must not drop it.
    sessionStorage.setItem('gdx_access_token', 'opaque-test-token');
    sessionStorage.setItem('gdx_user', JSON.stringify({ name: 'Office', role: 'admin' }));
    vi.stubGlobal('fetch', vi.fn(async (input) => {
      requested.push(urlOf(input));
      return { ok: true, status: 200, json: async () => ({}), text: async () => '{}' };
    }));
    const router = await coldLoad('/settings');
    expect(router.currentRoute.value.path).toBe('/settings');
    expect(host.querySelector('.app-topbar')).not.toBeNull();
    expect(requested).toContain('/api/notifications/count');
  });
});

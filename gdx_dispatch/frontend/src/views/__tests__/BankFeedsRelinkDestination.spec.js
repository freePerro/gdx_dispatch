/**
 * Bank Feeds — clicking "Re-link in Settings" navigates somewhere that can
 * actually re-link. This is the guard that crosses the seam.
 *
 * The two halves of this link already had tests and both were green while the
 * link was dead for five weeks: SettingsView was taught to read `?tab=` in
 * 8de3fe2, and the same commit gave the button a path that has never existed
 * (`/settings/integrations`). A sender test and a receiver test cannot catch
 * that — only a test that presses the real button and looks at where it lands.
 *
 * So this spec asserts the PAYLOAD, not just the path:
 *
 *   - `client-deep-links-resolve.spec.js` resolves paths with
 *     `path.split('?')[0]`, discarding the query by construction. Deleting the
 *     `?tab=` half leaves it green.
 *   - `SettingsTabDeepLink.spec.js` hardcodes `?tab=integrations` and never
 *     opens BankFeedsView. Changing what the button sends leaves it green.
 *
 * Red-test check (what makes this able to fail) — both mutations were run:
 *   (a) `query: { tab: 'integrationz' }`  → fails on the SETTINGS_TABS
 *       membership assertion, naming the bad value;
 *   (b) `$router.push('/settings')`        → fails on the fullPath assertion.
 * The full vitest gate stays green for both without this file.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { createRouter, createMemoryHistory } from 'vue-router';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { routes } from '../../router/index.js';

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet, post: apiPost, patch: vi.fn(), delete: vi.fn(), del: vi.fn(), put: vi.fn(),
  }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
// `canManage` gates the whole actions column the button lives in. Office staff
// repairing a bank feed hold bank_feeds.manage; without it there is no button
// to press and this spec would pass vacuously — so the count is asserted below.
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ hasPermission: () => true, accessToken: 'test-token' }),
}));

import BankFeedsView from '../BankFeedsView.vue';

const UNHEALTHY_SIMPLEFIN = {
  id: 'inst-sfin-1',
  label: 'Primary Bank',
  provider: 'simplefin',
  fi_host: '',
  enabled: true,
  configured: true,
  connected: true,
  auth_state: 'needs_reconnect',
  account_count: 2,
  last_synced_at: null,
  documents_available: null,
  breaker_state: 'closed',
};

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiGet.mockImplementation((url) => {
    if (url === '/api/bank-feeds/status') {
      return Promise.resolve({
        institutions: [UNHEALTHY_SIMPLEFIN],
        schedule: { frequency: 'manual', backfill_days: 365 },
      });
    }
    if (url === '/api/bank-feeds/accounts') return Promise.resolve({ accounts: [] });
    return Promise.resolve({});
  });
});

// `$router.push` returns a promise the template does not await, so the click
// handler resolves before the navigation does. Polling the router — rather
// than counting `flushPromises()` calls until it happens to pass — is what
// keeps this from becoming a timing flake that reddens on a slow machine.
async function settle(router) {
  await vi.waitFor(() => {
    if (router.currentRoute.value.path === '/bank-feeds') {
      throw new Error('navigation has not landed yet');
    }
  }, { timeout: 2000 });
  await flushPromises();
}

async function mountBankFeeds() {
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push('/bank-feeds');
  await router.isReady();
  const wrapper = mount(BankFeedsView, {
    global: { plugins: [router, createPinia()] },
  });
  await flushPromises();
  return { wrapper, router };
}

describe('the SimpleFIN re-link button reaches a page that can re-link', () => {
  it('navigates to /settings?tab=integrations, not NotFoundView', async () => {
    const { wrapper, router } = await mountBankFeeds();

    const button = wrapper.find('[data-testid="bank-simplefin-relink-inst-sfin-1"]');
    expect(button.exists()).toBe(true);

    await button.trigger('click');
    await settle(router);

    // The whole defect in one assertion: '/settings/integrations' resolves to
    // not-found, and a bare '/settings' loses the tab.
    expect(router.currentRoute.value.fullPath).toBe('/settings?tab=integrations');
    expect(router.currentRoute.value.name).toBe('settings');
    expect(router.currentRoute.value.name).not.toBe('not-found');

    wrapper.unmount();
  });

  it('sends a tab SettingsView will actually honour', async () => {
    const { wrapper, router } = await mountBankFeeds();
    await wrapper.find('[data-testid="bank-simplefin-relink-inst-sfin-1"]').trigger('click');
    await settle(router);

    const sent = router.currentRoute.value.query.tab;
    expect(sent).toBeTruthy();

    // SettingsView ignores a `?tab=` outside its allowlist and falls back to
    // Branding — a link that resolves and still lands on the wrong screen.
    // That allowlist is pinned to the rendered <Tab>s, in both directions, by
    // SettingsTabDeepLink.spec.js; this only has to prove the button aims
    // inside it.
    const settingsSource = readFileSync(
      join(dirname(fileURLToPath(import.meta.url)), '..', 'SettingsView.vue'),
      'utf8',
    );
    const listed = settingsSource.match(/const SETTINGS_TABS = \[([\s\S]*?)\]/);
    expect(listed).not.toBeNull();
    const allowed = [...listed[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    expect(allowed).toContain(sent);

    // And specifically the tab SimpleFINCard lives on — 'branding' would
    // satisfy the membership check above while still being a dead end.
    expect(sent).toBe('integrations');

    wrapper.unmount();
  });

  it('shows the re-link button for every unhealthy state, and none when healthy', async () => {
    // Pins what the assertions above discriminate against. If the button
    // rendered unconditionally they would still pass while the control was
    // wrong, and if it rendered for nothing they would pass vacuously.
    const states = [
      { auth_state: 'needs_reconnect', connected: true, expected: true },
      { auth_state: 'refresh_failed', connected: true, expected: true },
      { auth_state: null, connected: false, expected: true },
      { auth_state: 'healthy', connected: true, expected: false },
    ];
    for (const { auth_state, connected, expected } of states) {
      apiGet.mockImplementation((url) => {
        if (url === '/api/bank-feeds/status') {
          return Promise.resolve({
            institutions: [{ ...UNHEALTHY_SIMPLEFIN, auth_state, connected }],
            schedule: { frequency: 'manual', backfill_days: 365 },
          });
        }
        if (url === '/api/bank-feeds/accounts') return Promise.resolve({ accounts: [] });
        return Promise.resolve({});
      });
      const { wrapper } = await mountBankFeeds();
      const present = wrapper.find('[data-testid="bank-simplefin-relink-inst-sfin-1"]').exists();
      expect(present, `auth_state=${auth_state} connected=${connected}`).toBe(expected);
      wrapper.unmount();
    }
  });
});

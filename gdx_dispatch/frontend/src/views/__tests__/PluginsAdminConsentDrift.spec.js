/**
 * PluginsAdminView — the consent-drift banner (GDXA-462).
 *
 * A plugin upgrade that changes what it runs automatically pauses its events
 * and schedules until the owner re-consents. Before this the only trace was a
 * log line and a database row no screen read, so automation stopped silently.
 *
 * COUNTERFACTUAL: drop `await loadDrift()` from grantConsent and "re-consent
 * clears the banner" goes red; drop the v-if and "no drift, no banner" goes red.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PluginsAdminView from '../PluginsAdminView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../stores/auth', () => ({ useAuthStore: () => ({ role: 'owner' }) }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn().mockResolvedValue(true) }),
}));

const get = vi.fn();
const post = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get, post, del: vi.fn() }),
}));

let drift;
let reachable = true;
const EVENTS_ONLY = [{ name: 'events', risk: 'runs on events', consented: true }];
let perms = EVENTS_ONLY;
function routeGet(url) {
  if (url === '/api/admin/plugins/consent-drift') {
    return Promise.resolve(reachable ? { catalog_reachable: true, drifted: drift }
      : { catalog_reachable: false, drifted: [] });
  }
  if (url.endsWith('/permissions')) {
    // As the server computes it: an empty list is never "all consented".
    return Promise.resolve({ permissions: perms, all_consented: perms.length > 0 && perms.every((p) => p.consented) });
  }
  if (url === '/api/admin/plugins/storefront') return Promise.resolve({ plugins: [] });
  return Promise.resolve([]);
}

const Pass = { template: '<div><slot /><slot name="footer" /></div>' };
const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Message: Pass,
  Dialog: { props: ['visible'], template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>' },
  DataTable: Pass,
  Column: true,
  InputText: true,
  Tag: true,
  // Honours `disabled` like the real one: a stub that drops it lets every
  // click test pass against a greyed-out button.
  Button: {
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button :data-label="label" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
};

async function mountView() {
  const w = mount(PluginsAdminView, {
    global: { stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return w;
}

describe('PluginsAdminView consent drift banner', () => {
  beforeEach(() => {
    get.mockReset().mockImplementation(routeGet);
    perms = EVENTS_ONLY;
    reachable = true;
    post.mockReset().mockResolvedValue({});
  });

  it('no drift, no banner', async () => {
    drift = [];
    const w = await mountView();
    expect(w.find('[data-testid="consent-drift"]').exists()).toBe(false);
  });

  it('names each paused plugin and what is paused', async () => {
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events', 'schedules'] }];
    const w = await mountView();
    const banner = w.find('[data-testid="consent-drift"]');
    expect(banner.exists()).toBe(true);
    expect(banner.find('[data-plugin="hooks"]').text()).toContain('Hooks');
    expect(banner.text()).toContain('events and schedules paused');
  });

  it('re-consent from the banner posts consent and clears the banner', async () => {
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events'], fingerprint: 'fp1' }];
    const w = await mountView();
    await w.find('[data-testid="consent-drift"] button').trigger('click');
    await flushPromises();
    expect(w.find('.dlg').exists()).toBe(true);

    drift = []; // the server's live drift after the grant
    await w.find('.dlg button[data-label="Re-consent"]').trigger('click');
    await flushPromises();

    // Pinned to what the dialog showed, so a later change is refused (409).
    expect(post).toHaveBeenCalledWith('/api/admin/plugins/hooks/consent', { fingerprint: 'fp1' }, expect.anything());
    expect(w.find('[data-testid="consent-drift"]').exists()).toBe(false);
    expect(w.find('[data-testid="drift-changes"]').exists()).toBe(false);
  });

  it('a refused grant (409) keeps the page and dialog up and shows the new list', async () => {
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events'], fingerprint: 'fp1', events_added: ['c.d'] }];
    const w = await mountView();
    await w.find('[data-testid="consent-drift"] button').trigger('click');
    await flushPromises();

    post.mockRejectedValueOnce(Object.assign(new Error('changed again'), { status: 409 }));
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events'], fingerprint: 'fp2', events_added: ['c.d', 'e.f'] }];
    // The upgrade that moved the pin also added a permission: the dialog must
    // list it before the next click can grant it.
    perms = [...EVENTS_ONLY, { name: 'browser', risk: 'drives a browser', consented: false }];
    await w.find('.dlg button[data-label="Re-consent"]').trigger('click');
    await flushPromises();

    expect(w.find('.dlg [data-testid="drift-changes"]').text()).toContain('Now runs on: c.d, e.f');
    expect(w.find('.dlg').text()).toContain('browser');
    expect(w.find('[data-testid="consent-drift"]').exists()).toBe(true);

    await w.find('.dlg button[data-label="Grant consent"]').trigger('click');
    await flushPromises();
    expect(post).toHaveBeenLastCalledWith('/api/admin/plugins/hooks/consent', { fingerprint: 'fp2' }, expect.anything());
  });

  it('opening the dialog reads the drift live, not the page-load snapshot', async () => {
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events'], fingerprint: 'fp1' }];
    const w = await mountView();
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events'], fingerprint: 'fp2', events_added: ['e.f'] }];
    await w.find('[data-testid="consent-drift"] button').trigger('click');
    await flushPromises();
    expect(w.find('.dlg [data-testid="drift-changes"]').text()).toContain('Now runs on: e.f');
    await w.find('.dlg button[data-label="Re-consent"]').trigger('click');
    await flushPromises();
    expect(post).toHaveBeenCalledWith('/api/admin/plugins/hooks/consent', { fingerprint: 'fp2' }, expect.anything());
  });

  it('an unpinned grant refused because the plugin drifted meanwhile takes up the pin', async () => {
    drift = [{ key: 'other', name: 'Other', paused: ['events'], fingerprint: 'fpo' }];
    const w = await mountView();
    // Open a non-drifted plugin's dialog through the same handler the table uses.
    drift = [];
    w.vm.openConsent({ key: 'hooks', name: 'Hooks' });
    await flushPromises();
    expect(w.find('[data-testid="drift-changes"]').exists()).toBe(false);

    post.mockRejectedValueOnce(Object.assign(new Error('drifted'), { status: 409 }));
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events'], fingerprint: 'fp2', events_added: ['e.f'] }];
    await w.find('.dlg button[data-label="Re-consent"]').trigger('click');
    await flushPromises();
    expect(post).toHaveBeenLastCalledWith('/api/admin/plugins/hooks/consent', {}, expect.anything());
    expect(w.find('.dlg [data-testid="drift-changes"]').text()).toContain('Now runs on: e.f');

    await w.find('.dlg button[data-label="Re-consent"]').trigger('click');
    await flushPromises();
    expect(post).toHaveBeenLastCalledWith('/api/admin/plugins/hooks/consent', { fingerprint: 'fp2' }, expect.anything());
  });

  it('a refusal after which the drift is gone closes the dialog, never posting unpinned', async () => {
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events'], fingerprint: 'fp1' }];
    const w = await mountView();
    await w.find('[data-testid="consent-drift"] button').trigger('click');
    await flushPromises();
    post.mockRejectedValueOnce(Object.assign(new Error('changed again'), { status: 409 }));
    drift = [];
    await w.find('.dlg button[data-label="Re-consent"]').trigger('click');
    await flushPromises();
    expect(w.find('.dlg').exists()).toBe(false);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it('a drifted plugin that now declares no permission can still be re-consented', async () => {
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events'], fingerprint: 'fp1', events_removed: ['job.created'] }];
    perms = [];
    const w = await mountView();
    await w.find('[data-testid="consent-drift"] button').trigger('click');
    await flushPromises();
    expect(w.find('.dlg [data-testid="consent-retired"]').exists()).toBe(true);
    const grant = w.find('.dlg button[data-label="Re-consent"]');
    expect(grant.attributes('disabled')).toBeUndefined();

    drift = [];
    await grant.trigger('click');
    await flushPromises();
    expect(post).toHaveBeenCalledWith('/api/admin/plugins/hooks/consent', { fingerprint: 'fp1' }, expect.anything());
    expect(w.find('[data-testid="consent-drift"]').exists()).toBe(false);
    // Nothing left to show: the dialog closes rather than claim the plugin
    // "requests elevated capabilities" over an empty list.
    expect(w.find('.dlg').exists()).toBe(false);
  });

  it('plugin-host unreachable after a refusal is not read as "drift cleared"', async () => {
    drift = [{ key: 'hooks', name: 'Hooks', paused: ['events'], fingerprint: 'fp1' }];
    const w = await mountView();
    await w.find('[data-testid="consent-drift"] button').trigger('click');
    await flushPromises();

    post.mockRejectedValueOnce(Object.assign(new Error('changed again'), { status: 409 }));
    reachable = false; // the drift read now answers { catalog_reachable: false, drifted: [] }
    await w.find('.dlg button[data-label="Re-consent"]').trigger('click');
    await flushPromises();

    expect(w.find('.dlg').exists()).toBe(true);
    expect(w.find('[data-testid="consent-drift"]').exists()).toBe(true);
  });

  it('a plugin that is not drifted and declares nothing keeps the grant disabled', async () => {
    drift = [];
    perms = [];
    const w = await mountView();
    w.vm.openConsent({ key: 'quiet', name: 'Quiet' });
    await flushPromises();
    expect(w.find('.dlg button[data-label="Grant consent"]').attributes('disabled')).toBeDefined();
  });

  it('the dialog opened from the banner shows what re-consent approves', async () => {
    drift = [{
      key: 'hooks', name: 'Hooks', paused: ['events', 'schedules'],
      events_added: ['invoice.voided'], events_removed: ['job.created'], schedules: ['poll', 'sweep'],
    }];
    const w = await mountView();
    await w.find('[data-testid="consent-drift"] button').trigger('click');
    await flushPromises();
    const changes = w.find('.dlg [data-testid="drift-changes"]');
    expect(changes.exists()).toBe(true);
    expect(changes.text()).toContain('Now runs on: invoice.voided');
    expect(changes.text()).toContain('No longer runs on: job.created');
    expect(changes.text()).toContain('All scheduled jobs it now declares: poll, sweep');
  });
});

/**
 * PluginsAdminView — a catalog card for a plugin this server cannot run
 * (GDXA-1742).
 *
 * plugin-host skips a plugin whose `requires` the host does not meet, with only
 * a log line. Install used to record it anyway and say "restart to load it", so
 * the owner restarted and got nothing. The card now shows the constraint and,
 * when the server says it is not met, disables Install and says why.
 *
 * COUNTERFACTUAL: drop `:disabled="p.compatible === false"` from Install and
 * "Install is disabled" goes red; drop the reason Message and "says why" goes
 * red; drop the pending_restart branch of incompatibleNote and "a recorded
 * install that will never load says so" goes red; drop its state checks and
 * "a running plugin with no update pending" goes red.
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

const REASON = 'It requires gdx>=9.0, and this server runs GDX 1.2.0. Update GDX to install it.';
const card = (over) => ({
  key: 'n8n', name: 'n8n Automations', distribution: 'gdx-plugin-n8n', version: '0.2.0',
  description: '', permissions: [], requires: '', state: 'available',
  installed_version: null, running_version: null, update_available: false,
  compatible: true, incompatible_reason: null, ...over,
});
let plugins;

const Pass = { template: '<div><slot /><slot name="footer" /></div>' };
const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Message: Pass,
  Dialog: { props: ['visible'], template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>' },
  DataTable: Pass,
  Column: true,
  InputText: true,
  Tag: true,
  // Honours `disabled` like the real one.
  Button: {
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button :data-label="label" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
};

async function mountView() {
  const w = mount(PluginsAdminView, { global: { stubs, directives: { tooltip: {} } } });
  await flushPromises();
  return w;
}

describe('PluginsAdminView store card host-version gate', () => {
  beforeEach(() => {
    get.mockReset().mockImplementation((url) => {
      if (url === '/api/admin/plugins/storefront') return Promise.resolve({ plugins, error: null });
      if (url === '/api/admin/plugins/consent-drift') return Promise.resolve({ catalog_reachable: true, drifted: [] });
      return Promise.resolve([]);
    });
    post.mockReset().mockResolvedValue({});
  });

  it('shows the constraint on the card', async () => {
    plugins = [card({ requires: 'gdx>=1.0' })];
    const w = await mountView();
    expect(w.find('[data-plugin="n8n"] [data-testid="store-requires"]').text()).toBe('Requires gdx>=1.0');
  });

  it('a plugin with no constraint shows none', async () => {
    plugins = [card()];
    const w = await mountView();
    expect(w.find('[data-testid="store-requires"]').exists()).toBe(false);
  });

  it('a compatible plugin can be installed', async () => {
    plugins = [card({ requires: 'gdx>=1.0' })];
    const w = await mountView();
    const btn = w.find('[data-plugin="n8n"] button[data-label="Install"]');
    expect(btn.attributes('disabled')).toBeUndefined();
    expect(w.find('[data-testid="store-incompatible"]').exists()).toBe(false);
  });

  it('Install is disabled and the card says why', async () => {
    plugins = [card({ requires: 'gdx>=9.0', compatible: false, incompatible_reason: REASON })];
    const w = await mountView();
    const btn = w.find('[data-plugin="n8n"] button[data-label="Install"]');
    expect(btn.attributes('disabled')).toBeDefined();
    await btn.trigger('click');
    expect(w.find('.dlg').exists()).toBe(false);
    expect(w.find('[data-plugin="n8n"] [data-testid="store-incompatible"]').text())
      .toBe(`v0.2.0 can't be installed here. ${REASON}`);
  });

  it('an update the server cannot run is disabled too', async () => {
    plugins = [card({
      requires: 'gdx>=9.0', compatible: false, incompatible_reason: REASON,
      state: 'running', installed_version: '0.1.0', running_version: '0.1.0', update_available: true,
    })];
    const w = await mountView();
    expect(w.find('[data-plugin="n8n"] button[data-label="Update to 0.2.0"]').attributes('disabled')).toBeDefined();
    expect(w.find('[data-plugin="n8n"] [data-testid="store-incompatible"]').text()).toContain('v0.2.0');
  });

  it('a recorded install that will never load says so', async () => {
    // Installed before this gate, or APP_VERSION changed since: "restart to
    // load" alone would send the owner round a restart that changes nothing.
    plugins = [card({
      requires: 'gdx>=9.0', compatible: false, incompatible_reason: REASON,
      state: 'pending_restart', installed_version: '0.2.0',
    })];
    const w = await mountView();
    expect(w.find('[data-plugin="n8n"] [data-testid="store-incompatible"]').text())
      .toBe(`v0.2.0 is installed but plugin-host won't load it. ${REASON}`);
  });

  it('a compatible update shows no warning', async () => {
    plugins = [card({
      requires: 'gdx>=1.0', state: 'running', installed_version: '0.1.0',
      running_version: '0.1.0', update_available: true,
    })];
    const w = await mountView();
    expect(w.find('[data-testid="store-incompatible"]').exists()).toBe(false);
  });

  it('a running plugin with no update pending shows no warning', async () => {
    plugins = [card({
      version: '0.1.0', requires: 'gdx>=9.0', compatible: false, incompatible_reason: REASON,
      state: 'running', installed_version: '0.1.0', running_version: '0.1.0',
    })];
    const w = await mountView();
    expect(w.find('[data-testid="store-incompatible"]').exists()).toBe(false);
    expect(w.find('[data-plugin="n8n"] [data-testid="store-requires"]').exists()).toBe(true);
  });
});

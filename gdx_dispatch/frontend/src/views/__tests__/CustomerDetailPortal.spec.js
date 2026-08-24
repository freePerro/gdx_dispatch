/**
 * CustomerDetailView — the Portal tab.
 *
 * Why this spec exists: the tab was a working-looking fake. All three of its
 * calls went to ui_compat shims — a GET that returned a hardcoded
 * `{"exists": false, "account": null}` for EVERY customer, a POST that 501'd,
 * and a DELETE with no handler at all (405). On prod that meant the office was
 * told "No portal account registered for this customer" about the one customer
 * who actually had one. Portal provisioning had been real the whole time on
 * `portal.py`'s staff_router, which PortalView already used.
 *
 * The dialog also modelled the wrong flow: it asked staff to type the
 * customer's password. Provisioning never sets one — the customer arrives via
 * a magic link and sets their own.
 *
 * Pins:
 *  - status reads the REAL endpoint, and an existing account renders as active
 *    (the exact thing the shim got wrong)
 *  - no call is ever made to the retired `/portal-account` paths
 *  - the invite posts to the real endpoint and reports whether email SENT,
 *    rather than showing a success toast either way
 *  - a failed send still surfaces the magic link, so the office can pass it on
 *  - "turn off" PATCHes portal_enabled:false — it does not DELETE
 *  - no password field exists anywhere in the tab
 *
 * jsdom applies no media queries — this proves wiring, never layout.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDelete = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, delete: apiDelete, del: apiDelete }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, delete: apiDelete, del: apiDelete }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ isEnabled: () => false, modules: { value: [] } }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission: vi.fn(() => true),
    permissions: { value: ['customers.write'] },
    permissionsLoaded: { value: true },
    reloadPermissions: vi.fn(),
  }),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'cust-1' } }),
  useRouter: () => ({ push: vi.fn(), back: vi.fn() }),
}));

import CustomerDetailView from '../CustomerDetailView.vue';

const CUSTOMER = { id: 'cust-1', name: 'Riverbend Lumber', email: 'account@example.com' };

const stubs = {
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Tag: { props: ['value', 'severity'], template: '<span class="tag">{{ value }}</span>' },
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text', 'rounded', 'outlined', 'size', 'type'],
    emits: ['click'],
    template: '<button :data-label="label" :disabled="disabled" @click="$emit(\'click\')"><slot />{{ label }}</button>',
  },
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  InputText: {
    props: ['modelValue', 'readonly'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" :readonly="readonly" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  PhoneInput: { props: ['modelValue'], template: '<input class="phone" />' },
  Textarea: { props: ['modelValue'], template: '<textarea :value="modelValue"></textarea>' },
  DataTable: { template: '<div><slot /></div>' },
  Column: { template: '<div><slot /></div>' },
  Select: { props: ['modelValue'], template: '<select />' },
  DatePicker: { props: ['modelValue'], template: '<input />' },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  RadioButton: { props: ['modelValue'], template: '<input type="radio" />' },
  ToggleSwitch: { props: ['modelValue'], emits: ['change'], template: '<button class="tgl" />' },
  ProgressSpinner: { template: '<div />' },
  Toast: { template: '<div />' },
  JobStateChip: { template: '<span />' },
  EmailTimeline: { template: '<div />' },
};

const ENABLED_STATUS = {
  id: 'cust-1',
  customer_name: 'Riverbend Lumber',
  email: 'billing@example.com',
  portal_enabled: true,
  last_login: null,
  signin_link_expires_at: null,
};

function routeGet(url) {
  if (url === '/api/portal/cust-1') return Promise.resolve({ ...ENABLED_STATUS });
  if (url === '/api/customers/cust-1') return Promise.resolve(CUSTOMER);
  return Promise.resolve([]);
}

async function mountPortalTab() {
  const wrapper = mount(CustomerDetailView, {
    global: { stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  wrapper.vm.activeTab = 'Portal';
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  apiGet.mockImplementation(routeGet);
  apiPost.mockResolvedValue({
    ok: true,
    invite_sent: true,
    email: 'billing@example.com',
    magic_link: 'https://example.test/customer-portal?token=abc',
    email_skip_reason: null,
  });
  apiPatch.mockResolvedValue({ ok: true, portal_enabled: false });
  apiDelete.mockResolvedValue({ ok: true });
});

describe('CustomerDetailView — Portal tab', () => {
  it('reads status from the real staff endpoint, never the retired shim', async () => {
    await mountPortalTab();
    const urls = apiGet.mock.calls.map((c) => c[0]);
    expect(urls).toContain('/api/portal/cust-1');
    expect(urls.some((u) => String(u).includes('portal-account'))).toBe(false);
  });

  it('renders an existing account as active — the exact thing the shim got wrong', async () => {
    const wrapper = await mountPortalTab();
    const line = wrapper.find('[data-testid="portal-status-line"]');
    expect(line.exists()).toBe(true);
    expect(line.text()).toContain('Active');
    expect(line.text()).toContain('billing@example.com');
    // The shim's copy. If this ever comes back for an enabled customer, the
    // tab has regressed to lying.
    expect(line.text()).not.toContain('No portal account registered');
  });

  it('says so when an active customer has never signed in', async () => {
    // Prod's one real account: enabled, invited, zero logins. Silence there
    // reads as "working".
    const wrapper = await mountPortalTab();
    expect(wrapper.find('[data-testid="portal-never-signed-in"]').exists()).toBe(true);
  });

  it('shows a disabled customer as off, not as an error', async () => {
    apiGet.mockImplementation((url) =>
      url === '/api/portal/cust-1'
        ? Promise.resolve({ ...ENABLED_STATUS, portal_enabled: false })
        : routeGet(url),
    );
    const wrapper = await mountPortalTab();
    expect(wrapper.find('[data-testid="portal-status-line"]').text()).toContain('Portal access is off');
  });

  it('has no password field — staff never set a customer password', async () => {
    const wrapper = await mountPortalTab();
    await wrapper.find('[data-testid="portal-invite-btn"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-testid="portal-dialog-password"]').exists()).toBe(false);
    expect(wrapper.html()).not.toContain('Password must be at least');
  });

  it('sends the invite through the real endpoint with the customer id', async () => {
    const wrapper = await mountPortalTab();
    await wrapper.find('[data-testid="portal-invite-btn"]').trigger('click');
    await flushPromises();
    await wrapper.find('form.dialog-form').trigger('submit');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith('/api/portal/invite', {
      customer_id: 'cust-1',
      email: 'billing@example.com',
    });
    const posted = apiPost.mock.calls.map((c) => c[0]);
    expect(posted.some((u) => String(u).includes('portal-account'))).toBe(false);
  });

  it('reports that the email actually sent, rather than a blanket success', async () => {
    const wrapper = await mountPortalTab();
    await wrapper.find('[data-testid="portal-invite-btn"]').trigger('click');
    await flushPromises();
    await wrapper.find('form.dialog-form').trigger('submit');
    await flushPromises();

    expect(wrapper.find('[data-testid="portal-invite-sent"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="portal-invite-not-sent"]').exists()).toBe(false);
  });

  it('surfaces the link and the reason when the email does NOT send', async () => {
    // The failure that matters: a green toast here would tell the office a
    // customer was invited when nothing left the building.
    apiPost.mockResolvedValue({
      ok: true,
      invite_sent: false,
      email: 'billing@example.com',
      magic_link: 'https://example.test/customer-portal?token=xyz',
      email_skip_reason: 'no tenant email configured',
    });
    const wrapper = await mountPortalTab();
    await wrapper.find('[data-testid="portal-invite-btn"]').trigger('click');
    await flushPromises();
    await wrapper.find('form.dialog-form').trigger('submit');
    await flushPromises();

    const notSent = wrapper.find('[data-testid="portal-invite-not-sent"]');
    expect(notSent.exists()).toBe(true);
    expect(notSent.text()).toContain('no tenant email configured');
    expect(wrapper.find('[data-testid="portal-invite-link"]').attributes('value'))
      .toBe('https://example.test/customer-portal?token=xyz');
  });

  it('turns access off with a PATCH, and never issues a DELETE', async () => {
    // The old Remove button fired DELETE at a path with no handler — a 405.
    const wrapper = await mountPortalTab();
    await wrapper.find('[data-testid="portal-remove-btn"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledWith('/api/portal/cust-1', { portal_enabled: false });
    expect(apiDelete).not.toHaveBeenCalled();
  });

  it('flags an expired invite link instead of leaving it to be assumed live', async () => {
    apiGet.mockImplementation((url) =>
      url === '/api/portal/cust-1'
        ? Promise.resolve({ ...ENABLED_STATUS, signin_link_expires_at: '2020-01-01T00:00:00+00:00' })
        : routeGet(url),
    );
    const wrapper = await mountPortalTab();
    const note = wrapper.find('[data-testid="portal-invite-note"]');
    expect(note.exists()).toBe(true);
    expect(note.text()).toContain('expired');
  });

  it('describes a still-valid invite as valid', async () => {
    const future = new Date(Date.now() + 3 * 86400000).toISOString();
    apiGet.mockImplementation((url) =>
      url === '/api/portal/cust-1'
        ? Promise.resolve({ ...ENABLED_STATUS, signin_link_expires_at: future })
        : routeGet(url),
    );
    const wrapper = await mountPortalTab();
    const note = wrapper.find('[data-testid="portal-invite-note"]');
    expect(note.text()).toContain('valid until');
    expect(note.text()).not.toContain('expired');
  });
});

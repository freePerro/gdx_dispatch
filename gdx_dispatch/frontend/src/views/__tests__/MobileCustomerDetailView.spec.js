/**
 * MobileCustomerDetailView — pins the contract:
 *  - Renders 9 tabs (Jobs/Estimates/Invoices/Locations/Notes/Equipment/Recurring/Communications/Portal).
 *  - Loads the customer on mount and lazy-fetches each tab on selection.
 *  - Quick-action strip (Call/Text/Email/Map) honors the customer's contact info.
 *  - Back button navigates to /mobile/customers when no history.
 *
 * Replaces the per-S116 stub by mounting the view bare (the AppLayout-into-App.vue
 * refactor moves the shell to App.vue; views render only their <section> body).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

// 2026-07-22 "New job" quick action: the view now imports usePermission
// (which reads the Pinia auth store) — mock it like the dialog spec does.
const hasPermission = vi.fn(() => true);
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission,
    permissions: { value: ['jobs.write'] },
    permissionsLoaded: { value: true },
    reloadPermissions: vi.fn(),
  }),
}));

const routerPush = vi.fn();
const routerBack = vi.fn();
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'cust-123' } }),
  useRouter: () => ({ push: routerPush, back: routerBack }),
}));

import MobileCustomerDetailView from '../MobileCustomerDetailView.vue';

const stubs = {
  Tag: { props: ['value', 'severity'], template: '<span class="tag">{{ value }}</span>' },
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text', 'rounded'],
    emits: ['click'],
    template: '<button @click="$emit(\'click\')"><slot />{{ label }}</button>',
  },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  InputText: { props: ['modelValue'], template: '<input :value="modelValue" />' },
  Textarea: { props: ['modelValue'], template: '<textarea :value="modelValue"></textarea>' },
  // The embedded new-job dialog has its own spec; here it just needs to
  // mount quietly and surface its open state.
  MobileJobNewDialog: {
    props: ['visible', 'customer'],
    emits: ['update:visible', 'created'],
    template: '<div v-if="visible" data-test="stub-new-job-dialog">{{ customer && customer.name }}</div>',
  },
};

const customerFixture = {
  id: 'cust-123',
  name: 'Jeff Johnson',
  customer_type: 'Residential',
  phone: '555-867-5309',
  email: 'jeff@example.com',
  address: '100 Main St',
};

describe('MobileCustomerDetailView', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    apiPatch.mockReset();
    routerPush.mockReset();
    routerBack.mockReset();
  });

  it('renders the customer header and quick-action strip', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/customers/cust-123') return Promise.resolve(customerFixture);
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileCustomerDetailView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.text()).toContain('Jeff Johnson');
    expect(wrapper.find('[data-test="mcd-quick-actions"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="mcd-call"]').attributes('href')).toBe('tel:555-867-5309');
    expect(wrapper.find('[data-test="mcd-text"]').attributes('href')).toBe('sms:555-867-5309');
    expect(wrapper.find('[data-test="mcd-email"]').attributes('href')).toBe('mailto:jeff@example.com');
    expect(wrapper.find('[data-test="mcd-navigate"]').attributes('href')).toContain('100%20Main%20St');
  });

  it('renders all 9 tabs', async () => {
    apiGet.mockResolvedValue(customerFixture);
    const wrapper = mount(MobileCustomerDetailView, { global: { stubs } });
    await flushPromises();
    const tabs = wrapper.findAll('[data-test="mcd-tab-strip"] .tab-btn');
    expect(tabs.map((t) => t.text())).toEqual([
      'Jobs',
      'Estimates',
      'Invoices',
      'Locations',
      'Notes',
      'Equipment',
      'Recurring',
      'Communications',
      'Portal',
    ]);
  });

  it('lazy-fetches a tab the first time it is selected', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/customers/cust-123') return Promise.resolve(customerFixture);
      if (url.startsWith('/api/jobs')) return Promise.resolve([]);
      if (url.startsWith('/api/estimates')) return Promise.resolve([{ id: 'est-1', total: 500, status: 'Sent' }]);
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileCustomerDetailView, { global: { stubs } });
    await flushPromises();

    // Default tab Jobs already fetched on mount.
    expect(apiGet).toHaveBeenCalledWith(expect.stringContaining('/api/jobs?customer_id=cust-123'));

    apiGet.mockClear();
    // Click Estimates tab.
    await wrapper.find('[data-test="mcd-tab-estimates"]').trigger('click');
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/estimates?customer_id=cust-123');

    // Re-select Jobs — should NOT refetch (already loaded).
    apiGet.mockClear();
    await wrapper.find('[data-test="mcd-tab-jobs"]').trigger('click');
    await flushPromises();
    expect(apiGet).not.toHaveBeenCalled();
  });

  it('disables quick-action buttons when contact info is missing', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/customers/cust-123') return Promise.resolve({ id: 'cust-123', name: 'Sparse' });
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileCustomerDetailView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-test="mcd-call"]').classes()).toContain('disabled');
    expect(wrapper.find('[data-test="mcd-email"]').classes()).toContain('disabled');
    expect(wrapper.find('[data-test="mcd-navigate"]').classes()).toContain('disabled');
  });

  it('New job quick action opens the dialog preseeded with this customer', async () => {
    // 2026-07-22: closes the create-customer → create-job loop; the tech
    // never re-searches the person they're already looking at.
    apiGet.mockImplementation((url) => {
      if (url === '/api/customers/cust-123') return Promise.resolve(customerFixture);
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileCustomerDetailView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-test="stub-new-job-dialog"]').exists()).toBe(false);
    await wrapper.find('[data-test="mcd-new-job"]').trigger('click');

    const dlg = wrapper.find('[data-test="stub-new-job-dialog"]');
    expect(dlg.exists()).toBe(true);
    expect(dlg.text()).toContain('Jeff Johnson');
  });

  it('New job quick action hidden without jobs.write', async () => {
    hasPermission.mockReturnValue(false);
    try {
      apiGet.mockImplementation((url) => {
        if (url === '/api/customers/cust-123') return Promise.resolve(customerFixture);
        return Promise.resolve([]);
      });
      const wrapper = mount(MobileCustomerDetailView, { global: { stubs } });
      await flushPromises();
      expect(wrapper.find('[data-test="mcd-new-job"]').exists()).toBe(false);
    } finally {
      hasPermission.mockReturnValue(true);
    }
  });

  it('shows error state and retry when customer fetch fails', async () => {
    apiGet.mockRejectedValueOnce(new Error('boom'));
    const wrapper = mount(MobileCustomerDetailView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-test="mcd-error"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="mcd-error"]').text()).toContain('boom');
  });

  // ── Portal tab ────────────────────────────────────────────────────────────
  // This tab called `/api/customers/{id}/portal-account`, a ui_compat shim that
  // returned a hardcoded {"exists": false, "account": null}. Two consequences:
  // it said "No portal account." for every customer, and `last_login` could
  // never render because the shim never sent that key. When the shim was
  // deleted with the desktop tab's repoint, THIS caller was missed and the tab
  // broke outright — the sweep was scoped to the desktop file. These pin the
  // repoint so it cannot silently regress again.

  async function openPortalTab() {
    apiGet.mockImplementation((url) => {
      if (url === '/api/customers/cust-123') return Promise.resolve(customerFixture);
      if (url === '/api/portal/cust-123') {
        return Promise.resolve({
          id: 'cust-123',
          customer_name: 'Jeff Johnson',
          email: 'jeff@example.com',
          portal_enabled: true,
          last_login: null,
          signin_link_expires_at: null,
        });
      }
      return Promise.resolve([]);
    });
    const wrapper = mount(MobileCustomerDetailView, { global: { stubs } });
    await flushPromises();
    // Click the tab rather than setting activeTab: the fetch hangs off
    // selectTab(), and a feature the office cannot reach is not shipped.
    const tab = wrapper.findAll('button').find((b) => b.text().trim() === 'Portal');
    expect(tab, 'the Portal tab control must exist').toBeTruthy();
    await tab.trigger('click');
    await flushPromises();
    return wrapper;
  }

  it('Portal tab reads the real staff endpoint, not the deleted shim', async () => {
    await openPortalTab();
    const urls = apiGet.mock.calls.map((c) => c[0]);
    expect(urls).toContain('/api/portal/cust-123');
    expect(urls.some((u) => String(u).includes('portal-account'))).toBe(false);
  });

  it('Portal tab shows an enabled account as on, not as unavailable', async () => {
    const wrapper = await openPortalTab();
    // NOTE: the tab BUTTON and the tab PANEL both carry
    // data-test="mcd-tab-portal" (the strip templates its testid from the tab
    // name). A bare find() returns the button. Pre-existing collision; select
    // the panel explicitly rather than assert against a button's label.
    const panel = wrapper.findAll('[data-test="mcd-tab-portal"]').find((el) => el.element.tagName !== 'BUTTON');
    expect(panel, 'the Portal panel must render').toBeTruthy();
    const tab = panel;
    expect(tab.text()).toContain('Portal access on');
    // What a broken fetch renders. If this reappears the endpoint moved again.
    expect(tab.text()).not.toContain('Portal status unavailable');
    expect(tab.text()).not.toContain('No portal account');
  });

  it('Portal tab says when an active customer has never signed in', async () => {
    const wrapper = await openPortalTab();
    expect(wrapper.find('[data-test="mcd-portal-never"]').exists()).toBe(true);
  });
});

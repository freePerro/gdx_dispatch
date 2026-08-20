/**
 * CustomerDetailView — the Contacts tab.
 *
 * Why this spec exists: `customer_contacts` shipped with a model, a mobile
 * writer and an email recipient picker, and held ZERO rows in production —
 * because the office had no way in. A second person at a business account
 * ended up as a QuickBooks sub-customer instead. See
 * docs/design/qb-subcustomer-flattening-plan.md.
 *
 * Pins:
 *  - the tab exists and is reachable (a feature nobody can find is not shipped)
 *  - it loads contacts on mount and renders them, with the default-recipient tag
 *  - add / edit / remove call the real endpoints
 *  - remove goes through a REAL dialog, not useDestructiveConfirm — that
 *    composable auto-accepts silently (issue #215), so a confirm built on it
 *    would delete on first click with no prompt
 *  - the server's 422 for stranding the default recipient surfaces inline
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
const hasPermission = vi.fn(() => true);
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission,
    permissions: { value: ['customers.contact_write'] },
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
const CONTACTS = [
  { id: 'c1', name: 'Site A', label: 'job contact', phone: '2185550100', email: 'jeff@example.com', is_primary: true },
  { id: 'c2', name: 'Sam', label: null, phone: null, email: null, is_primary: false },
  { id: 'c3', name: 'Dana', label: 'front desk', phone: null, email: 'dana@example.com', is_primary: false },
];

const stubs = {
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Tag: { props: ['value', 'severity'], template: '<span class="tag">{{ value }}</span>' },
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text', 'rounded', 'outlined', 'size', 'type'],
    emits: ['click'],
    template: '<button :data-label="label" @click="$emit(\'click\')"><slot />{{ label }}</button>',
  },
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  PhoneInput: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="phone" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Textarea: { props: ['modelValue'], template: '<textarea :value="modelValue"></textarea>' },
  DataTable: { template: '<div><slot /></div>' },
  Column: { template: '<div><slot /></div>' },
  Select: { props: ['modelValue'], template: '<select />' },
  DatePicker: { props: ['modelValue'], template: '<input />' },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  RadioButton: { props: ['modelValue'], template: '<input type="radio" />' },
  ToggleSwitch: { props: ['modelValue'], emits: ['change'], template: '<button class="tgl" @click="$emit(\'change\', true)" />' },
  ProgressSpinner: { template: '<div />' },
  Toast: { template: '<div />' },
  JobStateChip: { template: '<span />' },
  EmailTimeline: { template: '<div />' },
};

function routeGet(url) {
  if (url.includes('/contacts')) return Promise.resolve(CONTACTS);
  if (url === '/api/customers/cust-1') return Promise.resolve(CUSTOMER);
  return Promise.resolve([]);
}

async function mountView() {
  const wrapper = mount(CustomerDetailView, {
    global: { stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return wrapper;
}

async function openContactsTab(wrapper) {
  wrapper.vm.activeTab = 'Contacts';
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  hasPermission.mockReturnValue(true);
  apiGet.mockImplementation(routeGet);
  apiPost.mockResolvedValue({ ok: true });
  apiPatch.mockResolvedValue({ ok: true });
  apiDelete.mockResolvedValue({ ok: true, was_primary: false });
});

describe('CustomerDetailView — Contacts tab', () => {
  it('renders a Contacts tab button the office can actually click', async () => {
    // Asserting 'Contacts' is in the tabs array only proves a literal is in a
    // literal. This asserts a control exists in the DOM and switches the panel.
    const wrapper = await mountView();
    const tab = wrapper.findAll('button').find((b) => b.text().trim() === 'Contacts');
    expect(tab).toBeTruthy();
    await tab.trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-testid="tab-contacts-content"]').exists()).toBe(true);
  });

  it('loads the account contacts on mount', async () => {
    await mountView();
    expect(apiGet).toHaveBeenCalledWith('/api/customers/cust-1/contacts');
  });

  it('renders each contact and tags the default recipient', async () => {
    const wrapper = await openContactsTab(await mountView());
    const panel = wrapper.get('[data-testid="tab-contacts-content"]');
    expect(panel.text()).toContain('Site A');
    expect(panel.text()).toContain('Sam');
    expect(wrapper.find('[data-testid="contact-primary-tag-c1"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="contact-primary-tag-c2"]').exists()).toBe(false);
  });

  it('offers "Make default" only where it can work, and it calls make-primary', async () => {
    const wrapper = await openContactsTab(await mountView());
    // c1 is already primary; c2 has no email — neither may be promoted.
    expect(wrapper.find('[data-testid="make-primary-c1"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="make-primary-c2"]').exists()).toBe(false);
    // c3 has an email and is not primary — the control must be there AND live.
    // An earlier version of this test only asserted the two absences, so
    // deleting makeContactPrimary and its button would have kept it green.
    const promote = wrapper.get('[data-testid="make-primary-c3"]');
    await promote.trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith(
      '/api/customers/cust-1/contacts/c3/make-primary',
      {},
      expect.objectContaining({ successMessage: expect.stringContaining('automated emails') }),
    );
  });

  it('creates a contact through the office endpoint', async () => {
    const wrapper = await openContactsTab(await mountView());
    await wrapper.get('[data-testid="add-contact-btn"]').trigger('click');
    wrapper.vm.contactForm.name = 'Site A';
    wrapper.vm.contactForm.label = 'job contact';
    await wrapper.vm.saveContact();
    expect(apiPost).toHaveBeenCalledWith(
      '/api/customers/cust-1/contacts',
      expect.objectContaining({ name: 'Site A', label: 'job contact' }),
    );
  });

  it('refuses to save a contact with no name, without calling the API', async () => {
    const wrapper = await openContactsTab(await mountView());
    wrapper.vm.contactForm = { id: null, name: '   ', label: '', phone: '', email: '' };
    await wrapper.vm.saveContact();
    expect(apiPost).not.toHaveBeenCalled();
    expect(wrapper.vm.contactError).toBeTruthy();
  });

  it('edits an existing contact via PATCH, not a delete-and-re-add', async () => {
    const wrapper = await openContactsTab(await mountView());
    wrapper.vm.editContact(CONTACTS[0]);
    wrapper.vm.contactForm.email = 'jeff.new@example.com';
    await wrapper.vm.saveContact();
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/customers/cust-1/contacts/c1',
      expect.objectContaining({ email: 'jeff.new@example.com' }),
    );
    expect(apiDelete).not.toHaveBeenCalled();
  });

  it("surfaces the server's reason inline when a save is refused", async () => {
    apiPatch.mockRejectedValueOnce(new Error('This is the default recipient for the account'));
    const wrapper = await openContactsTab(await mountView());
    wrapper.vm.editContact(CONTACTS[0]);
    wrapper.vm.contactForm.email = '';
    await wrapper.vm.saveContact();
    expect(wrapper.vm.contactError).toContain('default recipient');
  });

  it('asks before removing — the first click opens a dialog and deletes nothing', async () => {
    const wrapper = await openContactsTab(await mountView());
    await wrapper.get('[data-testid="delete-contact-c1"]').trigger('click');
    expect(apiDelete).not.toHaveBeenCalled();
    expect(wrapper.find('[data-testid="remove-contact-dialog"]').exists()).toBe(true);
  });

  it('warns that removing the default recipient moves email to the account address', async () => {
    const wrapper = await openContactsTab(await mountView());
    await wrapper.get('[data-testid="delete-contact-c1"]').trigger('click');
    expect(wrapper.get('[data-testid="remove-primary-warning"]').text()).toContain('account@example.com');
  });

  it('removes only after the confirm is pressed', async () => {
    const wrapper = await openContactsTab(await mountView());
    await wrapper.get('[data-testid="delete-contact-c2"]').trigger('click');
    await wrapper.get('[data-testid="confirm-remove-contact-btn"]').trigger('click');
    await flushPromises();
    expect(apiDelete).toHaveBeenCalledWith('/api/customers/cust-1/contacts/c2');
  });

  it('says so when the removed person was the default recipient', async () => {
    // The server reports was_primary; saying nothing means the office finds out
    // weeks later that automated email quietly changed address.
    apiDelete.mockResolvedValueOnce({ ok: true, was_primary: true, fallback: 'account_email' });
    const wrapper = await openContactsTab(await mountView());
    await wrapper.get('[data-testid="delete-contact-c1"]').trigger('click');
    await wrapper.get('[data-testid="confirm-remove-contact-btn"]').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ detail: expect.stringContaining('account email') }),
    );
  });

  it('hides every write control from a role without customers.contact_write', async () => {
    // accounting and viewer reach this page but cannot write contacts. Showing
    // the buttons anyway makes every click a "Permission denied" dead end.
    hasPermission.mockReturnValue(false);
    const wrapper = await openContactsTab(await mountView());
    expect(wrapper.find('[data-testid="add-contact-btn"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="edit-contact-c1"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="delete-contact-c1"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="make-primary-c3"]').exists()).toBe(false);
    // but the list itself still reads — they can see who to call
    expect(wrapper.get('[data-testid="tab-contacts-content"]').text()).toContain('Sam');
  });

  it('says the load failed rather than claiming there are no contacts', async () => {
    apiGet.mockImplementation((url) =>
      url.includes('/contacts') ? Promise.reject(new Error('boom')) : routeGet(url));
    const wrapper = await openContactsTab(await mountView());
    expect(wrapper.find('[data-testid="contacts-empty"]').exists()).toBe(false);
    expect(wrapper.get('[data-testid="contacts-error"]').text()).toBeTruthy();
  });

  it('shows an empty state that tells the office what a contact is for', async () => {
    apiGet.mockImplementation((url) =>
      url.includes('/contacts') ? Promise.resolve([]) : routeGet(url),
    );
    const wrapper = await openContactsTab(await mountView());
    expect(wrapper.get('[data-testid="contacts-empty"]').text()).toMatch(/property manager/i);
  });
});

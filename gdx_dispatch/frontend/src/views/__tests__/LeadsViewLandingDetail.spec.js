/**
 * LeadsView — Landing Leads detail dialog + Phone column.
 *
 * Regression for the 2026-05-17 lost-business report: a web-form submission
 * landed in `landing_leads` (name/email/phone/message captured by the public
 * intake route shipped 2026-05-13) but the Inbound Leads page rendered only
 * Name/Email/Source/Submitted — the visitor's phone number and the message
 * they typed were invisible with no way to read them.
 *
 * Pins: (1) the Landing Leads table renders the phone value; (2) clicking a
 * submission row opens a read-only dialog exposing the FULL message text.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { createRouter, createMemoryHistory } from 'vue-router';
import LeadsView from '../LeadsView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: vi.fn() }) }));

const apiGet = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

const hasPermission = vi.fn(() => true);
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ hasPermission }),
}));

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Card: { template: '<div><slot name="title" /><slot /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  Column: { template: '<div />' },
  Badge: { template: '<span />' },
  ProgressSpinner: { template: '<div />' },
  InputText: { template: '<input />' },
  Textarea: { template: '<textarea />' },
  Select: { template: '<select />' },
  // Render rows so we can assert phone + trigger the row-click that opens
  // the detail dialog (matches PrimeVue DataTable's row-click event shape).
  DataTable: {
    props: ['value'],
    emits: ['row-click'],
    template: `<table><tbody>
        <tr v-for="(row, i) in (value || [])" :key="i" class="dt-row"
            @click="$emit('row-click', { data: row })">
          <td class="dt-name">{{ row.name }}</td>
          <td class="dt-phone">{{ row.phone }}</td>
        </tr>
      </tbody><slot /></table>`,
  },
  Dialog: {
    props: ['visible'],
    template: `<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>`,
  },
  Button: {
    props: ['label'],
    emits: ['click'],
    template: `<button @click="$emit('click')">{{ label }}<slot /></button>`,
  },
};

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/jobs', name: 'jobs', component: { template: '<div />' } },
    ],
  });
}

const LANDING = {
  id: 'll-1',
  name: 'Jane Doe',
  email: 'jane@acme.com',
  phone: '555-0101',
  source: 'website',
  status: 'new',
  message: "My opener won't close — can someone come Tuesday morning?",
  referrer: 'https://example.com/contact',
  utm_campaign: null,
  utm_source: null,
  utm_medium: null,
  created_at: '2026-05-16T14:02:00Z',
  contacted_at: null,
};

describe('LeadsView — Landing Leads detail', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGet.mockReset();
    hasPermission.mockReturnValue(true);
    apiGet.mockImplementation((url) => {
      if (url === '/api/leads') return Promise.resolve([]);
      if (url === '/api/leads/pipeline-summary') return Promise.resolve({});
      if (url === '/api/landing-leads') return Promise.resolve([LANDING]);
      return Promise.resolve([]);
    });
  });

  afterEach(() => vi.restoreAllMocks());

  function mountView() {
    return mount(LeadsView, {
      global: { stubs, plugins: [makeRouter()], directives: { tooltip: {} } },
    });
  }

  it('renders the visitor phone number in the Landing Leads table', async () => {
    const w = mountView();
    await flushPromises();
    const phones = w.findAll('.dt-phone').map((n) => n.text());
    expect(phones).toContain('555-0101');
  });

  it('opens a detail dialog with the full message when a submission row is clicked', async () => {
    const w = mountView();
    await flushPromises();

    // No detail dialog until a row is clicked.
    expect(w.find('[data-testid="landing-message"]').exists()).toBe(false);

    // The landing row is the one whose name matches the submission.
    const row = w.findAll('.dt-row').find((r) => r.text().includes('Jane Doe'));
    expect(row).toBeTruthy();
    await row.trigger('click');
    await flushPromises();

    const msg = w.find('[data-testid="landing-message"]');
    expect(msg.exists()).toBe(true);
    expect(msg.text()).toBe(
      "My opener won't close — can someone come Tuesday morning?",
    );
    // Contact info + provenance also surfaced in the dialog.
    const dialogText = w.find('.dlg').text();
    expect(dialogText).toContain('jane@acme.com');
    expect(dialogText).toContain('555-0101');
    expect(dialogText).toContain('example.com/contact');
  });
});

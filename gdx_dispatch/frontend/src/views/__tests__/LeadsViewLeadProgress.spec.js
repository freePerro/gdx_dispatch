/**
 * LeadsView — the Progress column follows the lead's pick.
 *
 * Moving "counts as won" in the lead dialog changes which estimate's job the
 * row's Progress chip tracks, and only the server knows that job's state — so
 * the list reloads when the dialog closes, however it closes.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { createRouter, createMemoryHistory } from 'vue-router';
import LeadsView from '../LeadsView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: vi.fn() }) }));

const apiGet = vi.fn();
const apiPost = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    post: apiPost,
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
  Tag: { template: '<span />' },
  ProgressSpinner: { template: '<div />' },
  InputText: { template: '<input />' },
  Textarea: { template: '<textarea />' },
  Select: { template: '<select />' },
  DataTable: {
    props: ['value'],
    emits: ['row-click'],
    template: `<table><tbody>
        <tr v-for="(row, i) in (value || [])" :key="i" class="dt-row"
            @click="$emit('row-click', { data: row })">
          <td>{{ row.name }}</td>
        </tr>
      </tbody><slot /></table>`,
  },
  Dialog: {
    props: ['visible'],
    template: `<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>`,
  },
  // The lead dialog's estimates panel: a stub that can report a pick change.
  LeadEstimatesPanel: {
    props: ['leadId', 'canWrite'],
    emits: ['selected'],
    template: `<button data-testid="stub-pick" @click="$emit('selected', { id: leadId, selected_estimate_id: 'est-2' })">pick</button>`,
  },
  Button: {
    props: ['label', 'loading', 'disabled'],
    emits: ['click'],
    template: `<button :data-testid="$attrs['data-testid']" @click="$emit('click')">{{ label }}<slot /></button>`,
    inheritAttrs: false,
  },
};

const LANDING = {
  id: 'll-1',
  name: 'Jane Doe',
  email: 'jane@acme.com',
  phone: '555-0101',
  source: 'website',
  status: 'new',
  message: 'Opener is dead',
  created_at: '2026-08-08T14:02:00Z',
  contacted_at: null,
};

const LEAD = {
  id: 'lead-9',
  name: 'Bob Builder',
  email: 'bob@build.test',
  phone: '555-0102',
  stage: 'new',
  estimated_value: 0,
  source: 'website',
  notes: 'Wants a new door',
  converted_customer_id: null,
  created_at: '2026-08-08T10:00:00Z',
};

function makeRouter() {
  return createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }] });
}

describe('LeadsView — Progress follows the pick', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGet.mockReset();
    apiPost.mockReset();
    hasPermission.mockReturnValue(true);
    apiGet.mockImplementation((url) => {
      if (url === '/api/leads') return Promise.resolve([LEAD]);
      if (url === '/api/leads/pipeline-summary') return Promise.resolve({});
      if (url === '/api/landing-leads') return Promise.resolve([]);
      return Promise.resolve([]);
    });
  });
  afterEach(() => vi.restoreAllMocks());

  const leadListCalls = () => apiGet.mock.calls.filter(([u]) => u === '/api/leads').length;

  async function openLead() {
    const w = mount(LeadsView, { global: { stubs, plugins: [makeRouter()], directives: { tooltip: {} } } });
    await flushPromises();
    await w.findAll('.dt-row').find((r) => r.text().includes('Bob Builder')).trigger('click');
    await flushPromises();
    return w;
  }

  it('reloads the list when the dialog closes after the pick moved', async () => {
    const w = await openLead();
    const before = leadListCalls();
    await w.find('[data-testid="stub-pick"]').trigger('click');
    await flushPromises();
    const cancel = w.findAll('button').find((b) => b.text() === 'Cancel');
    await cancel.trigger('click');
    await flushPromises();
    expect(leadListCalls()).toBe(before + 1);
  });

  it('does not reload when the dialog closes with the pick untouched', async () => {
    const w = await openLead();
    const before = leadListCalls();
    const cancel = w.findAll('button').find((b) => b.text() === 'Cancel');
    await cancel.trigger('click');
    await flushPromises();
    expect(leadListCalls()).toBe(before);
  });
});

/**
 * LeadsView — lead → the actual work (2026-08-08).
 *
 * A web form is usually a service call or an estimate request, and often
 * needs an email reply; the leads page used to dead-end all three. Pins:
 * (1) landing "Service call" chains promote → convert(won) → job create →
 *     navigate to the job;
 * (2) landing "Reply by email" routes to the REAL inbox composer (not
 *     mailto:) carrying landing_lead_id so the send can stamp contacted;
 * (3) pipeline "Create estimate" converts with stage=quoted and routes to
 *     /estimates/new with the customer pre-selected (no bare $0 estimate).
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
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/inbox', name: 'inbox', component: { template: '<div />' } },
      { path: '/jobs/:id', name: 'job-detail', component: { template: '<div />' } },
      { path: '/estimates/new', name: 'estimate-create', component: { template: '<div />' } },
    ],
  });
}

describe('LeadsView — lead actions', () => {
  let router;
  let push;

  beforeEach(() => {
    setActivePinia(createPinia());
    apiGet.mockReset();
    apiPost.mockReset();
    hasPermission.mockReturnValue(true);
    apiGet.mockImplementation((url) => {
      if (url === '/api/leads') return Promise.resolve([LEAD]);
      if (url === '/api/leads/pipeline-summary') return Promise.resolve({});
      if (url === '/api/landing-leads') return Promise.resolve([LANDING]);
      return Promise.resolve([]);
    });
    router = makeRouter();
    push = vi.spyOn(router, 'push');
  });

  afterEach(() => vi.restoreAllMocks());

  function mountView() {
    return mount(LeadsView, {
      global: { stubs, plugins: [router], directives: { tooltip: {} } },
    });
  }

  async function openLandingDialog(w) {
    const row = w.findAll('.dt-row').find((r) => r.text().includes('Jane Doe'));
    await row.trigger('click');
    await flushPromises();
  }

  it('landing "Service call" chains promote → convert(won) → job → navigate', async () => {
    apiPost.mockImplementation((url) => {
      if (url.endsWith('/convert-to-lead')) {
        return Promise.resolve({ id: 'lead-new', name: 'Jane Doe', notes: 'Opener is dead', stage: 'new' });
      }
      if (url.endsWith('/convert-to-customer')) {
        return Promise.resolve({ converted: true, customer_id: 'cust-1', existing: false });
      }
      if (url === '/api/jobs') return Promise.resolve({ id: 'job-1' });
      return Promise.resolve({});
    });
    const w = mountView();
    await flushPromises();
    await openLandingDialog(w);

    await w.find('[data-testid="landing-service-call"]').trigger('click');
    await flushPromises();

    const urls = apiPost.mock.calls.map((c) => c[0]);
    expect(urls).toContain('/api/landing-leads/ll-1/convert-to-lead');
    expect(urls).toContain('/api/leads/lead-new/convert-to-customer');
    const convBody = apiPost.mock.calls.find((c) => c[0].endsWith('/convert-to-customer'))[1];
    expect(convBody).toEqual({ stage: 'won' });
    const jobBody = apiPost.mock.calls.find((c) => c[0] === '/api/jobs')[1];
    expect(jobBody.customer_id).toBe('cust-1');
    expect(jobBody.title).toContain('Jane Doe');
    expect(push).toHaveBeenCalledWith('/jobs/job-1');
  });

  it('landing "Reply by email" routes to the inbox composer with the stamp id', async () => {
    const w = mountView();
    await flushPromises();
    await openLandingDialog(w);

    await w.find('[data-testid="landing-email"]').trigger('click');
    expect(push).toHaveBeenCalledWith({
      path: '/inbox',
      query: {
        to: 'jane@acme.com',
        subject: 'Your website inquiry',
        landing_lead_id: 'll-1',
      },
    });
  });

  it('"Estimate" converts with stage=quoted and routes to /estimates/new', async () => {
    // Driven through the landing dialog's Estimate button (the stubbed
    // DataTable can't render Column body templates, so the pipeline-row
    // button isn't reachable here) — both share createEstimateFromLead.
    apiPost.mockImplementation((url) => {
      if (url.endsWith('/convert-to-lead')) {
        return Promise.resolve({ id: 'lead-new', name: 'Jane Doe', notes: 'Opener is dead', stage: 'new' });
      }
      if (url.endsWith('/convert-to-customer')) {
        return Promise.resolve({ converted: true, customer_id: 'cust-7', existing: true });
      }
      return Promise.resolve({});
    });
    const w = mountView();
    await flushPromises();
    await openLandingDialog(w);

    await w.find('[data-testid="landing-estimate"]').trigger('click');
    await flushPromises();

    const convBody = apiPost.mock.calls.find((c) => c[0] === '/api/leads/lead-new/convert-to-customer')[1];
    expect(convBody).toEqual({ stage: 'quoted' });
    expect(push).toHaveBeenCalledWith({
      path: '/estimates/new',
      query: { customer_id: 'cust-7' },
    });
  });
});

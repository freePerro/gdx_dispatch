/**
 * Job Costing — estimate-vs-actual labor (GDXA-1561).
 *
 * GET /api/jobs/{id}/labor-variance had no caller anywhere in the SPA; only
 * its backend tests reached it. These tests pin that the Cost breakdown dialog
 * calls it for the job it opened, and that each of its states (loaded, empty,
 * error, loading) shows on screen. If the call is ever removed, the first test
 * goes red and the route is an orphan again.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {}, path: '/job-costing' }),
}));

const mountOpts = { global: { stubs: { teleport: true } } };

const VARIANCE = {
  job_id: 'job-1',
  estimate_id: 'est-1',
  estimate_status: 'accepted',
  estimated_hours: 5,
  actual_hours: 6.5,
  variance_hours: 1.5,
  estimated_cost: 250,
  actual_cost: 325,
  variance_cost: 75,
  primary_rate: 50,
  primary_rate_source: 'technician',
  per_tech: [
    {
      assignment_id: 'a-1', tech_id: 't-1', user_id: 'u-1', tech_name: 'Pat Lead', is_lead: true,
      arrived_at: '2026-05-01T13:00:00+00:00', completed_at: '2026-05-01T17:00:00+00:00',
      actual_hours: 4, hourly_rate: 50, rate_source: 'technician', actual_cost: 200,
    },
    {
      assignment_id: 'a-2', tech_id: 't-2', user_id: null, tech_name: null, is_lead: false,
      arrived_at: '2026-05-01T13:00:00+00:00', completed_at: '2026-05-01T15:30:00+00:00',
      actual_hours: 2.5, hourly_rate: 50, rate_source: 'technician', actual_cost: 125,
    },
  ],
};

const EMPTY = {
  ...VARIANCE,
  estimate_id: null,
  estimated_hours: 0,
  actual_hours: 0,
  variance_hours: 0,
  estimated_cost: 0,
  actual_cost: 0,
  variance_cost: 0,
  per_tech: [],
};

function routeApi(variance) {
  apiGet.mockImplementation(async (url) => {
    if (url.startsWith('/api/costing/jobs/')) return { job_id: 'job-1', total_cost: 100, invoiced_amount: 200, margin_percent: 50 };
    if (url.endsWith('/labor-variance')) return variance();
    if (url.includes('/line-items')) return { items: [] };
    if (url.startsWith('/api/costing/profitability')) {
      return [{ job_id: 'job-1', invoice_total: 200, cost_estimate: 100, profit: 100, margin_percent: 50 }];
    }
    return [];
  });
}

async function openDialog() {
  const { default: View } = await import('../JobCostingView.vue');
  const w = mount(View, mountOpts);
  await flushPromises();
  const details = w.findAll('button').filter((b) => b.text().includes('Details'));
  await details[0].trigger('click');
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('GDXA-1561 — labor variance on Job Costing', () => {
  it('calls /api/jobs/{id}/labor-variance for the opened job', async () => {
    routeApi(() => VARIANCE);
    await openDialog();
    const calls = apiGet.mock.calls.filter((c) => String(c[0]).endsWith('/labor-variance'));
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe('/api/jobs/job-1/labor-variance');
  });

  it('shows estimated vs on-site hours, both variances and the per-tech rows', async () => {
    routeApi(() => VARIANCE);
    const w = await openDialog();
    const hours = w.find('[data-testid="jc-labor-hours"]').text();
    expect(hours).toContain('6.50 h');
    expect(hours).toContain('5.00 h estimated');
    const vh = w.find('[data-testid="jc-labor-variance-hours"]');
    expect(vh.text()).toContain('+1.50 h');
    expect(vh.text()).toContain('Over estimate');
    expect(vh.find('.variance-over').exists()).toBe(true);
    const vc = w.find('[data-testid="jc-labor-variance-cost"]').text();
    expect(vc).toContain('+$75.00');
    expect(vc).toContain('$325.00');
    expect(vc).toContain('$250.00');
    const table = w.find('[data-testid="jc-labor-per-tech"]').text();
    expect(table).toContain('Pat Lead');
    expect(table).toContain('Lead');
    expect(table).toContain('Unnamed technician');
    expect(table).toContain('technician rate');
    // Clock time is labelled as clock time, never as billed labor.
    expect(w.find('[data-testid="jc-labor-variance"]').text()).toMatch(/not the\s+hours billed/);
  });

  it('does not call a job with no estimated hours "on estimate"', async () => {
    routeApi(() => ({ ...EMPTY, actual_hours: 4, variance_hours: 4, actual_cost: 200, variance_cost: 200, per_tech: VARIANCE.per_tech.slice(0, 1) }));
    const w = await openDialog();
    const vh = w.find('[data-testid="jc-labor-variance-hours"]').text();
    expect(vh).toContain('No estimated hours to compare');
    expect(vh).not.toContain('Over estimate');
    expect(w.find('[data-testid="jc-labor-variance-cost"]').text()).not.toContain('+$200.00');
    expect(w.find('[data-testid="jc-labor-hours"]').text()).toContain('(no estimate)');
  });

  it('shows an empty state when there is nothing to compare', async () => {
    routeApi(() => EMPTY);
    const w = await openDialog();
    expect(w.find('[data-testid="jc-labor-empty"]').exists()).toBe(true);
    expect(w.find('[data-testid="jc-labor-per-tech"]').exists()).toBe(false);
    expect(w.find('[data-testid="jc-labor-variance-hours"]').exists()).toBe(false);
  });

  it('shows the error in the section and retries on request', async () => {
    let fail = true;
    routeApi(() => {
      if (fail) throw new Error('Server error (500)');
      return VARIANCE;
    });
    const w = await openDialog();
    const err = w.find('[data-testid="jc-labor-error"]');
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain('Server error (500)');
    // The section shows the error itself; a toast on top would say it twice.
    const call = apiGet.mock.calls.find((c) => String(c[0]).endsWith('/labor-variance'));
    expect(call[1]).toEqual({ suppressErrorToast: true });

    fail = false;
    await err.find('button').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="jc-labor-error"]').exists()).toBe(false);
    expect(w.find('[data-testid="jc-labor-hours"]').text()).toContain('6.50 h');
  });

  it("never shows one job's labor under another job's heading", async () => {
    // Open A, close, open B; A's reply lands after B's. B must keep B's numbers.
    const pending = {};
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/costing/jobs/')) {
        const id = url.split('/').pop();
        return { job_id: id, total_cost: 1, invoiced_amount: 2, margin_percent: 50 };
      }
      if (url.endsWith('/labor-variance')) {
        const id = url.split('/')[3];
        return new Promise((resolve) => { pending[id] = resolve; });
      }
      if (url.includes('/line-items')) return { items: [] };
      if (url.startsWith('/api/costing/profitability')) {
        return [
          { job_id: 'job-a', invoice_total: 2, cost_estimate: 1, profit: 1, margin_percent: 50 },
          { job_id: 'job-b', invoice_total: 2, cost_estimate: 1, profit: 1, margin_percent: 50 },
        ];
      }
      return [];
    });
    const { default: View } = await import('../JobCostingView.vue');
    const w = mount(View, mountOpts);
    await flushPromises();
    const details = () => w.findAll('button').filter((b) => b.text().includes('Details'));
    await details()[0].trigger('click');
    await flushPromises();
    await details()[1].trigger('click');
    await flushPromises();
    pending['job-b']({ ...VARIANCE, job_id: 'job-b', estimated_hours: 2, actual_hours: 3, variance_hours: 1 });
    await flushPromises();
    pending['job-a']({ ...VARIANCE, job_id: 'job-a', estimated_hours: 1, actual_hours: 9, variance_hours: 8 });
    await flushPromises();
    expect(w.find('.job-detail-header h3').text()).toContain('job-b');
    const hours = w.find('[data-testid="jc-labor-hours"]').text();
    expect(hours).toContain('3.00 h');
    expect(hours).toContain('2.00 h estimated');
    expect(w.find('[data-testid="jc-labor-loading"]').exists()).toBe(false);
  });

  // GDXA-1572: a job-id compare lets both A requests through on A→B→A.
  function deferredApi() {
    const pending = { detail: [], variance: [], lines: [] };
    const defer = (bucket, id) => new Promise((resolve, reject) => { pending[bucket].push({ id, resolve, reject }); });
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/costing/jobs/')) {
        const id = url.split('/').pop();
        return defer('detail', id);
      }
      if (url.endsWith('/labor-variance')) return defer('variance', url.split('/')[3]);
      if (url.includes('/line-items')) return defer('lines', url.split('/')[3]);
      if (url.startsWith('/api/costing/profitability')) {
        return [
          { job_id: 'job-a', invoice_total: 2, cost_estimate: 1, profit: 1, margin_percent: 50 },
          { job_id: 'job-b', invoice_total: 2, cost_estimate: 1, profit: 1, margin_percent: 50 },
        ];
      }
      return [];
    });
    return pending;
  }
  const detailFor = (id) => ({ job_id: id, total_cost: 1, invoiced_amount: 2, margin_percent: 50 });

  async function mountView() {
    const { default: View } = await import('../JobCostingView.vue');
    const w = mount(View, mountOpts);
    await flushPromises();
    const details = () => w.findAll('button').filter((b) => b.text().includes('Details'));
    return { w, details };
  }

  it('keeps the newer visit\'s labor when the first visit to the same job fails late (A→B→A)', async () => {
    const pending = deferredApi();
    const { w, details } = await mountView();
    for (const i of [0, 1, 0]) {
      await details()[i].trigger('click');
      await flushPromises();
      pending.detail.at(-1).resolve(detailFor(i === 0 ? 'job-a' : 'job-b'));
      await flushPromises();
    }
    expect(pending.variance.map((p) => p.id)).toEqual(['job-a', 'job-b', 'job-a']);
    pending.variance[2].resolve({ ...VARIANCE, job_id: 'job-a', estimated_hours: 2, actual_hours: 3 });
    await flushPromises();
    pending.variance[1].resolve({ ...VARIANCE, job_id: 'job-b', estimated_hours: 7, actual_hours: 8 });
    pending.variance[0].reject(new Error('Server error (500)'));
    await flushPromises();
    expect(w.find('.job-detail-header h3').text()).toContain('job-a');
    expect(w.find('[data-testid="jc-labor-error"]').exists()).toBe(false);
    const hours = w.find('[data-testid="jc-labor-hours"]').text();
    expect(hours).toContain('3.00 h');
    expect(hours).toContain('2.00 h estimated');
    expect(w.find('[data-testid="jc-labor-loading"]').exists()).toBe(false);
  });

  it("never opens the dialog on an earlier job's late detail reply", async () => {
    const pending = deferredApi();
    const { w, details } = await mountView();
    await details()[0].trigger('click');
    await flushPromises();
    await details()[1].trigger('click');
    await flushPromises();
    pending.detail[1].resolve(detailFor('job-b'));
    await flushPromises();
    pending.detail[0].resolve(detailFor('job-a'));
    await flushPromises();
    expect(w.find('.job-detail-header h3').text()).toContain('job-b');
    expect(pending.variance.map((p) => p.id)).toEqual(['job-b']);
  });

  it("never shows one job's invoice lines under another job", async () => {
    const pending = deferredApi();
    const { w, details } = await mountView();
    for (const i of [0, 1]) {
      await details()[i].trigger('click');
      await flushPromises();
      pending.detail.at(-1).resolve(detailFor(i === 0 ? 'job-a' : 'job-b'));
      await flushPromises();
    }
    expect(pending.lines.map((p) => p.id)).toEqual(['job-a', 'job-b']);
    pending.lines[1].resolve({ items: [{ id: 'l-b', description: 'Line for B', quantity: 1, unit_price: 1, total: 1 }] });
    await flushPromises();
    pending.lines[0].resolve({ items: [{ id: 'l-a', description: 'Line for A', quantity: 1, unit_price: 1, total: 1 }] });
    await flushPromises();
    const text = w.text();
    expect(text).toContain('Line for B');
    expect(text).not.toContain('Line for A');
  });

  it('shows a loading state while the request is in flight', async () => {
    let release;
    routeApi(() => new Promise((resolve) => { release = () => resolve(VARIANCE); }));
    const w = await openDialog();
    expect(w.find('[data-testid="jc-labor-loading"]').exists()).toBe(true);
    release();
    await flushPromises();
    expect(w.find('[data-testid="jc-labor-loading"]').exists()).toBe(false);
    expect(w.find('[data-testid="jc-labor-hours"]').exists()).toBe(true);
  });
});

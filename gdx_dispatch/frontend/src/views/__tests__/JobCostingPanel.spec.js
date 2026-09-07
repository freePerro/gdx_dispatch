/**
 * Job Costing — the "Cost breakdown" dialog (#653 / #455).
 *
 * Every write control in this dialog was wired to an endpoint that does not
 * exist: GET and POST /api/jobs/{id}/parts 404, DELETE 405, PATCH 501, and
 * PATCH /api/jobs/{id}/costing 405. It never saved anything, in any verb. The
 * `|| []` on the read made the 404 look identical to "this job has no parts",
 * so the failure was silent as well as total.
 *
 * It is now a profitability summary that POINTS at the parts detail rather than
 * rendering its own. A first attempt at this fix rebuilt the parts table here
 * from /api/costing/jobs/{id} — and JobDetailView's "Parts Used" card (#477)
 * already renders that same payload, better: it handles the ambiguous case,
 * where unattributed supplier lines mean the catalog estimates might be the same
 * spend and the engine EXCLUDES them from the total rather than double-counting.
 * Replacing a parallel fake with a weaker parallel duplicate is the same defect
 * wearing a different hat.
 *
 * What these tests pin:
 *  1. The dialog never fetches the dead /api/jobs/{id}/parts.
 *  2. No write control survives — a button that cannot save is the defect.
 *  3. "Your Cost" is the SERVER's total, not a client sum over rows that never
 *     loaded (it used to silently report invoice lines only).
 *  4. There is no second parts table, and there IS a way to the one that exists.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();
const routerPush = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush, back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {}, path: '/costing' }),
}));

// Real PrimeVue components, not hand-rolled stubs: tests/setup.js registers the
// plugin globally, and a stubbed DataTable cannot render Column children per row
// — which is exactly where the parts table lives, so stubbing it would have
// tested the stub. Teleport is stubbed so the modal's content renders inline
// where the wrapper can see it.
const mountOpts = { global: { stubs: { teleport: true } } };

const COSTING = {
  job_id: 'job-1',
  parts: {
    total: 310.5,
    items: [
      { name: 'Torsion spring', qty: 2, unit_cost: 88.25, subtotal: 176.5, source: 'vendor_bill', cost_known: true, is_estimate: false },
      { name: 'Nylon roller', qty: 10, unit_cost: 13.4, subtotal: 134, source: 'catalog_estimate', cost_known: true, is_estimate: true },
      { name: 'Bearing plate', qty: 1, unit_cost: 0, subtotal: 0, source: 'parts_needed', cost_known: false, is_estimate: false },
    ],
  },
  labor: { total: 240 },
  total_cost: 605.25,
  invoiced_amount: 900,
  margin_percent: 32.75,
  cost_incomplete: true,
  unknown_cost_parts: 1,
  unlinked_bill_lines: 0,
  estimated_parts_cost: 134,
  actual_parts_cost: 176.5,
  catalog_variance: 12.75,
};

// The dialog is opened the way a user opens it — the "Details" button on the
// profitability row — not by poking an internal. `<script setup>` exposes
// nothing on the vm, and a test that reached past the UI would not notice the
// button being removed.
async function openDialog(costing = COSTING) {
  const { default: View } = await import('../JobCostingView.vue');
  apiGet.mockImplementation(async (url) => {
    if (url.startsWith('/api/costing/jobs/')) return costing;
    if (url.includes('/line-items')) return { items: [], total: 0 };
    if (url.startsWith('/api/costing/profitability')) {
      return [{ job_id: 'job-1', invoice_total: 900, cost_estimate: 605.25, profit: 294.75, margin_percent: 32.75 }];
    }
    if (url.startsWith('/api/costing/markup-rules')) return [];
    return [];
  });
  const w = mount(View, mountOpts);
  await flushPromises();
  const details = w.findAll('button').filter((b) => b.text().includes('Details'));
  expect(details.length, 'the Details button opens the dialog').toBeGreaterThan(0);
  await details[0].trigger('click');
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  apiPost.mockResolvedValue({});
  apiPatch.mockResolvedValue({});
  apiDel.mockResolvedValue({});
});

describe('#653 — the Job Costing dialog reports instead of pretending to edit', () => {
  it('never calls the dead parts endpoints', async () => {
    await openDialog();
    const called = apiGet.mock.calls.map((c) => String(c[0]));
    // The exact 404 this issue is named for.
    expect(called.some((u) => /\/api\/jobs\/[^/]+\/parts$/.test(u))).toBe(false);
    // ...and it DOES read the endpoint that works, so this is not vacuous.
    expect(called.some((u) => u.startsWith('/api/costing/jobs/'))).toBe(true);
  });

  it('does not render a second parts table', async () => {
    // JobDetailView's "Parts Used" card already renders this payload and gets
    // the ambiguous-estimate case right. A copy here would drift from it, and
    // the drift would be silent — two screens quoting different parts costs for
    // the same job.
    const w = await openDialog();
    expect(w.find('[data-testid="parts-table"]').exists()).toBe(false);
    expect(w.find('[data-testid="jc-cost-incomplete"]').exists()).toBe(false);
    // ...but the dialog still says where that detail lives.
    expect(w.find('[data-testid="jc-parts-pointer"]').text()).toContain('Parts Used');
  });

  it('shows the SERVER total as Your Cost, not a client sum', async () => {
    // The old computed added a parts grid that never loaded to a line-items
    // grid, so it silently reported invoice lines only — understating the one
    // number this dialog exists to show by every part on the job.
    const w = await openDialog();
    const card = w.find('[data-testid="your-cost-card"]');
    expect(card.text()).toContain('605.25');
    // The label has to name what the number is. It said "Parts + items" over a
    // figure that was neither.
    expect(w.find('[data-testid="your-cost-basis"]').text()).toContain('overhead');
    expect(card.text()).not.toContain('310.50');
    expect(card.text()).not.toContain('900');
  });

  it('drops the Min Margin control, which could never compute', async () => {
    // It filtered rows below a margin threshold using unit_cost — and
    // GET /api/jobs/{id}/line-items returns no unit_cost, so every positive
    // price scored 100% and the warning was unreachable. Its one firing input
    // was a $0 line, which it mislabelled as "margin below 15%".
    const w = await openDialog();
    expect(w.find('[data-testid="min-margin-input"]').exists()).toBe(false);
    expect(w.find('[data-testid="min-margin-warning"]').exists()).toBe(false);
  });

  it('has no control that cannot save', async () => {
    const w = await openDialog();
    for (const id of [
      'add-part-btn',
      'confirm-add-part',
      'add-item-btn',
      'confirm-add-item',
      'save-costing-btn',
    ]) {
      expect(w.find(`[data-testid="${id}"]`).exists()).toBe(false);
    }
    // Nothing in the dialog writes at all.
    expect(apiPost).not.toHaveBeenCalled();
    expect(apiPatch).not.toHaveBeenCalled();
    expect(apiDel).not.toHaveBeenCalled();
  });

  it('points at the surface where parts are actually edited', async () => {
    // Read-only is only honest if there is a way in. Parts are captured on the
    // job (parts-needed), which is where every capture path already writes.
    const w = await openDialog();
    const link = w.find('[data-testid="jc-edit-parts-on-job"]');
    expect(link.exists()).toBe(true);
    await link.trigger('click');
    // /jobs/:id is the page whose Parts Used card owns this data.
    expect(routerPush).toHaveBeenCalledWith('/jobs/job-1');
  });

});

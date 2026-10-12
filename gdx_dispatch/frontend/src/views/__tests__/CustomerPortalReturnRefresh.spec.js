/**
 * Customer portal: show what the server already knows (GDXA-746).
 *
 *  1. Paying opens the pay page in another tab. Coming back to the portal tab
 *     re-reads estimates and invoices, so a paid invoice stops saying unpaid —
 *     only when signed in, and the listener goes away on unmount.
 *  2. An estimate with a label still shows its number, on the card and on the
 *     detail dialog's header.
 *  3. A failure that has a server reason shows the reason (deposit pay), and a
 *     failed photo load is not reported as "no photos".
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const toastAdd = vi.hoisted(() => vi.fn());
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {}, params: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import CustomerPortalView from '../CustomerPortalView.vue';

const stubs = {
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" data-testid="dialog"><h2 data-testid="dialog-header">{{ header }}</h2><slot /><slot name="footer" /></div>',
  },
  Image: { template: '<img />' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
};

const UNPAID = { id: 'inv-1', invoice_number: 'INV-1', status: 'sent', payment_status: 'unpaid',
  total: 300, balance_due: 300, pay_url: 'https://example.test/pay/t' };
const PAID = { ...UNPAID, payment_status: 'paid', balance_due: 0, pay_url: null };
const LABELLED = { id: 'est-1', estimate_number: '1042', label: 'Front door', status: 'accepted', total: 2600,
  deposit_ask: { amount: 500 }, lines: [], images: [], totals: { subtotal: 2600, total: 2600 } };
const UNLABELLED = { ...LABELLED, id: 'est-2', estimate_number: '1043', label: null, deposit_ask: null };
const JOB = { id: 'job-1', title: 'Spring swap', lifecycle_stage: 'completed', photo_count: 2 };

let visibility = 'visible';
const UNPAID_2 = { ...UNPAID, id: 'inv-2', invoice_number: 'INV-2' };

function invoiceDetailFor(id) {
  const inv = invoicesNow.find((i) => i.id === id);
  return { ...inv, lines: [], totals: { total: inv.total, balance_due: inv.balance_due } };
}
let invoicesNow;
let overrides;
const mounted = [];

function ok(body) { return { ok: true, status: 200, json: async () => body }; }

async function mountPortal({ signedIn = true } = {}) {
  const fetchMock = vi.fn(async (url) => {
    const u = String(url);
    for (const [suffix, res] of Object.entries(overrides)) if (u.endsWith(suffix)) return res();
    const invMatch = u.match(/\/portal\/invoices\/([\w-]+)$/);
    if (invMatch) return ok(invoiceDetailFor(invMatch[1]));
    if (u.endsWith('/portal/estimates/est-1')) return ok(LABELLED);
    if (u.endsWith('/portal/estimates')) return ok([LABELLED, UNLABELLED]);
    if (u.endsWith('/portal/invoices')) return ok(invoicesNow);
    if (u.endsWith('/portal/jobs')) return ok([JOB]);
    if (u.includes('/portal/context')) return ok({ company: { name: 'GDX' } });
    return ok([]);
  });
  vi.stubGlobal('fetch', fetchMock);
  if (signedIn) sessionStorage.setItem('gdx_portal_jwt', 'portal-token');
  const w = mount(CustomerPortalView, { global: { stubs } });
  mounted.push(w);
  await flushPromises();
  return { w, fetchMock };
}

async function returnToTab() {
  visibility = 'visible';
  document.dispatchEvent(new Event('visibilitychange'));
  await flushPromises();
}

const listCalls = (fetchMock) => fetchMock.mock.calls
  .map(([u]) => String(u))
  .filter((u) => u.endsWith('/portal/invoices') || u.endsWith('/portal/estimates'));

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  toastAdd.mockClear();
  visibility = 'visible';
  invoicesNow = [UNPAID];
  overrides = {};
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
});
// Unmount every portal: a leftover one would still be listening on document.
afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount());
  vi.unstubAllGlobals();
});

describe('coming back from the pay tab', () => {
  it('re-reads invoices and estimates, so a paid invoice stops saying unpaid', async () => {
    const { w, fetchMock } = await mountPortal();
    expect(w.find('[data-testid="invoice-pay-btn"]').exists()).toBe(true);

    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    const before = listCalls(fetchMock).length;

    invoicesNow = [PAID]; // the customer paid in the other tab
    await returnToTab();

    expect(listCalls(fetchMock).length).toBe(before + 2);
    expect(w.find('[data-testid="invoice-pay-btn"]').exists()).toBe(false);
    expect(w.find('[data-testid="invoice-card"]').text()).toContain('Paid in full');
  });

  it('does not fetch when the tab is hidden', async () => {
    const { fetchMock } = await mountPortal();
    const before = fetchMock.mock.calls.length;
    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(fetchMock.mock.calls.length).toBe(before);
  });

  it('does not fetch when signed out', async () => {
    const { fetchMock } = await mountPortal({ signedIn: false });
    await returnToTab();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('stops listening once the page is gone', async () => {
    const { w, fetchMock } = await mountPortal();
    w.unmount();
    const before = fetchMock.mock.calls.length;
    await returnToTab();
    window.dispatchEvent(new Event('focus'));
    await flushPromises();
    expect(fetchMock.mock.calls.length).toBe(before);
  });

  it('an expired session on return says so', async () => {
    const { w } = await mountPortal();
    overrides['/portal/invoices'] = () => ({ ok: false, status: 401, json: async () => ({}) });
    await returnToTab();
    expect(w.text()).toContain('Your portal session has expired.');
  });
  it('the invoice dialog the customer paid from stops offering Pay', async () => {
    const { w, fetchMock } = await mountPortal();
    await w.find('[data-testid="invoice-card"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="invoice-detail-pay-btn"]').exists()).toBe(true);

    invoicesNow = [PAID];
    await returnToTab();

    const detailReads = fetchMock.mock.calls.filter(([u]) => String(u).endsWith('/portal/invoices/inv-1'));
    expect(detailReads).toHaveLength(2);
    expect(w.find('[data-testid="invoice-detail-pay-btn"]').exists()).toBe(false);
    expect(w.find('[data-testid="invoice-balance-due"]').text()).toContain('$0.00');
  });

  it('a refresh that succeeds clears a failed first load', async () => {
    overrides['/portal/jobs'] = () => ({ ok: false, status: 500, json: async () => ({}) });
    const { w } = await mountPortal();
    expect(w.find('[data-testid="portal-error"]').exists()).toBe(true);
    delete overrides['/portal/jobs'];
    await returnToTab();
    expect(w.find('[data-testid="portal-error"]').exists()).toBe(false);
    expect(w.find('[data-testid="job-photos-job-1"]').exists()).toBe(true);
  });

  it('a back-navigation restored from the page cache refreshes too', async () => {
    const { w } = await mountPortal();
    invoicesNow = [PAID];
    window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true }));
    await flushPromises();
    expect(w.find('[data-testid="invoice-pay-btn"]').exists()).toBe(false);
  });
  it('a pay window closing (focus back) refreshes too', async () => {
    const { w } = await mountPortal();
    invoicesNow = [PAID];
    window.dispatchEvent(new Event('focus'));
    await flushPromises();
    expect(w.find('[data-testid="invoice-pay-btn"]').exists()).toBe(false);
  });

  it.each(['/portal/jobs', '/portal/context'])('a failed %s read does not throw away the paid invoice', async (path) => {
    const { w } = await mountPortal();
    invoicesNow = [PAID];
    overrides[path] = () => ({ ok: false, status: 500, json: async () => ({}) });
    await returnToTab();
    expect(w.find('[data-testid="invoice-pay-btn"]').exists()).toBe(false);
  });

  it('a late detail read does not overwrite a dialog the customer switched away from', async () => {
    invoicesNow = [UNPAID, UNPAID_2];
    const { w } = await mountPortal();
    await w.findAll('[data-testid="invoice-card"]')[0].trigger('click');
    await flushPromises();

    let release;
    const held = new Promise((r) => { release = r; });
    overrides['/portal/invoices/inv-1'] = () => held;
    await returnToTab(); // the refresh's read of INV-1 is now in flight

    await w.findAll('[data-testid="invoice-card"]')[1].trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="dialog-header"]').text()).toBe('Invoice INV-2');

    release(ok(invoiceDetailFor('inv-1')));
    await flushPromises();
    expect(w.find('[data-testid="dialog-header"]').text()).toBe('Invoice INV-2');
  });
});

describe('estimate number next to the label', () => {
  it('the card shows label and number; an unlabelled one shows the number', async () => {
    const { w } = await mountPortal();
    const titles = w.findAll('[data-testid="estimate-card-title"]').map((t) => t.text());
    expect(titles).toEqual(['Front door · #1042', '1043']);
  });

  it('the detail dialog header carries the number too', async () => {
    const { w } = await mountPortal();
    await w.findAll('[data-testid="estimate-card"]')[0].trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="dialog-header"]').text()).toBe('Front door · #1042');
  });
});

describe('failures say what the server said', () => {
  it('a refused deposit pay shows the server reason, not a generic line', async () => {
    vi.spyOn(window, 'open').mockImplementation(() => null);
    overrides['/deposit/pay'] = () => ({ ok: false, status: 409,
      json: async () => ({ detail: 'accept the estimate before paying a deposit' }) });
    const { w } = await mountPortal();
    await w.find('[data-testid="pay-deposit-ask-btn"]').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      detail: 'accept the estimate before paying a deposit' }));
    window.open.mockRestore();
  });

  it('a failed photo load is not shown as "no photos"', async () => {
    overrides['/portal/jobs/job-1/photos'] = () => ({ ok: false, status: 500, json: async () => ({}) });
    const { w } = await mountPortal();
    await w.find('[data-testid="job-photos-job-1"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="job-photos-error"]').text()).toContain('Could not load the photos');
    expect(w.find('[data-testid="job-photos-empty"]').exists()).toBe(false);
  });
});

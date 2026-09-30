/**
 * CashCalendar (Forecasting page) and CashCalendarSummaryCard (Dashboard):
 *  1. Rows render dated, grouped by week, with in/out and the running balance;
 *     rows under the floor are marked, and each row links to its record.
 *  2. The low point and the first day under the floor are named.
 *  3. With no floor / default accounts the page says so; only owner/admin
 *     get the button that saves (the PUT is owner/admin only).
 *  4. Past-due / undated items are listed beside the calendar, not in it.
 *  5. The Dashboard card renders nothing without accounting.read, and links
 *     to the full calendar on Forecasting when it can.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPut = vi.fn();
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, put: apiPut }),
}));

let mockRole = 'owner';
let mockPerms = new Set(['accounting.read']);
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    get role() { return mockRole; },
    loadPermissions: vi.fn(async () => {}),
    hasPermission: (p) => mockPerms.has(p),
  }),
}));

import CashCalendar from '../forecasting/CashCalendar.vue';
import CashCalendarSummaryCard from '../forecasting/CashCalendarSummaryCard.vue';

const stubs = {
  RouterLink: RouterLinkStub,
  Button: { props: ['label'], emits: ['click'], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  SelectButton: { props: ['modelValue', 'options'], template: '<div class="sb" />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  ProgressSpinner: { template: '<div />' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Dialog: { props: ['visible'], template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>' },
  InputNumber: { template: '<input />' },
  Checkbox: { template: '<input type="checkbox" />' },
  Card: { template: '<div class="card"><slot name="title" /><slot name="content" /></div>' },
};

function calendar(overrides = {}) {
  return {
    as_of: '2026-10-01',
    days: 14,
    last_day: '2026-10-14',
    floor: 1000,
    accounts: {
      chosen: true,
      included: [{ id: 'a1', name: 'Operating', balance: 1000, balance_as_of: '2026-10-01T05:00:00+00:00' }],
      excluded: [{ id: 'a2', name: 'Credit line', balance: -20000, balance_as_of: null, reason: 'Negative balance' }],
    },
    starting_balance: 1000,
    balance_as_of: '2026-10-01T05:00:00+00:00',
    rows: [
      { date: '2026-10-03', direction: 'out', kind: 'vendor_bill', label: 'Supplier · bill B1', amount: 900,
        certainty: 'scheduled', link: { kind: 'vendor_bill', id: 'b1' }, balance_after: 100, balance_if_nothing_comes_in: 100 },
      { date: '2026-10-03', direction: 'in', kind: 'invoice', label: 'Smith · invoice INV-1', amount: 1549,
        certainty: 'scheduled', link: { kind: 'invoice', id: 'i1' }, balance_after: 1649, balance_if_nothing_comes_in: 100 },
      { date: '2026-10-09', direction: 'out', kind: 'recurring', label: 'Payroll', amount: 2900, detail: 'Recurring',
        certainty: 'expected', link: { kind: 'recurring_stream', id: 's1' }, balance_after: -1251, balance_if_nothing_comes_in: -2800 },
    ],
    summary: {
      total_in: 1549, total_out: 3800, ending_balance: -1251,
      lowest_balance: -1251, lowest_date: '2026-10-09', first_below_floor: '2026-10-03',
      lowest_if_nothing_comes_in: -2800, lowest_if_nothing_comes_in_date: '2026-10-09',
      first_below_floor_if_nothing_comes_in: '2026-10-03',
    },
    unscheduled: {
      customer_invoices_past_due: { count: 1, total: 300, items: [
        { label: 'Jones · invoice INV-0', amount: 300, due_date: '2026-09-20', link: { kind: 'invoice', id: 'i0' } },
      ] },
      finished_jobs_not_billed: { count: 0, total: 0, items: [] },
      vendor_bills_past_due_or_undated: { count: 0, total: 0, items: [] },
    },
    notes: { jobs_without_accepted_estimate: 0, jobs_already_invoiced: 0 },
    ...overrides,
  };
}

async function mountCalendar(data) {
  apiGet.mockResolvedValue(data);
  const w = mount(CashCalendar, { global: { stubs, directives: { tooltip: {} } } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPut.mockReset();
  mockRole = 'owner';
  mockPerms = new Set(['accounting.read']);
});

describe('CashCalendar', () => {
  it('asks for 14 days and renders dated rows grouped by week with running balance', async () => {
    const w = await mountCalendar(calendar());
    expect(apiGet).toHaveBeenCalledWith('/api/forecast/cash-calendar?days=14', undefined);
    const rows = w.findAll('[data-testid="cc-row"]');
    expect(rows).toHaveLength(3);
    expect(rows[0].text()).toContain('Supplier · bill B1');
    expect(rows[0].text()).toContain('$900.00');
    expect(rows[0].text()).toContain('$100.00');
    expect(w.findAll('.cc-week')).toHaveLength(2);
    // Under the floor of 1000 after the bill, and after payroll; not after the invoice.
    expect(rows.map((r) => r.classes('cc-row-bad'))).toEqual([true, false, true]);
    // Estimates are marked; due dates are not.
    expect(rows[2].find('.tag').exists()).toBe(true);
    expect(rows[0].find('.tag').exists()).toBe(false);
  });

  it('links each row to its record', async () => {
    const w = await mountCalendar(calendar());
    const to = w.findAllComponents(RouterLinkStub).map((l) => l.props('to'));
    expect(to).toEqual(expect.arrayContaining(['/vendor-bills/b1', '/billing/i1', '/forecasting/recurring', '/billing/i0']));
  });

  it('names the low point and the first day under the floor', async () => {
    const w = await mountCalendar(calendar());
    expect(w.find('[data-testid="cc-lowest"]').text()).toContain('-$1,251.00');
    expect(w.find('[data-testid="cc-lowest"]').classes()).toContain('cc-bad');
    const first = w.find('[data-testid="cc-first-below"]');
    expect(first.text()).toContain('First day under $1,000.00');
    expect(first.text()).toMatch(/Oct\s+3/);
  });

  it('says plainly when no floor is set and accounts are the default', async () => {
    const w = await mountCalendar(calendar({ floor: null, accounts: { ...calendar().accounts, chosen: false } }));
    expect(w.find('[data-testid="cc-no-floor"]').text()).toContain('No cash floor is set');
    expect(w.find('[data-testid="cc-default-accounts"]').text()).toContain('Operating');
    expect(w.find('[data-testid="cc-first-below"]').text()).toContain('No floor set');
    expect(w.findAll('[data-testid="cc-row"]').some((r) => r.classes('cc-row-bad'))).toBe(false);
  });

  it('warns when the chosen operating accounts have no synced balance', async () => {
    const acc = { chosen: true, included: [], excluded: [{ id: 'a1', name: 'Operating', balance: null, reason: 'No balance' }] };
    const w = await mountCalendar(calendar({ accounts: acc, starting_balance: 0 }));
    expect(w.find('[data-testid="cc-chosen-no-balance"]').text()).toContain('None of the chosen operating accounts');
    expect(w.find('[data-testid="cc-default-accounts"]').exists()).toBe(false);
  });

  it('offers the settings button to owner/admin only', async () => {
    let w = await mountCalendar(calendar());
    expect(w.find('[data-testid="cc-settings"]').exists()).toBe(true);
    mockRole = 'accounting';
    w = await mountCalendar(calendar({ floor: null }));
    expect(w.find('[data-testid="cc-settings"]').exists()).toBe(false);
    expect(w.find('[data-testid="cc-no-floor"]').text()).toContain('An owner or admin can set one');
  });

  it('lists past-due customer invoices beside the calendar, not in it', async () => {
    const w = await mountCalendar(calendar());
    const box = w.find('[data-testid="cc-customers-past-due"]');
    expect(box.text()).toContain('Customers past due · $300.00');
    expect(box.text()).toContain('Jones · invoice INV-0');
    expect(w.findAll('[data-testid="cc-row"]').some((r) => r.text().includes('INV-0'))).toBe(false);
  });

  it('names bills with no future due date on the floor tile, outside the balance', async () => {
    const data = calendar();
    data.unscheduled.vendor_bills_past_due_or_undated = { count: 28, total: 98177, items: [] };
    data.summary = { ...data.summary, first_below_floor: null };
    const w = await mountCalendar(data);
    const tile = w.find('[data-testid="cc-first-below"]');
    expect(tile.text()).toContain('None among dated items');
    expect(w.find('[data-testid="cc-bills-outside"]').text()).toContain('$98,177.00 in bills with no future due date');
  });

  it('lists finished jobs not yet billed, linked, and outside the balance', async () => {
    const data = calendar();
    data.unscheduled.finished_jobs_not_billed = { count: 1, total: 900, items: [
      { label: 'Smith · Done, unbilled', amount: 900, due_date: '2026-09-21', link: { kind: 'job', id: 'j9' } },
    ] };
    const w = await mountCalendar(data);
    const box = w.find('[data-testid="cc-finished-unbilled"]');
    expect(box.text()).toContain('Finished jobs not billed · $900.00');
    expect(box.findComponent(RouterLinkStub).props('to')).toBe('/jobs/j9');
    expect(w.findAll('[data-testid="cc-row"]').some((r) => r.text().includes('Done, unbilled'))).toBe(false);
  });

  it('shows a finished repair nobody has priced as "not priced yet"', async () => {
    const data = calendar();
    data.unscheduled.finished_jobs_not_billed = { count: 2, total: 359.06, unpriced: 1, items: [
      { label: 'Smith · Spring repair', amount: 359.06, due_date: null, link: { kind: 'job', id: 'j1' } },
      { label: 'Smith · Opener repair', amount: null, due_date: null, link: { kind: 'job', id: 'j2' } },
    ] };
    const w = await mountCalendar(data);
    const box = w.find('[data-testid="cc-finished-unbilled"]').text();
    expect(box).toContain('2 completed jobs not invoiced yet (1 not priced)');
    expect(box).toContain('not priced yet');
    expect(box).toContain('$359.06');
  });

  it('explains jobs left out of money in', async () => {
    const w = await mountCalendar(calendar({ notes: { jobs_without_accepted_estimate: 2, jobs_already_invoiced: 1 } }));
    const note = w.find('[data-testid="cc-jobs-note"]').text();
    expect(note).toContain('2 scheduled jobs have no accepted estimate');
    expect(note).toContain('1 scheduled job is already fully invoiced');
  });

  it('shows an empty state when nothing is dated in the window', async () => {
    const w = await mountCalendar(calendar({ rows: [] }));
    expect(w.find('.cc-empty').text()).toContain('Nothing dated in the next 14 days');
  });
});

describe('CashCalendarSummaryCard', () => {
  it('renders nothing and fetches nothing without accounting.read', async () => {
    mockPerms = new Set();
    const w = mount(CashCalendarSummaryCard, { global: { stubs } });
    await flushPromises();
    expect(apiGet).not.toHaveBeenCalled();
    expect(w.find('[data-testid="cash-calendar-card"]').exists()).toBe(false);
  });

  it('shows the balance, the low point and the first tight day, linking to Forecasting', async () => {
    apiGet.mockResolvedValue(calendar());
    const w = mount(CashCalendarSummaryCard, { global: { stubs } });
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/forecast/cash-calendar?days=14', { suppressErrorToast: true });
    const card = w.find('[data-testid="cash-calendar-card"]');
    expect(card.text()).toContain('Cash · next 14 days');
    expect(card.text()).toContain('$1,000.00');
    expect(card.text()).toContain('-$1,251.00');
    expect(w.find('[data-testid="cash-calendar-card-floor"]').text()).toMatch(/Oct\s+3/);
    expect(w.findComponent(RouterLinkStub).props('to')).toBe('/forecasting');
  });

  it('says what is not in its numbers: undated bills and past-due customers', async () => {
    const data = calendar();
    data.unscheduled.vendor_bills_past_due_or_undated = { count: 28, total: 98177, items: [] };
    data.summary = { ...data.summary, first_below_floor: null };
    apiGet.mockResolvedValue(data);
    const w = mount(CashCalendarSummaryCard, { global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="cash-calendar-card-floor"]').text()).toContain('None among dated items');
    const outside = w.find('[data-testid="cash-calendar-card-outside"]').text();
    expect(outside).toContain('$98,177.00 in bills with no future due date');
    expect(outside).toContain('$300.00 customers owe past due');
    expect(outside).not.toContain('finished jobs');
  });

  it('names finished jobs not billed among what is outside its numbers', async () => {
    const data = calendar();
    data.unscheduled.finished_jobs_not_billed = { count: 2, total: 6301.38, items: [] };
    apiGet.mockResolvedValue(data);
    const w = mount(CashCalendarSummaryCard, { global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="cash-calendar-card-outside"]').text()).toContain('$6,301.38 in finished jobs not billed');
  });

  it('counts finished jobs nobody has priced, even when none has a value', async () => {
    let data = calendar();
    data.unscheduled.finished_jobs_not_billed = { count: 9, total: 7160.44, unpriced: 4, items: [] };
    apiGet.mockResolvedValue(data);
    let w = mount(CashCalendarSummaryCard, { global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="cash-calendar-card-outside"]').text()).toContain('$7,160.44 in finished jobs not billed (+4 not priced)');
    data = calendar();
    data.unscheduled.finished_jobs_not_billed = { count: 3, total: 0, unpriced: 3, items: [] };
    apiGet.mockResolvedValue(data);
    w = mount(CashCalendarSummaryCard, { global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="cash-calendar-card-outside"]').text()).toContain('3 finished jobs not billed, not priced');
  });

  it('says "No tight days" when nothing crosses the floor', async () => {
    const data = calendar();
    data.summary = { ...data.summary, first_below_floor: null, first_below_floor_if_nothing_comes_in: null, lowest_balance: 1200 };
    apiGet.mockResolvedValue(data);
    const w = mount(CashCalendarSummaryCard, { global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="cash-calendar-card-floor"]').text()).toContain('No tight days');
  });
});

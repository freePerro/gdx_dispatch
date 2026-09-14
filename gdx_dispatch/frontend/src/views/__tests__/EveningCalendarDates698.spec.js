/**
 * An evening entry saves TODAY, not tomorrow (#698, sibling sweep of #684).
 *
 * The shape: a `Date` that carries the clock — usually a `new Date()` default —
 * turned into a calendar date through UTC (`toISOString().slice(0, 10)`). In
 * Central time any moment after ~7pm is already tomorrow in UTC. About 20% of
 * prod activity happens after 7pm.
 *
 * Every test runs at 8:30 PM on Sep 10 in Chicago, which is 01:30 on Sep 11
 * UTC, and asserts what the REAL component sends or shows. Each one fails on
 * the pre-#698 code.
 */
process.env.TZ = 'America/Chicago';

import { mount, flushPromises } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: vi.fn() }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, delete: vi.fn(), del: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(async () => true) }),
}));
vi.mock('vue-chartjs', () => ({ Line: { template: '<div />' } }));
vi.mock('chart.js', () => ({
  Chart: { register: () => {} },
  LineElement: {}, PointElement: {}, LinearScale: {}, CategoryScale: {},
  Tooltip: {}, Legend: {}, Filler: {},
}));

import { formatDate } from '../../composables/useFormatters';
import EndRecurringDialog from '../../components/forecasting/EndRecurringDialog.vue';
import OverheadView from '../OverheadView.vue';
import CollectionsView from '../CollectionsView.vue';
import PurchaseOrdersView from '../PurchaseOrdersView.vue';
import MobileTimeclockView from '../MobileTimeclockView.vue';

// 8:30 PM Sep 10 in Chicago == 2026-09-11T01:30:00Z.
const EVENING = new Date(2026, 8, 10, 20, 30);

const passthrough = { template: '<div><slot /><slot name="start" /><slot name="end" /><slot name="footer" /><slot name="content" /></div>' };
const stubs = {
  Toolbar: passthrough, Card: passthrough, Message: passthrough,
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  Button: {
    props: ['label'],
    emits: ['click'],
    inheritAttrs: false,
    template: `<button :data-testid="$attrs['data-testid']" @click="$emit('click', $event)">{{ label }}</button>`,
  },
  DataTable: { props: ['value'], template: '<table><slot name="empty" /></table>' },
  Column: { template: '<i />' },
  Select: { template: '<select />' }, SelectButton: { template: '<div />' },
  DatePicker: { template: '<input />' }, InputText: { template: '<input />' },
  InputNumber: { template: '<input />' }, Textarea: { template: '<textarea />' },
  ToggleSwitch: { template: '<input />' }, Tag: { template: '<span />' },
  ProgressSpinner: { template: '<div />' }, Badge: { template: '<span />' },
  EmptyState: { template: '<div />' },
  FormField: { template: '<div />' },
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(EVENING);
  apiGet.mockReset().mockResolvedValue([]);
  apiPost.mockReset().mockResolvedValue({});
  apiPatch.mockReset().mockResolvedValue({});
});
afterEach(() => {
  vi.useRealTimers();
});

describe('forecasting — End recurring payment', () => {
  it('defaults the effective date to today', async () => {
    const wrapper = mount(EndRecurringDialog, {
      props: { visible: true, stream: { label: 'Truck loan', amount_min: 500, amount_max: 500, cadence: 'monthly' } },
      global: { stubs },
    });
    await wrapper.findAll('button').find((b) => b.text() === 'End stream').trigger('click');

    expect(wrapper.emitted('submit')[0][0]).toEqual({ reason: 'paid_off', ended_at: '2026-09-10' });
  });
});

describe('Overhead — Add obligation', () => {
  beforeEach(() => {
    apiGet.mockImplementation((url) => {
      if (url.includes('/suggestions')) {
        return Promise.resolve({
          count: 1,
          suggestions: [{ stream_id: 's1', label: 'ACME Insurance', suggested_amount: '190.00', cadence: 'monthly' }],
        });
      }
      if (url.includes('projection')) return Promise.resolve({ months: [], step_downs: [] });
      return Promise.resolve({ obligations: [], categories: ['other'], cadences: ['monthly'], cost_types: ['fixed'] });
    });
  });

  it('a new obligation starts today', async () => {
    const wrapper = mount(OverheadView, { global: { stubs } });
    await flushPromises();
    await wrapper.findAll('button').find((b) => b.text() === 'Add obligation').trigger('click');

    expect(wrapper.find('input.date-input').element.value).toBe('2026-09-10');
  });

  it('a suggestion with no detected start saves today', async () => {
    const wrapper = mount(OverheadView, { global: { stubs } });
    await flushPromises();
    await wrapper.findAll('button').find((b) => b.text() === 'Add').trigger('click');
    await wrapper.findAll('button').find((b) => b.text() === 'Save').trigger('click');
    await flushPromises();

    const [url, payload] = apiPost.mock.calls[0];
    expect(url).toBe('/api/overhead');
    expect(payload.start_date).toBe('2026-09-10');
  });
});

describe('Collections — last contact', () => {
  // The server stores last_contact as a timestamptz (payment_reminders.sent_at)
  // and the list renders it with formatDate. A bare "YYYY-MM-DD" is stored as
  // UTC midnight and renders as the day BEFORE here, so the client sends the
  // instant — and the assertion is on what the list will show.
  async function mountCollections() {
    apiGet.mockImplementation((url) => Promise.resolve(url.includes('aging') ? null : []));
    const wrapper = mount(CollectionsView, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    return wrapper;
  }
  // What the list gets back: routers/collections.py runs datetime.fromisoformat
  // on the value (a bare date becomes midnight UTC, an instant stays itself) and
  // returns `sent_at.isoformat()`. JS parses both forms the same way.
  const asStored = (value) => new Date(value).toISOString();

  it('Mark contacted stamps this moment, which the list shows as today', async () => {
    const wrapper = await mountCollections();
    await wrapper.vm.markContacted({ id: 'c1' });

    const [url, payload] = apiPatch.mock.calls[0];
    expect(url).toBe('/api/collections/c1');
    expect(payload).toEqual({ last_contact: '2026-09-11T01:30:00.000Z', status: 'active' });
    expect(formatDate(asStored(payload.last_contact))).toBe('Sep 10, 2026');
  });

  it('a last contact picked in the edit dialog shows as the picked day', async () => {
    const wrapper = await mountCollections();
    wrapper.vm.editingEntry = { id: 'c1' };
    wrapper.vm.form.last_contact = new Date(2026, 7, 1); // the DatePicker's local midnight
    await wrapper.vm.saveEntry();

    const [, payload] = apiPatch.mock.calls[0];
    expect(formatDate(asStored(payload.last_contact))).toBe('Aug 1, 2026');
  });
});

describe('Purchase orders — order date', () => {
  it('a PO created in the evening is ordered today', async () => {
    const wrapper = mount(PurchaseOrdersView, { global: { stubs } });
    await flushPromises();
    await wrapper.get('[data-testid="new-po-btn"]').trigger('click');
    await wrapper.vm.savePo();

    const [url, payload] = apiPost.mock.calls.find(([u]) => u === '/api/purchase-orders');
    expect(url).toBe('/api/purchase-orders');
    expect(payload.order_date).toBe('2026-09-10');
  });

  it('opening and saving an existing PO leaves its order date alone', async () => {
    // The load half moved with the save half: `new Date("2026-09-10")` is UTC
    // midnight, 7 PM on Sep 9 here. It round-tripped only while the save went
    // back through UTC — fixing the save alone would move every edited PO a
    // day earlier.
    const wrapper = mount(PurchaseOrdersView, { global: { stubs } });
    await flushPromises();
    await wrapper.vm.openDetail({
      id: 'po-1', po_number: 'PO-1', status: 'draft', order_date: '2026-08-01', expected_date: '2026-08-05', lines: [],
    });
    await flushPromises();

    const ymd = (d) => [d.getFullYear(), d.getMonth() + 1, d.getDate()];
    expect(ymd(wrapper.vm.form.order_date)).toEqual([2026, 8, 1]);
    expect(ymd(wrapper.vm.form.expected_date)).toEqual([2026, 8, 5]);

    await wrapper.vm.savePo();
    const [url, payload] = apiPatch.mock.calls[0];
    expect(url).toBe('/api/purchase-orders/po-1');
    expect(payload.order_date).toBe('2026-08-01');
    expect(payload.expected_date).toBe('2026-08-05');
  });
});

describe('Mobile time clock — Today', () => {
  const geolocation = { getCurrentPosition: (_ok, err) => err && err({ code: 1 }) };

  it("lists today's shifts on the shop's calendar, morning and evening", async () => {
    Object.defineProperty(global.navigator, 'geolocation', { configurable: true, value: geolocation });
    apiGet.mockImplementation((url) => {
      if (url === '/api/me/timezone') return Promise.resolve({ tenant_timezone: 'America/Chicago' });
      if (url === '/api/timeclock/status') return Promise.resolve({ clocked_in: false });
      if (url === '/api/timeclock/entries') {
        return Promise.resolve([
          // 8:00 AM Sep 10 Central — UTC day Sep 10.
          { id: 'morning', entry_type: 'work', clock_in_at: '2026-09-10T13:00:00+00:00', clock_out_at: '2026-09-10T17:00:00+00:00' },
          // 7:45 PM Sep 10 Central — UTC day is already Sep 11.
          { id: 'evening', entry_type: 'work', clock_in_at: '2026-09-11T00:45:00+00:00', clock_out_at: '2026-09-11T01:15:00+00:00' },
          // 8:00 AM Sep 9 Central — yesterday, must not appear.
          { id: 'yesterday', entry_type: 'work', clock_in_at: '2026-09-09T13:00:00+00:00', clock_out_at: '2026-09-09T17:00:00+00:00' },
        ]);
      }
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileTimeclockView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.vm.todayEntries.map((e) => e.id).sort()).toEqual(['evening', 'morning']);
  });
});

describe('Estimates — valid until', () => {
  // EstimateView is 3050 lines and needs the pricing engine, tax config,
  // catalogs and a route param to mount (see EstimateViewMobileLayout.spec.js),
  // so its two save paths are pinned by absence. The behaviour is walked in a
  // browser in the #698 PR. Its default is now + 30 days WITH the clock.
  const SRC = readFileSync(join(__dirname, '..', 'EstimateView.vue'), 'utf8');

  it('neither the create nor the autosave path slices valid_until through UTC', () => {
    expect(SRC).not.toMatch(/valid_until\.toISOString\(\)/);
  });
});

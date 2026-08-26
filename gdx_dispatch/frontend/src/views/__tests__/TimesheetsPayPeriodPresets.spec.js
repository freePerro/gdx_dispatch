/**
 * Timesheets — the pay-period presets.
 *
 * The failures these pin:
 *
 *  - The buttons computing a fortnight in JavaScript. They must use the
 *    ranges GET /api/timeclock/pay-periods returned, or this page can show
 *    one period while the emailed file covers another.
 *  - `new Date('2026-08-10')` parses as UTC midnight and renders as the 9th
 *    west of Greenwich, so a preset would land a day early for everyone in
 *    Minnesota. The page must build the Date from calendar fields.
 *  - The buttons appearing for a shop with no payroll calendar, where they
 *    could only produce an authoritative-looking wrong range.
 *  - The "Last pay period" banner staying up after the operator moves the
 *    dates by hand — which is how someone exports a range that is not the
 *    period the banner names.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const routeQuery = { value: {} };

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ get query() { return routeQuery.value; } }),
  useRouter: () => ({ replace: vi.fn(() => Promise.resolve()), push: vi.fn() }),
}));

const tenantTz = ref('America/Chicago');
vi.mock('../../composables/useTenantTimezone', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    useTenantTimezone: () => ({
      tenantTimezone: tenantTz,
      ensureLoaded: () => Promise.resolve(tenantTz.value),
      zonedDateKey: (v) => actual.dateKeyInZone(v, tenantTz.value),
    }),
  };
});

import TimesheetsView from '../TimesheetsView.vue';

const stubs = {
  Avatar: { props: ['label'], template: '<span />' },
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Card: { template: '<div class="timecard"><slot name="title" /><slot name="content" /></div>' },
  Column: { props: ['header'], template: '<div />' },
  DataTable: { props: ['value'], template: '<div class="dt" :data-rows="value.length" />' },
  DatePicker: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input />' },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
  InputText: { props: ['value'], template: '<input />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  ProgressSpinner: { template: '<div />' },
  Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<select />' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
};

const CALENDAR = {
  configured: true,
  cadence: 'biweekly',
  cadence_label: 'Every two weeks',
  timezone: 'America/Chicago',
  today: '2026-08-26',
  current: { start: '2026-08-24', end: '2026-09-06', label: '2026-08-24 – 2026-09-06', pay_date: '2026-09-11' },
  previous: { start: '2026-08-10', end: '2026-08-23', label: '2026-08-10 – 2026-08-23', pay_date: '2026-08-28' },
  next: { start: '2026-09-07', end: '2026-09-20', label: '2026-09-07 – 2026-09-20', pay_date: '2026-09-25' },
};

/** Every /entries call the page made, as parsed query objects. */
function entryCalls() {
  return apiGet.mock.calls
    .map(([url]) => url)
    .filter((url) => String(url).startsWith('/api/timeclock/entries'))
    .map((url) => Object.fromEntries(new URLSearchParams(String(url).split('?')[1] || '')));
}

function mockApi({ calendar = CALENDAR } = {}) {
  apiGet.mockImplementation((url) => {
    if (String(url).startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: 'America/Chicago' });
    if (String(url).startsWith('/api/timeclock/pay-periods')) {
      return calendar ? Promise.resolve(calendar) : Promise.reject(new Error('no calendar'));
    }
    if (String(url).startsWith('/api/timeclock/roster')) return Promise.resolve([]);
    if (String(url).startsWith('/api/timeclock/entries')) return Promise.resolve([]);
    return Promise.resolve([]);
  });
}

async function mountPage() {
  const wrapper = mount(TimesheetsView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiPatch.mockReset();
  routeQuery.value = {};
  mockApi();
});

describe('Timesheets — pay-period presets', () => {
  it('offers the two pay-period shortcuts', async () => {
    const wrapper = await mountPage();
    expect(wrapper.find('[data-testid="timesheets-this-pay-period"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="timesheets-last-pay-period"]').exists()).toBe(true);
  });

  it('opens on the current pay period rather than making you know to click', async () => {
    await mountPage();
    const [first] = entryCalls();
    // A day of slack on each end is deliberate — the server buckets by UTC day.
    expect(first.date_start).toBe('2026-08-23');
    expect(first.date_end).toBe('2026-09-07');
  });

  it('loads the exact range the SERVER gave for last pay period', async () => {
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-last-pay-period"]').trigger('click');
    await flushPromises();

    const last = entryCalls().at(-1);
    expect(last.date_start).toBe('2026-08-09');  // 2026-08-10 minus a day of slack
    expect(last.date_end).toBe('2026-08-24');    // 2026-08-23 plus a day of slack
  });

  it('names the period on screen, with the day it gets paid', async () => {
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-last-pay-period"]').trigger('click');
    await flushPromises();

    const note = wrapper.find('[data-testid="timesheets-period-note"]');
    expect(note.text()).toContain('Last pay period');
    expect(note.text()).toContain('2026-08-10 – 2026-08-23');
    expect(note.text()).toContain('2026-08-28');
  });

  it('drops the period banner once the range is no longer that period', async () => {
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-last-pay-period"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-testid="timesheets-period-note"]').exists()).toBe(true);

    await wrapper.find('[data-testid="timesheets-this-week"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-testid="timesheets-period-note"]').exists()).toBe(false);
  });

  it('hides the shortcuts entirely when no payroll calendar is configured', async () => {
    mockApi({ calendar: { configured: false, message: 'needs a start date', current: null, previous: null, next: null } });
    const wrapper = await mountPage();
    expect(wrapper.find('[data-testid="timesheets-this-pay-period"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="timesheets-last-pay-period"]').exists()).toBe(false);
    // and the week shortcuts still work
    expect(wrapper.find('[data-testid="timesheets-this-week"]').exists()).toBe(true);
  });

  it('still loads a week when the calendar endpoint fails outright', async () => {
    mockApi({ calendar: null });
    const wrapper = await mountPage();
    expect(wrapper.find('[data-testid="timesheets-this-pay-period"]').exists()).toBe(false);
    expect(entryCalls().length).toBeGreaterThan(0);
    expect(wrapper.find('[data-testid="timesheets-empty"]').exists()).toBe(true);
  });

  it('a deep link from Dispatch still wins over the pay-period default', async () => {
    routeQuery.value = { on: '2026-05-11', entry: 'e-1' };
    await mountPage();
    const [first] = entryCalls();
    expect(first.date_start.startsWith('2026-05')).toBe(true);
  });
});

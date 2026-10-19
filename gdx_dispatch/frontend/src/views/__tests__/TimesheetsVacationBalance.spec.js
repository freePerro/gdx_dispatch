/**
 * TimesheetsView — the Vacation balances card (GDXA-701).
 *
 * Pins, named for the failure each prevents:
 *  - the card asks for EVERYONE for the year the selected range ends in;
 *  - each row names that person's own vacation year, and the title names no
 *    year: under hire_anniversary the windows differ person to person
 *    (GDXA-1902);
 *  - "no allowance set" is said in words, never as 0h left;
 *  - an overdrawn balance shows negative and in danger, not clamped to 0;
 *  - a failed read says so; it never renders as an empty crew;
 *  - a 403 (a role granted scheduling.write but not dispatch/admin) hides the
 *    card instead of reading as a fault to try again (GDXA-761);
 *  - "still ahead" reads as part of "used", not added to it (GDXA-761).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
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
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text', 'size'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Card: { template: '<div class="timecard"><slot name="title" /><slot name="content" /></div>' },
  Column: { props: ['header'], template: '<div />' },
  DataTable: { props: ['value'], template: '<div class="dt" :data-rows="value.length" />' },
  DatePicker: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input />' },
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  InputText: { props: ['value'], template: '<input />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  ProgressSpinner: { template: '<div />' },
  Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<select />' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  HolidayPostDialog: { props: ['visible', 'holiday'], template: '<div class="hpd" />' },
};

const ROSTER = [{ technician_id: 'u-a', name: 'Dana Ruiz', active: true, has_entries: true }];
const BALANCES = {
  year: 2026, as_of: '2026-05-20', timezone: 'America/Chicago', shop_allowance_hours: 80,
  balances: [
    { technician_id: 'u-a', technician_name: 'Dana Ruiz', year: 2026, allowance_hours: 80, allowance_source: 'shop',
      used_hours: 24, taken_hours: 16, scheduled_hours: 8, remaining_hours: 56, overdrawn: false,
      window_start: '2026-01-01', window_end: '2026-12-31' },
    { technician_id: 'u-b', technician_name: 'Lee Park', year: 2026, allowance_hours: 16, allowance_source: 'person',
      used_hours: 24.5, taken_hours: 24.5, scheduled_hours: 0, remaining_hours: -8.5, overdrawn: true,
      window_start: '2026-01-01', window_end: '2026-12-31' },
    { technician_id: 'u-c', technician_name: 'Sam Ode', year: 2026, allowance_hours: null, allowance_source: null,
      used_hours: 0, taken_hours: 0, scheduled_hours: 0, remaining_hours: null, overdrawn: false,
      window_start: '2026-01-01', window_end: '2026-12-31' },
  ],
};

function mockApi({ balances = BALANCES, fail = false, status = 503 } = {}) {
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: 'America/Chicago' });
    if (url.startsWith('/api/timeclock/roster')) return Promise.resolve(ROSTER);
    if (url.startsWith('/api/timeclock/time-off/balances')) {
      return fail
        ? Promise.reject(Object.assign(new Error(String(status)), { status }))
        : Promise.resolve(balances);
    }
    if (url.startsWith('/api/timeclock/time-off/options')) return Promise.resolve({});
    return Promise.resolve([]);
  });
}

async function mountRange(opts, end = new Date(2026, 4, 31)) {
  mockApi(opts);
  const w = mount(TimesheetsView, { global: { stubs, directives: { tooltip: {} } } });
  await flushPromises();
  w.vm.startDate = new Date(end.getFullYear(), end.getMonth(), 1);
  w.vm.endDate = end;
  await w.vm.load();
  await flushPromises();
  return w;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  toastAdd.mockReset();
});

describe('TimesheetsView — vacation balances', () => {
  it('asks for everyone for the range-end year and shows used, ahead and left', async () => {
    const w = await mountRange();
    const urls = apiGet.mock.calls.map(([u]) => u).filter((u) => u.startsWith('/api/timeclock/time-off/balances'));
    expect(urls.at(-1)).toBe('/api/timeclock/time-off/balances?all_technicians=true&year=2026');
    const card = w.find('[data-testid="vacation-balances"]');
    expect(card.text()).toContain('Vacation balances');
    expect(w.find('[data-testid="vacation-window-u-a"]').text()).toBe('2026 ·');
    expect(card.text()).toContain('as of 2026-05-20');
    const dana = w.find('[data-testid="vacation-balance-u-a"]').text();
    expect(dana).toContain('24h used');
    expect(dana).toContain('(8h of it still ahead)');
    expect(dana).toContain('of 80h');
    expect(w.find('[data-testid="vacation-remaining-u-a"]').text()).toBe('56h left');
  });

  it('names each person\'s anniversary window on their row, not one year for all', async () => {
    const anniversary = {
      ...BALANCES,
      balances: [
        { ...BALANCES.balances[0], year: 2025, window_start: '2025-06-03', window_end: '2026-06-02' },
        { ...BALANCES.balances[1], year: 2026, window_start: '2026-03-01', window_end: '2027-02-28' },
        // No hire date: the calendar year.
        BALANCES.balances[2],
      ],
    };
    const w = await mountRange({ balances: anniversary });
    expect(w.find('[data-testid="vacation-window-u-a"]').text()).toBe('Jun 3 2025 – Jun 2 2026 ·');
    expect(w.find('[data-testid="vacation-window-u-b"]').text()).toBe('Mar 1 2026 – Feb 28 2027 ·');
    expect(w.find('[data-testid="vacation-window-u-c"]').text()).toBe('2026 ·');
    expect(w.find('[data-testid="vacation-balances"]').text()).not.toMatch(/Vacation 20\d\d/);
  });

  it('shows an overdrawn balance as negative, and an unset one in words', async () => {
    const w = await mountRange();
    expect(w.find('[data-testid="vacation-remaining-u-b"]').text()).toBe('-8.50h left');
    expect(w.find('[data-testid="vacation-balance-u-b"]').text()).toContain('(own)');
    const sam = w.find('[data-testid="vacation-balance-u-c"]');
    expect(sam.find('[data-testid="vacation-no-allowance"]').exists()).toBe(true);
    expect(sam.text()).not.toContain('left');
    expect(w.find('[data-testid="vacation-shop-unset"]').exists()).toBe(false);
  });

  it('points the office at Settings when no shop allowance is set', async () => {
    const w = await mountRange({ balances: { ...BALANCES, shop_allowance_hours: null } });
    expect(w.find('[data-testid="vacation-shop-unset"]').text()).toContain('Settings');
  });

  it('follows the range into another year', async () => {
    await mountRange({}, new Date(2025, 11, 31));
    const urls = apiGet.mock.calls.map(([u]) => u).filter((u) => u.startsWith('/api/timeclock/time-off/balances'));
    expect(urls.at(-1)).toContain('year=2025');
  });

  it('says the read failed instead of showing nobody', async () => {
    const w = await mountRange({ fail: true });
    expect(w.find('[data-testid="vacation-balances-error"]').exists()).toBe(true);
    expect(w.find('[data-testid="vacation-balance-u-a"]').exists()).toBe(false);
  });

  it('hides the card on a 403 rather than saying the read failed', async () => {
    const w = await mountRange({ fail: true, status: 403 });
    expect(w.find('[data-testid="vacation-balances"]').exists()).toBe(false);
    expect(w.find('[data-testid="vacation-balances-error"]').exists()).toBe(false);
  });
});

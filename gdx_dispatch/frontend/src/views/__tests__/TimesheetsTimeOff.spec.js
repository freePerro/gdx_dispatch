/**
 * TimesheetsView — the office's side of time off.
 *
 * Pins, named for the failure each prevents:
 *  - a vacation row is summed as TIME OFF, never as worked hours — folding
 *    it into "Worked" is how overtime gets applied to a day nobody worked
 *    (same rule as Shift.worked_minutes in core/timesheet_hours.py);
 *  - pending requests render with Approve/Deny; approving calls the route
 *    and RELOADS the entries (the approval wrote timeclock rows);
 *  - deny carries the note; revoke needs a second click;
 *  - a holiday in range with nobody paid yet is surfaced with a Post button;
 *  - the shop's overtime statement is shown beside the time-off total.
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

const ROSTER = [{ technician_id: 'u-tech-a', name: 'Dana Ruiz', active: true, has_entries: true }];
const ENTRIES = [
  // Worked 8h with a 30m lunch on Mon May 4 2026 (13:00Z = 8am Chicago).
  { id: 'w1', technician_id: 'u-tech-a', entry_type: 'clock', minutes: 480, break_minutes: 30,
    clock_in_at: '2026-05-04T13:00:00+00:00', clock_out_at: '2026-05-04T21:00:00+00:00', notes: null },
  // Vacation Tue May 5: a synthetic 8h span.
  { id: 'v1', technician_id: 'u-tech-a', entry_type: 'vacation', minutes: 480, break_minutes: null,
    clock_in_at: '2026-05-05T13:00:00+00:00', clock_out_at: '2026-05-05T21:00:00+00:00', notes: 'beach' },
];
const PENDING = {
  id: 'r1', technician_id: 'u-tech-a', technician_name: 'Dana Ruiz', entry_type: 'vacation',
  entry_type_label: 'Vacation', start_date: '2026-05-11', end_date: '2026-05-12', minutes_per_day: 480,
  workday_count: 2, notes: 'trip', status: 'pending', review_note: null,
};
const APPROVED = { ...PENDING, id: 'r2', status: 'approved', entry_ids: ['x'] };

function mockApi({ requests = [], holidays = [], options = { time_off_counts_toward_overtime: false } } = {}) {
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: 'America/Chicago' });
    if (url.startsWith('/api/timeclock/roster')) return Promise.resolve(ROSTER);
    if (url.startsWith('/api/timeclock/entries')) return Promise.resolve(ENTRIES);
    if (url.startsWith('/api/timeclock/time-off/requests')) return Promise.resolve(requests);
    if (url.startsWith('/api/timeclock/time-off/holidays')) return Promise.resolve(holidays);
    if (url.startsWith('/api/timeclock/time-off/options')) return Promise.resolve(options);
    return Promise.resolve([]);
  });
}

async function mountMay(opts) {
  mockApi(opts);
  const w = mount(TimesheetsView, { global: { stubs, directives: { tooltip: {} } } });
  await flushPromises();
  w.vm.startDate = new Date(2026, 4, 1);
  w.vm.endDate = new Date(2026, 4, 31);
  await w.vm.load();
  await flushPromises();
  return w;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  toastAdd.mockReset();
});

describe('TimesheetsView — time off', () => {
  it('sums a vacation row as time off, apart from worked hours', async () => {
    const w = await mountMay();
    const card = w.vm.timecards[0];
    expect(card.hours).toBeCloseTo(7.5, 5);          // 480 − 30, the clock row only
    expect(card.timeOffHours).toBeCloseTo(8, 5);     // the vacation row
    expect(w.find('[data-testid="timecard-total-u-tech-a"]').text()).toContain('7.50h');
    expect(w.find('[data-testid="timecard-time-off-u-tech-a"]').text()).toContain('8.00h off');
    expect(w.find('[data-testid="timesheets-time-off-tile"]').text()).toContain('8.00h');
    expect(w.find('[data-testid="timesheets-ot-statement"]').text()).toContain('not counted toward overtime');
  });

  it('states the shop’s setting when time off counts toward overtime', async () => {
    const w = await mountMay({ options: { time_off_counts_toward_overtime: true } });
    expect(w.find('[data-testid="timesheets-ot-statement"]').text()).toContain('counts toward overtime');
  });

  it('reads the crew’s requests, shows pending ones, and approving reloads the sheet', async () => {
    apiPost.mockResolvedValue({ status: 'approved', created: 2, skipped_days: [] });
    const w = await mountMay({ requests: [PENDING, APPROVED] });

    const reqUrl = apiGet.mock.calls.find(([u]) => u.startsWith('/api/timeclock/time-off/requests'))[0];
    expect(reqUrl).toContain('all_technicians=true');
    expect(w.find('[data-testid="time-off-pending-count"]').text()).toBe('1 pending');
    expect(w.find('[data-testid="time-off-request-r1"]').text()).toContain('Dana Ruiz');
    expect(w.find('[data-testid="time-off-request-r1"]').text()).toContain('2 workdays × 8h');
    // Decided ones are hidden until asked for.
    expect(w.find('[data-testid="time-off-request-r2"]').exists()).toBe(false);
    await w.find('[data-testid="time-off-toggle-decided"]').trigger('click');
    expect(w.find('[data-testid="time-off-request-r2"]').exists()).toBe(true);

    const entriesBefore = apiGet.mock.calls.filter(([u]) => u.startsWith('/api/timeclock/entries')).length;
    await w.find('[data-testid="time-off-approve-r1"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith('/api/timeclock/time-off/requests/r1/approve', {}, expect.anything());
    const entriesAfter = apiGet.mock.calls.filter(([u]) => u.startsWith('/api/timeclock/entries')).length;
    expect(entriesAfter).toBe(entriesBefore + 1);
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }));
  });

  it('deny carries the note; revoke needs a second click', async () => {
    apiPost.mockResolvedValue({ status: 'denied' });
    const w = await mountMay({ requests: [PENDING, APPROVED] });

    await w.find('[data-testid="time-off-deny-r1"]').trigger('click');
    await w.find('[data-testid="time-off-deny-note-r1"]').setValue('short-staffed');
    await w.find('[data-testid="time-off-deny-confirm-r1"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith(
      '/api/timeclock/time-off/requests/r1/deny', { note: 'short-staffed' }, expect.anything(),
    );

    apiPost.mockClear();
    apiPost.mockResolvedValue({ status: 'revoked', removed: 1 });
    await w.find('[data-testid="time-off-toggle-decided"]').trigger('click');
    const revoke = () => w.find('[data-testid="time-off-revoke-r2"]');
    await revoke().trigger('click');
    expect(apiPost).not.toHaveBeenCalled();
    expect(revoke().text()).toBe('Confirm revoke');
    await revoke().trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith('/api/timeclock/time-off/requests/r2/revoke', {}, expect.anything());
  });

  it('a send held on an unposted holiday offers Post right in the refusal', async () => {
    const w = await mountMay();
    apiPost.mockRejectedValue({
      status: 409,
      body: { detail: {
        blocked: 'unposted_holiday',
        detail: 'Memorial Day (2026-05-25) is on the holiday calendar and nobody has holiday pay for it yet.',
        flagged: [],
        holidays: [{ date: '2026-05-25', name: 'Memorial Day', minutes: 480 }],
      } },
    });
    w.vm.openSend();
    await w.vm.doSend();
    await flushPromises();
    expect(w.find('[data-testid="send-blocked"]').text()).toContain('Memorial Day');
    await w.find('[data-testid="send-post-holiday-2026-05-25"]').trigger('click');
    expect(w.vm.sendDialog).toBe(false);
    expect(w.vm.holidayPostVisible).toBe(true);
    expect(w.vm.holidayToPost).toEqual({ date: '2026-05-25', name: 'Memorial Day', minutes: 480 });
  });

  it('surfaces an unposted holiday in range with a Post button, and hides a posted one', async () => {
    const w = await mountMay({ holidays: [
      { date: '2026-05-25', name: 'Memorial Day', minutes: 480, posted: 0 },
      { date: '2026-05-04', name: 'Shop Day', minutes: 480, posted: 3 },
    ] });
    expect(w.find('[data-testid="holiday-unposted-2026-05-25"]').text()).toContain('Memorial Day');
    expect(w.find('[data-testid="holiday-unposted-2026-05-04"]').exists()).toBe(false);
    await w.find('[data-testid="holiday-post-2026-05-25"]').trigger('click');
    expect(w.vm.holidayPostVisible).toBe(true);
    expect(w.vm.holidayToPost.date).toBe('2026-05-25');
  });
});

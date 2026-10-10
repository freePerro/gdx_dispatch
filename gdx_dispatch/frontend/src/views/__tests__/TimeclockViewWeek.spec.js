/**
 * TimeclockView — the "My Timesheet" card (desktop tech self-service).
 *
 * The week machinery itself is pinned in useWeeklyTimesheet.spec.js; this
 * file pins the WIRING the card adds:
 *  - the week fetch is separate from Today's Entries (navigating weeks must
 *    not blank the today list);
 *  - an entry's pencil opens the shared TimeEntryDialog in selfMode with
 *    THAT entry;
 *  - "Add missed shift" opens it in create mode;
 *  - the week total renders break-netted hours.
 *
 * Timezone is left null here on purpose: bucketing across zones is the
 * composable spec's job, and null keeps "today" deterministic in any runner.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(() => Promise.resolve(true)) }),
}));

const tenantTz = ref(null);
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

import TimeclockView from '../TimeclockView.vue';
import TimeEntryDialog from '../../components/TimeEntryDialog.vue';

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text', 'rounded', 'size', 'outlined'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Card: { template: '<div class="card"><slot name="title" /><slot name="content" /></div>' },
  Column: { props: ['header'], template: '<div />' },
  DataTable: { props: ['value'], template: '<div class="dt" />' },
  DatePicker: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input />' },
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  Divider: { template: '<hr />' },
  InputText: { props: ['value'], template: '<input />' },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  ProgressSpinner: { template: '<div />' },
  Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<select />' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Textarea: { props: ['modelValue'], emits: ['update:modelValue'], template: '<textarea />' },
  Toast: { template: '<div />' },
};

// Today at fixed local hours — with tz null the composable buckets
// browser-local, so these land on today's row in any runner zone.
function todayAt(h) {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate(), h).toISOString();
}

const WEEK_FIXTURE = [
  {
    id: 'e1', technician_id: 'me',
    clock_in_at: todayAt(8), clock_out_at: todayAt(16),
    minutes: 480, break_minutes: 30, entry_type: 'clock', notes: null,
  },
];

function mockApi() {
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/timeclock/status')) return Promise.resolve({ clocked_in: false });
    if (url.startsWith('/api/timeclock/entries')) {
      // The WEEK fetch carries include_breaks; the today-list fetch is bare.
      return Promise.resolve(url.includes('include_breaks=true') ? WEEK_FIXTURE : []);
    }
    if (url.startsWith('/api/me/tech-mobile-settings')) {
      return Promise.resolve({ settings: {}, tenant_timezone: null });
    }
    if (url.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: null });
    return Promise.resolve([]);
  });
}

async function mountView() {
  const w = mount(TimeclockView, {
    global: { stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return w;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiPatch.mockReset();
  tenantTz.value = null;
  mockApi();
});

describe('TimeclockView — My Timesheet card', () => {
  it('renders the week with a break-netted total', async () => {
    const w = await mountView();
    expect(w.find('[data-testid="my-timesheet"]').exists()).toBe(true);
    expect(w.find('[data-testid="week-label"]').text()).not.toBe('');
    // 480 gross − 30 break = 7.50h
    expect(w.find('[data-testid="week-total"]').text()).toBe('7.50h');
    expect(w.findAll('[data-testid="ts-entry"]')).toHaveLength(1);
  });

  it('fetches the week separately from the today list', async () => {
    await mountView();
    const entryCalls = apiGet.mock.calls
      .map((c) => c[0])
      .filter((u) => u.startsWith('/api/timeclock/entries'));
    expect(entryCalls.some((u) => u.includes('include_breaks=true'))).toBe(true);
    expect(entryCalls.some((u) => !u.includes('include_breaks'))).toBe(true);
  });

  it('cannot browse into a future week', async () => {
    const w = await mountView();
    expect(w.find('[data-testid="week-next"]').attributes('disabled')).toBeDefined();
  });

  it("opens the shared dialog in selfMode on the entry's pencil", async () => {
    const w = await mountView();
    await w.find('[data-testid="ts-edit-e1"]').trigger('click');
    await flushPromises();
    const dialog = w.findComponent(TimeEntryDialog);
    expect(dialog.props('selfMode')).toBe(true);
    expect(dialog.props('entry').id).toBe('e1');
    expect(w.find('[data-testid="entry-dialog"]').exists()).toBe(true);
  });

  it('opens the dialog in create mode from "Add missed shift"', async () => {
    const w = await mountView();
    await w.find('[data-testid="ts-add-entry"]').trigger('click');
    await flushPromises();
    const dialog = w.findComponent(TimeEntryDialog);
    expect(dialog.props('entry')).toBeNull();
    expect(dialog.props('selfMode')).toBe(true);
    // Anchored on YESTERDAY: a today default would trip the server's
    // no-future-hours rule any time before 4pm.
    const anchor = dialog.props('anchorDate');
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    expect(anchor.getDate()).toBe(yesterday.getDate());
  });

  it('survives today-entries landing before /status resolves', async () => {
    // The browser walk caught this: the EOD block renders whenever today has
    // entries, but read todayTotalHours (null until /status returns) with a
    // bare .toFixed — so the entries fetch winning the race crashed the whole
    // page. /status here never resolves; the page must still render.
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/timeclock/status')) return new Promise(() => {});
      if (url.startsWith('/api/timeclock/entries')) {
        return Promise.resolve(url.includes('include_breaks=true') ? WEEK_FIXTURE : WEEK_FIXTURE);
      }
      if (url.startsWith('/api/me/tech-mobile-settings')) return Promise.resolve({ settings: {}, tenant_timezone: null });
      if (url.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: null });
      return Promise.resolve([]);
    });
    const w = await mountView();
    expect(w.find('[data-testid="my-timesheet"]').exists()).toBe(true);
    // The EOD summary degrades to 0.00 instead of crashing the render.
    expect(w.text()).toContain('0.00h');
  });

  it('reloads week, status and today after a save', async () => {
    const w = await mountView();
    await w.find('[data-testid="ts-edit-e1"]').trigger('click');
    await flushPromises();
    apiGet.mockClear();
    w.findComponent(TimeEntryDialog).vm.$emit('saved');
    await flushPromises();
    const urls = apiGet.mock.calls.map((c) => c[0]);
    expect(urls.some((u) => u.includes('include_breaks=true'))).toBe(true);
    expect(urls.some((u) => u.startsWith('/api/timeclock/status'))).toBe(true);
  });
});

describe('TimeclockView — Today is the shop day (GDXA-421)', () => {
  // 21:00 Central on Oct 9 is 02:00 UTC on Oct 10. The list compared the
  // browser date with the stamp's UTC prefix and submit-day sent the UTC
  // day, so Today was wrong and the submit attested Oct 10. The fixture is
  // wrong for the old filter in ANY runner zone: in UTC it kept nothing (the
  // browser said Oct 10), in Central it kept both entries (both UTC-dated
  // Oct 9). CI runs in UTC, so a fixture wrong only west of UTC guards nothing.
  const NOW = new Date('2026-10-10T02:00:00Z');
  // 18:30-19:45 Central on Oct 9: UTC-dated Oct 9, browser-UTC day Oct 10.
  const EVENING = {
    id: 'ev', technician_id: 'me',
    clock_in_at: '2026-10-09T23:30:00+00:00', clock_out_at: '2026-10-10T00:45:00+00:00',
    minutes: 75, entry_type: 'clock', notes: null,
  };
  // 22:00 Central on Oct 8 — UTC-dated Oct 9, so a UTC key would pull it in.
  const YESTERDAY_EVENING = {
    id: 'yd', technician_id: 'me',
    clock_in_at: '2026-10-09T03:00:00+00:00', clock_out_at: '2026-10-09T04:00:00+00:00',
    minutes: 60, entry_type: 'clock', notes: null,
  };

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    tenantTz.value = 'America/Chicago';
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/timeclock/status')) return Promise.resolve({ clocked_in: false, today_hours: 1.25 });
      if (url.startsWith('/api/timeclock/entries')) return Promise.resolve([EVENING, YESTERDAY_EVENING]);
      if (url.startsWith('/api/me/tech-mobile-settings')) {
        return Promise.resolve({ settings: {}, tenant_timezone: 'America/Chicago' });
      }
      if (url.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: 'America/Chicago' });
      return Promise.resolve([]);
    });
    apiPost.mockResolvedValue({ submitted: true, date: '2026-10-09', entries: 1, total_minutes: 75 });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('lists the evening entry (UTC-dated tomorrow by the browser) under today and submits the shop day', async () => {
    const w = await mountView();
    const eod = w.find('.eod-review');
    expect(eod.exists()).toBe(true);
    // Only the evening entry is today's; yesterday's is not, though its UTC
    // date is today's shop date. Asserted by id, not by count.
    expect(w.vm.todayEntries.map((e) => e.id)).toEqual(['ev']);
    expect(eod.text()).toContain('Entries1');
    await w.find('[data-testid="submit-day-btn"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith(
      '/api/timeclock/submit-day', { date: '2026-10-09' }, expect.anything(),
    );
  });
});

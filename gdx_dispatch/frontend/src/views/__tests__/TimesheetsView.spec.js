/**
 * TimesheetsView — the office's read + correct surface for the crew's clock.
 *
 * Pins the contract that makes it usable, all of which was verified live in a
 * browser on 2026-08-10 against real data before being written down here:
 *
 *  - Reads the CREW (all_technicians=true), not just the caller. Without this
 *    the page can only ever show one person — which is what /timeclock does,
 *    and is why Dispatch's "Fix" button was a dead end.
 *  - Groups entries per person with per-person totals, and excludes breaks from
 *    worked hours.
 *  - Flags exactly the three kinds routers/timeclock.py `/exceptions` reports,
 *    so a row flagged here is a row Dispatch is nagging about.
 *  - Resolves names from /api/timeclock/roster, NOT /api/users — a departed
 *    (soft-deleted) employee's hours must still carry their name. This rendered
 *    as "Unknown (<user-id>…)" in the browser before the roster endpoint existed.
 *  - Consumes Dispatch's ?entry= deep link by opening THAT entry's correction
 *    dialog, and says so plainly when the entry isn't in the loaded range.
 *  - Reads, writes and BUCKETS in shop time (Settings → Time Clock → Tenant
 *    timezone), never the browser's. Verified against prod-copy data: 2 of 39
 *    real rows are late-evening Central shifts whose UTC calendar day is the
 *    next day, so a UTC-day bucket files them under a day nobody worked.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const routerReplace = vi.fn(() => Promise.resolve());
const routeQuery = { value: {} };

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ get query() { return routeQuery.value; } }),
  useRouter: () => ({ replace: routerReplace, push: vi.fn() }),
}));

// The real useTenantTimezone caches the zone in a module-level singleton, which
// would leak between tests. Stub the hook only — `dateKeyInZone` stays real, so
// the shop-day bucketing below exercises the actual Intl conversion.
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
  DatePicker: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input />',
  },
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  InputText: { props: ['value'], template: '<input />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  ProgressSpinner: { template: '<div />' },
  Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<select />' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
};

const ROSTER = [
  { technician_id: 'u-tech-a', name: 'Dana Ruiz', active: true, has_entries: true },
  { technician_id: 'u-former', name: 'Sam Okafor', active: false, has_entries: true },
];

function mockApi(entries, roster = ROSTER, tz = 'America/Chicago') {
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: tz });
    if (url.startsWith('/api/timeclock/roster')) return Promise.resolve(roster);
    if (url.startsWith('/api/timeclock/entries')) return Promise.resolve(entries);
    return Promise.resolve([]);
  });
}

// The fixtures below are May 2026 shifts, but the page defaults to the CURRENT
// week — and since entriesInRange applies the real shop-day boundary, an
// unset range would filter every fixture out. Tests that rely on fixture data
// pass MAY so the range covers it; deep-link tests omit it, because the `on`
// query is what sets their range and that is the behavior under test.
const MAY = [new Date(2026, 4, 1), new Date(2026, 4, 31)];

async function mountView(range) {
  const wrapper = mount(TimesheetsView, {
    global: { stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  if (range) {
    wrapper.vm.startDate = range[0];
    wrapper.vm.endDate = range[1];
    await flushPromises();
  }
  return wrapper;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiPatch.mockReset();
  routerReplace.mockReset();
  routeQuery.value = {};
  tenantTz.value = 'America/Chicago';
});

describe('TimesheetsView — shop time, not browser time', () => {
  // The shop is US-Central; these tests must hold whatever zone CI runs in.
  // 2026-05-03T03:02:49Z is 10:02 PM on May 2 in Chicago — a real prod-copy row.
  const LATE_SHIFT = {
    id: 'late', technician_id: 'u-tech-a',
    clock_in_at: '2026-05-03T03:02:49Z', clock_out_at: '2026-05-03T05:02:49Z',
    minutes: 120, entry_type: 'clock', notes: null,
  };

  it('files a late-evening shift under the shop day it was worked', async () => {
    mockApi([LATE_SHIFT]);
    const w = await mountView();
    // Range is the week containing May 2 (Mon Apr 27 – Sun May 3), so it is in
    // range either way; what is pinned here is the DAY it renders as.
    w.vm.startDate = new Date(2026, 3, 27);
    w.vm.endDate = new Date(2026, 4, 3);
    await flushPromises();
    expect(w.vm.timecards[0].entries).toHaveLength(1);
    expect(w.vm.formatDay(LATE_SHIFT.clock_in_at)).toContain('May 2');
  });

  it('keeps that shift inside a range ending on its shop day', async () => {
    // The server buckets it as May 3 (UTC), so a naive client would drop it
    // from a range ending May 2. This is the 2-of-39 case found on real data.
    mockApi([LATE_SHIFT]);
    const w = await mountView();
    w.vm.startDate = new Date(2026, 3, 27);
    w.vm.endDate = new Date(2026, 4, 2);
    await flushPromises();
    expect(w.vm.entriesInRange).toHaveLength(1);
  });

  it('excludes it from a range that ends the shop day before', async () => {
    mockApi([LATE_SHIFT]);
    const w = await mountView();
    w.vm.startDate = new Date(2026, 3, 27);
    w.vm.endDate = new Date(2026, 4, 1);
    await flushPromises();
    expect(w.vm.entriesInRange).toHaveLength(0);
  });

  it('asks the server for a day of slack on each end', async () => {
    mockApi([]);
    const w = await mountView();
    apiGet.mockClear();
    w.vm.startDate = new Date(2026, 4, 4);
    w.vm.endDate = new Date(2026, 4, 10);
    await w.vm.load();
    const call = apiGet.mock.calls.find((c) => c[0].startsWith('/api/timeclock/entries'));
    expect(call[0]).toContain('date_start=2026-05-03');
    expect(call[0]).toContain('date_end=2026-05-11');
  });

  it('round-trips a shop wall clock through the picker without drift', async () => {
    mockApi([LATE_SHIFT]);
    const w = await mountView();
    w.vm.openEdit(LATE_SHIFT);
    // The picker shows 10:02 PM shop time...
    expect(w.vm.editing.clockIn.getHours()).toBe(22);
    expect(w.vm.editing.clockIn.getDate()).toBe(2);
    // ...and saving it back yields the exact instant we started from.
    expect(w.vm.shopWallClockToIso(w.vm.editing.clockIn))
      .toBe('2026-05-03T03:02:00.000Z');
  });

  it('books a typed time as shop time, not the browser\'s zone', async () => {
    mockApi([LATE_SHIFT]);
    apiPatch.mockResolvedValue({});
    const w = await mountView();
    w.vm.openEdit(LATE_SHIFT);
    // Office types "5:00 pm" — that must mean 5pm in the SHOP.
    w.vm.editing.clockOut = new Date(2026, 4, 2, 17, 0, 0, 0);
    await w.vm.save();
    const [, body] = apiPatch.mock.calls[0];
    // 5:00 pm CDT (UTC-5) === 22:00Z.
    expect(body.clock_out_at).toBe('2026-05-02T22:00:00.000Z');
  });

  it('builds the default range from the SHOP\'s today, not the browser\'s', async () => {
    // Every other test supplies explicit dates, so the default-range path —
    // the one every user hits on load — was never exercised. Near midnight in
    // another zone it would open on the wrong week.
    mockApi([]);
    const w = await mountView();
    const shopToday = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/Chicago', year: 'numeric', month: '2-digit', day: '2-digit',
    }).format(new Date());
    const start = w.vm.startDate, end = w.vm.endDate;
    const key = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    expect(key(start) <= shopToday).toBe(true);
    expect(key(end) >= shopToday).toBe(true);
    expect(start.getDay()).toBe(1);  // Monday-based shop week
  });

  it('degrades to browser-local rather than throwing when no zone is set', async () => {
    tenantTz.value = null;
    mockApi([LATE_SHIFT], ROSTER, null);
    const w = await mountView(MAY);
    expect(w.find('[data-testid="timesheets-zone"]').exists()).toBe(false);
    expect(() => w.vm.formatDay(LATE_SHIFT.clock_in_at)).not.toThrow();
    expect(w.vm.timecards.length).toBe(1);
  });
});

describe('TimesheetsView — reading the crew', () => {
  it('asks for every technician, not just the caller', async () => {
    mockApi([]);
    await mountView();
    const entriesCall = apiGet.mock.calls.find((c) => c[0].startsWith('/api/timeclock/entries'));
    expect(entriesCall).toBeTruthy();
    expect(entriesCall[0]).toContain('all_technicians=true');
  });

  it('groups per person and totals their worked hours', async () => {
    mockApi([
      { id: 'a', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: '2026-05-11T21:00:00Z', minutes: 480, entry_type: 'clock', notes: null },
      { id: 'b', technician_id: 'u-tech-a', clock_in_at: '2026-05-12T13:00:00Z', clock_out_at: '2026-05-12T19:00:00Z', minutes: 360, entry_type: 'clock', notes: null },
      { id: 'c', technician_id: 'u-former', clock_in_at: '2026-05-11T14:00:00Z', clock_out_at: '2026-05-11T18:00:00Z', minutes: 240, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(MAY);
    const totals = w.findAll('.timecard-total').map((n) => n.text());
    const names = w.findAll('.timecard-name').map((n) => n.text());
    // Sorted by name: Dana before Sam.
    expect(names).toEqual(['Dana Ruiz', 'Sam Okafor']);
    expect(totals).toEqual(['14.00h', '4.00h']);
  });

  it('nets break time out of worked hours', async () => {
    // `minutes` is GROSS elapsed — clock-out writes _minutes_between(in, now)
    // and never subtracts breaks, which live in their own table and come back
    // as break_minutes. Totalling `minutes` alone pays out every lunch.
    // (An earlier version of this test seeded entry_type:'break' — a row shape
    // the backend never writes; it only writes 'clock' and 'manual'. It made a
    // dead filter go green while the real break table sat unread.)
    mockApi([
      { id: 'a', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: '2026-05-11T21:00:00Z', minutes: 480, break_minutes: 30, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(MAY);
    expect(w.find('.timecard-total').text()).toBe('7.50h');
    expect(w.vm.timecards[0].breakHours).toBe(0.5);
  });

  it('treats a missing break_minutes as no break, not as an error', async () => {
    mockApi([
      { id: 'a', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: '2026-05-11T21:00:00Z', minutes: 480, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(MAY);
    expect(w.find('.timecard-total').text()).toBe('8.00h');
  });

  it('counts only the entries actually on screen', async () => {
    // `entries` is the raw fetch and carries the ±1 day of slack the shop-day
    // boundary needs, so the tile must count the rendered rows instead.
    mockApi([
      { id: 'in', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: '2026-05-11T21:00:00Z', minutes: 480, entry_type: 'clock', notes: null },
      { id: 'slack', technician_id: 'u-tech-a', clock_in_at: '2026-06-18T13:00:00Z', clock_out_at: '2026-06-18T21:00:00Z', minutes: 480, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(MAY);
    expect(w.vm.shownEntryCount).toBe(1);
    expect(w.vm.entries).toHaveLength(2);
  });

  it('keeps every name in the tech filter after one is picked', async () => {
    // Deriving the options from the FILTERED list made this a one-way door:
    // pick one person and every other name disappeared from the dropdown.
    mockApi([
      { id: 'a', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: '2026-05-11T21:00:00Z', minutes: 480, entry_type: 'clock', notes: null },
      { id: 'c', technician_id: 'u-former', clock_in_at: '2026-05-11T14:00:00Z', clock_out_at: '2026-05-11T18:00:00Z', minutes: 240, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(MAY);
    expect(w.vm.techFilterOptions.map((o) => o.value)).toEqual(['all', 'u-tech-a', 'u-former']);

    w.vm.techFilter = 'u-tech-a';
    await flushPromises();
    expect(w.vm.timecards).toHaveLength(1);
    // ...and you can still switch straight to the other person.
    expect(w.vm.techFilterOptions.map((o) => o.value)).toEqual(['all', 'u-tech-a', 'u-former']);
  });

  it('names a departed employee from the roster', async () => {
    mockApi([
      { id: 'c', technician_id: 'u-former', clock_in_at: '2026-05-11T14:00:00Z', clock_out_at: '2026-05-11T18:00:00Z', minutes: 240, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(MAY);
    expect(w.find('.timecard-name').text()).toBe('Sam Okafor');
  });

  it('shows an unmatched technician id rather than dropping the hours', async () => {
    mockApi([
      { id: 'x', technician_id: 'ghost-4f2a9c11', clock_in_at: '2026-05-11T14:00:00Z', clock_out_at: '2026-05-11T18:00:00Z', minutes: 240, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(MAY);
    expect(w.find('.timecard-name').text()).toBe('Unknown (ghost-4f)');
  });
});

describe('TimesheetsView — flagging what Dispatch nags about', () => {
  const cases = [
    ['long-open shift (never clocked out)', { clock_out_at: null, minutes: null }],
    ['auto-closed, unknown duration', { clock_out_at: '2026-05-12T13:00:00Z', minutes: null }],
    ['implausible length', { clock_out_at: '2026-05-14T13:00:00Z', minutes: 4323 }],
  ];
  it.each(cases)('flags %s', async (_label, shape) => {
    mockApi([
      { id: 'f', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z', entry_type: 'clock', notes: null, ...shape },
    ]);
    const w = await mountView(MAY);
    expect(w.find('[data-testid="timecard-flagged-u-tech-a"]').text()).toBe('1 to fix');
  });

  it('does not flag an ordinary shift', async () => {
    mockApi([
      { id: 'ok', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: '2026-05-11T21:00:00Z', minutes: 480, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(MAY);
    expect(w.find('[data-testid="timecard-flagged-u-tech-a"]').exists()).toBe(false);
  });

  it('does not flag a tech who is simply on the clock right now', async () => {
    // The anti-wallpaper rule. Flagging every null clock_out would read
    // "Needs a look: 4" at 10am with four techs working, while Dispatch reads
    // 0 — training the office to ignore the count. /exceptions only reports an
    // open shift once it passes MAX_SHIFT_HOURS; this must agree.
    const twoHoursAgo = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    const today = [new Date(Date.now() - 86400000), new Date(Date.now() + 86400000)];
    mockApi([
      { id: 'live', technician_id: 'u-tech-a', clock_in_at: twoHoursAgo, clock_out_at: null, minutes: null, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(today);
    expect(w.vm.timecards[0].entries).toHaveLength(1);
    expect(w.vm.flaggedCount).toBe(0);
  });

  it('flags an open shift once it passes the max-shift threshold', async () => {
    const longAgo = new Date(Date.now() - 30 * 3600 * 1000).toISOString();
    const window = [new Date(Date.now() - 3 * 86400000), new Date(Date.now() + 86400000)];
    mockApi([
      { id: 'stuck', technician_id: 'u-tech-a', clock_in_at: longAgo, clock_out_at: null, minutes: null, entry_type: 'clock', notes: null },
    ]);
    const w = await mountView(window);
    expect(w.vm.flaggedCount).toBe(1);
  });
});

describe('TimesheetsView — Dispatch\'s Fix deep link', () => {
  const FLAGGED = {
    id: 'entry-42', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z',
    clock_out_at: null, minutes: null, entry_type: 'clock', notes: null,
  };

  it('opens the correction dialog for the entry Dispatch pointed at', async () => {
    routeQuery.value = { entry: 'entry-42', tech: 'u-tech-a', on: '2026-05-11' };
    mockApi([FLAGGED]);
    const w = await mountView();
    expect(w.find('.dlg').exists()).toBe(true);
    // and clears the query so a refresh doesn't reopen it
    expect(routerReplace).toHaveBeenCalledWith({ path: '/timesheets' });
  });

  it('says so plainly when the entry is not in the loaded range', async () => {
    routeQuery.value = { entry: 'entry-does-not-exist', tech: 'u-tech-a', on: '2026-05-11' };
    mockApi([FLAGGED]);
    const w = await mountView();
    expect(w.find('[data-testid="timesheets-deeplink-miss"]').exists()).toBe(true);
    expect(w.find('.dlg').exists()).toBe(false);
  });

  it('opens the entry once a widened range finally includes it', async () => {
    // The click already identified the shift; widening the dates should open
    // it rather than make the user hunt for the row again.
    routeQuery.value = { entry: 'entry-42', tech: 'u-tech-a', on: '2026-05-11' };
    mockApi([]);
    const w = await mountView();
    expect(w.find('[data-testid="timesheets-deeplink-miss"]').exists()).toBe(true);

    mockApi([FLAGGED]);
    await w.vm.load();
    await flushPromises();
    expect(w.find('[data-testid="timesheets-deeplink-miss"]').exists()).toBe(false);
    expect(w.find('.dlg').exists()).toBe(true);
  });

  it('opens no dialog at all without a deep link', async () => {
    mockApi([FLAGGED]);
    const w = await mountView();
    expect(w.find('.dlg').exists()).toBe(false);
    expect(w.find('[data-testid="timesheets-deeplink-miss"]').exists()).toBe(false);
  });
});

describe('TimesheetsView — writing a correction', () => {
  it('PATCHes the entry with UTC stamps', async () => {
    routeQuery.value = { entry: 'entry-42', tech: 'u-tech-a', on: '2026-05-11' };
    mockApi([{
      id: 'entry-42', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z',
      clock_out_at: null, minutes: null, entry_type: 'clock', notes: null,
    }]);
    apiPatch.mockResolvedValue({});
    const w = await mountView();

    w.vm.editing.clockOut = new Date('2026-05-11T21:00:00Z');
    await w.vm.save();

    expect(apiPatch).toHaveBeenCalledTimes(1);
    const [url, body] = apiPatch.mock.calls[0];
    expect(url).toBe('/api/timeclock/entries/entry-42');
    // UTC on the wire — a local-time payload books the wrong hours.
    expect(body.clock_in_at).toBe('2026-05-11T13:00:00.000Z');
    expect(body.clock_out_at).toBe('2026-05-11T21:00:00.000Z');
  });

  it('shows the dialog preview as ELAPSED, reconciled against the netted row', async () => {
    // The dialog edits clock times and cannot edit breaks, so its number is
    // elapsed — but the row it was opened from shows WORKED. Labelling the
    // preview "total" made the two contradict each other: a shift with an
    // hour's lunch read 7.00 in the table and 8.00h in the dialog.
    routeQuery.value = { entry: 'e-brk', tech: 'u-tech-a', on: '2026-05-11' };
    mockApi([{
      id: 'e-brk', technician_id: 'u-tech-a',
      clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: '2026-05-11T21:00:00Z',
      minutes: 480, break_minutes: 60, entry_type: 'clock', notes: null,
    }]);
    const w = await mountView();
    expect(w.vm.editing.hours).toBeCloseTo(8, 5);        // elapsed
    expect(w.vm.editing.breakMinutes).toBe(60);
    expect(w.vm.editingWorkedHours).toBeCloseTo(7, 5);   // matches the row
  });

  it('refuses to save a clock-out that precedes the clock-in', async () => {
    routeQuery.value = { entry: 'entry-42', tech: 'u-tech-a', on: '2026-05-11' };
    mockApi([{
      id: 'entry-42', technician_id: 'u-tech-a', clock_in_at: '2026-05-11T13:00:00Z',
      clock_out_at: null, minutes: null, entry_type: 'clock', notes: null,
    }]);
    const w = await mountView();

    w.vm.editing.clockOut = new Date('2026-05-11T09:00:00Z');
    await flushPromises();
    expect(w.vm.canSave).toBe(false);
  });

  it('refuses to reopen a closed shift by clearing the clock-out', async () => {
    // PATCH accepts an explicit null and nulls clock_out_at AND minutes, while
    // the audit row records only the new values — so the original clock-out
    // would be gone with no before-image, and the shift would reappear on
    // Dispatch's card. Reopening is not a correction this page needs to offer.
    routeQuery.value = { entry: 'closed', tech: 'u-tech-a', on: '2026-05-11' };
    mockApi([{
      id: 'closed', technician_id: 'u-tech-a',
      clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: '2026-05-11T21:00:00Z',
      minutes: 480, entry_type: 'clock', notes: null,
    }]);
    const w = await mountView();
    expect(w.vm.canSave).toBe(true);
    w.vm.editing.clockOut = null;
    await flushPromises();
    expect(w.vm.canSave).toBe(false);
  });

  it('still allows saving an open shift that stays open (notes-only edit)', async () => {
    routeQuery.value = { entry: 'open', tech: 'u-tech-a', on: '2026-05-11' };
    mockApi([{
      id: 'open', technician_id: 'u-tech-a',
      clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: null,
      minutes: null, entry_type: 'clock', notes: null,
    }]);
    const w = await mountView();
    w.vm.editing.notes = 'Chasing the real end time with the tech.';
    await flushPromises();
    expect(w.vm.canSave).toBe(true);
  });

  it('POSTs a manual entry for a shift that was never clocked', async () => {
    mockApi([]);
    apiPost.mockResolvedValue({});
    const w = await mountView();

    w.vm.openCreate();
    w.vm.editing.technicianId = 'u-tech-a';
    w.vm.editing.clockIn = new Date('2026-05-11T13:00:00Z');
    w.vm.editing.clockOut = new Date('2026-05-11T21:00:00Z');
    await w.vm.save();

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, body] = apiPost.mock.calls[0];
    expect(url).toBe('/api/timeclock/entries');
    expect(body.technician_id).toBe('u-tech-a');
    expect(body.clock_in_at).toBe('2026-05-11T13:00:00.000Z');
  });
});

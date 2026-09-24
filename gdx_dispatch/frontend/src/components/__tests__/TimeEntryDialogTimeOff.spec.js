/**
 * TimeEntryDialog — the office adds paid time off from the same dialog it
 * adds a shift with (Type = Vacation / Holiday).
 *
 * Pins:
 *  - the type picker exists only in office create mode;
 *  - picking a time-off type swaps the clock pickers for a date range and
 *    hours per day, reads the shop's day length from /options, and Save posts
 *    to /api/timeclock/time-off/entries with calendar dates and MINUTES — the
 *    dialog never fabricates clock stamps for a day nobody clocked;
 *  - Type = Shift still posts to /api/timeclock/entries exactly as before.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn(() => Promise.resolve({}));
const apiPost = vi.fn();
const apiPatch = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
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

import TimeEntryDialog from '../TimeEntryDialog.vue';

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  DatePicker: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input class="dp" />' },
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg" :data-header="header"><slot /><slot name="footer" /></div>',
  },
  InputNumber: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input type="number" />' },
  InputText: { props: ['value'], template: '<input />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<select />' },
  Textarea: { props: ['modelValue'], emits: ['update:modelValue'], template: '<textarea />' },
};

const ROSTER = [{ label: 'Dana Ruiz', value: 'u-tech-a' }];

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiPatch.mockReset();
  apiGet.mockImplementation((url) =>
    url.startsWith('/api/timeclock/time-off/options')
      ? Promise.resolve({ default_minutes: 420 })
      : Promise.resolve({}),
  );
});

async function openOffice(extra = {}) {
  const w = mount(TimeEntryDialog, {
    props: { visible: false, rosterOptions: ROSTER, anchorDate: new Date(2026, 9, 2), ...extra },
    global: { stubs },
  });
  await w.setProps({ visible: true });
  await flushPromises();
  return w;
}

describe('TimeEntryDialog — time off (office create)', () => {
  it('offers a Type picker in office create mode only', async () => {
    const office = await openOffice();
    expect(office.find('[data-testid="entry-kind-select"]').exists()).toBe(true);
    expect(office.find('[data-testid="entry-clock-in"]').exists()).toBe(true);

    const self = mount(TimeEntryDialog, { props: { visible: false, selfMode: true }, global: { stubs } });
    await self.setProps({ visible: true });
    await flushPromises();
    expect(self.find('[data-testid="entry-kind-select"]').exists()).toBe(false);
  });

  it('Vacation swaps the clock pickers for a range + hours and posts calendar dates as minutes', async () => {
    apiPost.mockResolvedValue({ created: 2, entry_ids: ['a', 'b'], skipped_days: [] });
    const w = await openOffice();
    w.vm.editing.technicianId = 'u-tech-a';
    w.vm.editing.kind = 'vacation';
    await flushPromises();

    expect(w.find('[data-testid="entry-clock-in"]').exists()).toBe(false);
    expect(w.find('[data-testid="entry-off-start"]').exists()).toBe(true);
    expect(w.find('[data-testid="entry-off-hours"]').exists()).toBe(true);
    expect(w.find('.dlg').attributes('data-header')).toBe('Add vacation');
    // Day length came from /options (7h), not a guess.
    expect(apiGet).toHaveBeenCalledWith('/api/timeclock/time-off/options', expect.anything());
    expect(w.vm.editing.offHours).toBe(7);

    w.vm.editing.offStart = new Date(2026, 9, 2);
    w.vm.editing.offEnd = new Date(2026, 9, 5);
    w.vm.editing.offHours = 4;
    w.vm.editing.notes = 'half days';
    await flushPromises();
    expect(w.vm.canSave).toBe(true);

    await w.find('[data-testid="entry-save"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, body] = apiPost.mock.calls[0];
    expect(url).toBe('/api/timeclock/time-off/entries');
    expect(body).toEqual({
      technician_id: 'u-tech-a', entry_type: 'vacation',
      start_date: '2026-10-02', end_date: '2026-10-05', minutes_per_day: 240, notes: 'half days',
    });
    expect(w.emitted('saved')).toBeTruthy();
  });

  it('Shift still posts a clock entry to /api/timeclock/entries', async () => {
    apiPost.mockResolvedValue({});
    const w = await openOffice();
    w.vm.editing.technicianId = 'u-tech-a';
    await flushPromises();
    expect(w.vm.editing.kind).toBe('shift');
    await w.find('[data-testid="entry-save"]').trigger('click');
    await flushPromises();
    expect(apiPost.mock.calls[0][0]).toBe('/api/timeclock/entries');
    expect(apiPost.mock.calls[0][1]).toHaveProperty('clock_in_at');
  });

  it('a Save without a person, or a backwards range, stays disabled', async () => {
    const w = await openOffice();
    w.vm.editing.kind = 'holiday';
    await flushPromises();
    expect(w.vm.canSave).toBe(false);   // no technician
    w.vm.editing.technicianId = 'u-tech-a';
    w.vm.editing.offStart = new Date(2026, 9, 5);
    w.vm.editing.offEnd = new Date(2026, 9, 2);
    await flushPromises();
    expect(w.vm.canSave).toBe(false);   // end before start
  });

  it('correcting a vacation row names it and keeps the span editable', async () => {
    const w = mount(TimeEntryDialog, {
      props: {
        visible: false, techName: 'Dana Ruiz',
        entry: {
          id: 'v1', technician_id: 'u-tech-a', entry_type: 'vacation', minutes: 480,
          clock_in_at: '2026-10-02T13:00:00+00:00', clock_out_at: '2026-10-02T21:00:00+00:00', notes: 'beach',
        },
      },
      global: { stubs },
    });
    await w.setProps({ visible: true });
    await flushPromises();
    expect(w.find('.dlg').attributes('data-header')).toBe('Correct this vacation day');
    expect(w.find('[data-testid="entry-time-off-hint"]').exists()).toBe(true);
    expect(w.find('[data-testid="entry-kind-select"]').exists()).toBe(false);
  });
});

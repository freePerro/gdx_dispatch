/**
 * Timesheets — the Export buttons.
 *
 * The failures pinned here:
 *
 *  - Exporting a range other than the one on screen. The endpoint takes the
 *    dates explicitly, so the button must send what the pickers hold — not
 *    a period name the server would resolve independently.
 *  - Exporting an empty range. A CSV with a header and nothing under it
 *    reads as "everyone worked zero hours" once it is in somebody's inbox.
 *  - A failed export that looks like a successful one. `downloadAuthedFile`
 *    rejecting must surface, not be swallowed into a spinner that stops.
 *  - The tech filter being ignored, so "export Amber" hands over the crew.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn();
const toastAdd = vi.fn();
const downloadAuthedFile = vi.fn(() => Promise.resolve());

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: vi.fn(), patch: vi.fn() }),
}));
vi.mock('../../composables/useAuthedFile', () => ({
  downloadAuthedFile: (...args) => downloadAuthedFile(...args),
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
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Card: { template: '<div class="timecard"><slot name="title" /><slot name="content" /></div>' },
  Column: { props: ['header'], template: '<div />' },
  DataTable: { props: ['value'], template: '<div class="dt" />' },
  DatePicker: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input />' },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
  InputText: { props: ['value'], template: '<input />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  ProgressSpinner: { template: '<div />' },
  // Renders real <option>s: a <select> with no options cannot hold a value,
  // so setValue() on an empty stub silently does nothing and the assertion
  // below would pass for the wrong reason.
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    template: `<select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)">
      <option v-for="o in (options || [])" :key="o.value ?? o" :value="o.value ?? o">{{ o.label ?? o }}</option>
    </select>`,
  },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
};

const CALENDAR = {
  configured: true,
  cadence: 'biweekly',
  timezone: 'America/Chicago',
  today: '2026-08-26',
  current: { start: '2026-08-24', end: '2026-09-06', label: '2026-08-24 – 2026-09-06', pay_date: '2026-09-11' },
  previous: { start: '2026-08-10', end: '2026-08-23', label: '2026-08-10 – 2026-08-23', pay_date: '2026-08-28' },
  next: { start: '2026-09-07', end: '2026-09-20', label: '2026-09-07 – 2026-09-20', pay_date: '2026-09-25' },
};

const ROSTER = [
  { technician_id: 'u-michael', name: 'Michael Tallman', active: true, has_entries: true },
  { technician_id: 'u-amber', name: 'Amber Joy Rosa', active: true, has_entries: true },
];

// Shifts in BOTH the current period (2026-08-24 .. 2026-09-06, what the page
// opens on) and the previous one, so switching periods still leaves rows —
// otherwise the export button would be disabled for the honest reason that
// the range is empty, and the assertion would fail for the wrong cause.
const ENTRIES = [
  {
    id: 'e0', technician_id: 'u-michael', clock_in_at: '2026-08-17T13:00:00+00:00',
    clock_out_at: '2026-08-17T22:00:00+00:00', minutes: 540, break_minutes: 0,
    entry_type: 'clock', notes: null,
  },
  {
    id: 'e1', technician_id: 'u-michael', clock_in_at: '2026-08-25T13:00:00+00:00',
    clock_out_at: '2026-08-25T22:00:00+00:00', minutes: 540, break_minutes: 0,
    entry_type: 'clock', notes: null,
  },
  {
    id: 'e2', technician_id: 'u-amber', clock_in_at: '2026-08-26T14:00:00+00:00',
    clock_out_at: '2026-08-26T18:00:00+00:00', minutes: 240, break_minutes: 0,
    entry_type: 'clock', notes: null,
  },
];

function mockApi(entries = ENTRIES) {
  apiGet.mockImplementation((url) => {
    const u = String(url);
    if (u.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: 'America/Chicago' });
    if (u.startsWith('/api/timeclock/pay-periods')) return Promise.resolve(CALENDAR);
    if (u.startsWith('/api/timeclock/roster')) return Promise.resolve(ROSTER);
    if (u.startsWith('/api/timeclock/entries')) return Promise.resolve(entries);
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
  toastAdd.mockReset();
  downloadAuthedFile.mockReset();
  downloadAuthedFile.mockResolvedValue(undefined);
  mockApi();
});

describe('Timesheets — export', () => {
  it('offers both files', async () => {
    const wrapper = await mountPage();
    expect(wrapper.find('[data-testid="timesheets-export-csv"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="timesheets-export-pdf"]').exists()).toBe(true);
  });

  it('exports exactly the range on screen', async () => {
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-export-csv"]').trigger('click');
    await flushPromises();

    const [url, filename] = downloadAuthedFile.mock.calls[0];
    const params = new URLSearchParams(url.split('?')[1]);
    expect(url).toContain('/api/timeclock/pay-period/export.csv');
    expect(params.get('start')).toBe('2026-08-24');
    expect(params.get('end')).toBe('2026-09-06');
    expect(filename).toBe('timesheet_2026-08-24_2026-09-06.csv');
  });

  it('follows the range when a different period is chosen', async () => {
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-last-pay-period"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="timesheets-export-pdf"]').trigger('click');
    await flushPromises();

    const params = new URLSearchParams(downloadAuthedFile.mock.calls[0][0].split('?')[1]);
    expect(params.get('start')).toBe('2026-08-10');
    expect(params.get('end')).toBe('2026-08-23');
  });

  it('names the file for the format asked for', async () => {
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-export-pdf"]').trigger('click');
    await flushPromises();
    expect(downloadAuthedFile.mock.calls[0][0]).toContain('export.pdf');
    expect(downloadAuthedFile.mock.calls[0][1]).toMatch(/\.pdf$/);
  });

  it('passes the tech filter, so "export Amber" is not the whole crew', async () => {
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-tech-filter"]').setValue('u-amber');
    await flushPromises();
    await wrapper.find('[data-testid="timesheets-export-csv"]').trigger('click');
    await flushPromises();

    const params = new URLSearchParams(downloadAuthedFile.mock.calls[0][0].split('?')[1]);
    expect(params.get('technician_id')).toBe('u-amber');
  });

  it('will not export a range nobody worked', async () => {
    mockApi([]);
    const wrapper = await mountPage();
    expect(wrapper.find('[data-testid="timesheets-export-csv"]').attributes('disabled')).toBeDefined();
    await wrapper.find('[data-testid="timesheets-export-csv"]').trigger('click');
    await flushPromises();
    expect(downloadAuthedFile).not.toHaveBeenCalled();
  });

  it('says so when the export fails instead of failing quietly', async () => {
    downloadAuthedFile.mockRejectedValue(new Error('Failed to load file (503)'));
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-export-csv"]').trigger('click');
    await flushPromises();

    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error' }),
    );
  });

  it('recovers so a second attempt is possible', async () => {
    downloadAuthedFile.mockRejectedValueOnce(new Error('boom'));
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-export-csv"]').trigger('click');
    await flushPromises();

    await wrapper.find('[data-testid="timesheets-export-csv"]').trigger('click');
    await flushPromises();
    expect(downloadAuthedFile).toHaveBeenCalledTimes(2);
  });
});

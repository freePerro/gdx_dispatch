/**
 * TimeOffRequestPanel + TimeOffRequestDialog — the tech's side of time off.
 *
 * Pins, named for the failure each prevents:
 *  - the list reads the caller's OWN requests (no all_technicians) and shows
 *    the office's note, so a denied tech learns why without asking;
 *  - Cancel exists only while pending and posts to the cancel route;
 *  - the dialog's POST carries calendar dates and MINUTES, never a
 *    technician_id (the backend resolves "me"), and Send stays disabled
 *    until the reason is typed — the server 422s a blank one;
 *  - the day length comes from /options, not a guess.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));

import TimeOffRequestPanel from '../TimeOffRequestPanel.vue';
import TimeOffRequestDialog from '../TimeOffRequestDialog.vue';

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text', 'outlined', 'size'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Tag: { props: ['value', 'severity'], template: '<span class="tag">{{ value }}</span>' },
  Dialog: {
    props: ['visible', 'header', 'position'],
    template: '<div v-if="visible" class="dlg" :data-position="position"><slot /><slot name="footer" /></div>',
  },
  DatePicker: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input />' },
  InputNumber: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input type="number" />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
};

const REQUESTS = [
  {
    id: 'r1', technician_id: 'me', entry_type: 'vacation', entry_type_label: 'Vacation',
    start_date: '2026-10-02', end_date: '2026-10-05', minutes_per_day: 480, workday_count: 2,
    notes: 'long weekend', status: 'pending', review_note: null,
  },
  {
    id: 'r2', technician_id: 'me', entry_type: 'vacation', entry_type_label: 'Vacation',
    start_date: '2026-09-01', end_date: '2026-09-01', minutes_per_day: 240, workday_count: 1,
    notes: 'dentist', status: 'denied', review_note: 'short-staffed that day',
  },
];

function mockApi(options = { default_minutes: 420, workdays: 31, self_service_backdate_days: 14 }) {
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/timeclock/time-off/requests')) return Promise.resolve(REQUESTS);
    if (url.startsWith('/api/timeclock/time-off/options')) return Promise.resolve(options);
    return Promise.resolve([]);
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
});

describe('TimeOffRequestPanel', () => {
  it('lists the caller’s own requests with status and the office’s note', async () => {
    mockApi();
    const w = mount(TimeOffRequestPanel, { global: { stubs } });
    await flushPromises();

    const url = apiGet.mock.calls.find(([u]) => u.startsWith('/api/timeclock/time-off/requests'))[0];
    expect(url).not.toContain('all_technicians');

    expect(w.find('[data-testid="time-off-request-r1"]').exists()).toBe(true);
    expect(w.find('[data-testid="time-off-request-r1"]').text()).toContain('pending');
    expect(w.find('[data-testid="time-off-request-r1"]').text()).toContain('2 workdays');
    expect(w.find('[data-testid="time-off-request-r2"]').text()).toContain('Office: short-staffed that day');
    // Cancel only while pending.
    expect(w.find('[data-testid="time-off-cancel-r1"]').exists()).toBe(true);
    expect(w.find('[data-testid="time-off-cancel-r2"]').exists()).toBe(false);
  });

  it('cancel posts to the cancel route, reloads and tells the parent', async () => {
    mockApi();
    apiPost.mockResolvedValue({ status: 'cancelled' });
    const w = mount(TimeOffRequestPanel, { global: { stubs } });
    await flushPromises();
    const before = apiGet.mock.calls.length;

    await w.find('[data-testid="time-off-cancel-r1"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith(
      '/api/timeclock/time-off/requests/r1/cancel', {}, expect.objectContaining({ successMessage: expect.any(String) }),
    );
    expect(apiGet.mock.calls.length).toBeGreaterThan(before);
    expect(w.emitted('changed')).toBeTruthy();
  });

  it('shows the empty state and the request button when nothing is on file', async () => {
    apiGet.mockResolvedValue([]);
    const w = mount(TimeOffRequestPanel, { global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="time-off-empty"]').exists()).toBe(true);
    expect(w.find('[data-testid="time-off-request-btn"]').exists()).toBe(true);
  });
});

describe('TimeOffRequestDialog', () => {
  it('reads the day length from /options and requires a reason before Send', async () => {
    mockApi({ default_minutes: 420, workdays: 31, self_service_backdate_days: 14 });
    const w = mount(TimeOffRequestDialog, { props: { visible: true }, global: { stubs } });
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith('/api/timeclock/time-off/options', expect.anything());
    expect(w.vm.form.hours).toBe(7);
    expect(w.vm.maxHours).toBe(7);
    // Dates default to today; no reason yet → disabled.
    expect(w.vm.canSave).toBe(false);
    w.vm.form.notes = 'beach';
    await flushPromises();
    expect(w.vm.canSave).toBe(true);
  });

  it('posts calendar dates and minutes, never a technician_id, then emits saved', async () => {
    mockApi();
    apiPost.mockResolvedValue({ id: 'new', status: 'pending' });
    const w = mount(TimeOffRequestDialog, { props: { visible: true }, global: { stubs } });
    await flushPromises();

    w.vm.form.start = new Date(2026, 9, 2);   // Fri Oct 2 (local)
    w.vm.form.end = new Date(2026, 9, 5);     // Mon Oct 5
    w.vm.form.hours = 4;
    w.vm.form.notes = '  half days  ';
    await flushPromises();
    expect(w.vm.workdayCount).toBe(2);

    await w.find('[data-testid="to-save"]').trigger('click');
    await flushPromises();

    const [url, body] = apiPost.mock.calls[0];
    expect(url).toBe('/api/timeclock/time-off/requests');
    expect(body).toEqual({
      start_date: '2026-10-02', end_date: '2026-10-05', minutes_per_day: 240, notes: 'half days',
    });
    expect(body).not.toHaveProperty('technician_id');
    expect(w.emitted('saved')).toBeTruthy();
    expect(w.emitted('update:visible')[0]).toEqual([false]);
  });

  it('keeps a server refusal on screen instead of vanishing', async () => {
    mockApi();
    apiPost.mockRejectedValue({ status: 409, body: { detail: 'a pending request already covers 2026-10-02 to 2026-10-05' } });
    const w = mount(TimeOffRequestDialog, { props: { visible: true }, global: { stubs } });
    await flushPromises();
    w.vm.form.notes = 'x';
    await flushPromises();
    await w.find('[data-testid="to-save"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="to-error"]').text()).toContain('already covers');
    expect(w.emitted('saved')).toBeFalsy();
  });

  it('renders as a bottom sheet on the phone', async () => {
    mockApi();
    const w = mount(TimeOffRequestDialog, { props: { visible: true, mobile: true }, global: { stubs } });
    await flushPromises();
    expect(w.find('.dlg').attributes('data-position')).toBe('bottom');
  });
});

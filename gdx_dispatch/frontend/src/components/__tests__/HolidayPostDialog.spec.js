/**
 * HolidayPostDialog — who gets the holiday's hours.
 *
 * Pins:
 *  - the people come from /api/timeclock/roster (active only), with those who
 *    have clocked in pre-ticked — the hourly crew — and the office free to
 *    change it;
 *  - the POST carries the calendar date and exactly the ticked ids;
 *  - the result (posted N, skipped who) stays on screen and `posted` fires,
 *    so the page behind it reloads its rows.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));

import HolidayPostDialog from '../HolidayPostDialog.vue';

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Checkbox: {
    props: ['modelValue', 'binary', 'inputId'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
  },
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  Message: { template: '<div class="msg"><slot /></div>' },
};

const ROSTER = [
  { technician_id: 'u-tech-a', name: 'Dana Ruiz', active: true, has_entries: true },
  { technician_id: 'u-office', name: 'Pat Office', active: true, has_entries: false },
  { technician_id: 'u-former', name: 'Sam Okafor', active: false, has_entries: true },
];
const XMAS = { date: '2026-12-25', name: 'Christmas Day', minutes: 480 };

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiGet.mockResolvedValue(ROSTER);
});

describe('HolidayPostDialog', () => {
  it('lists active people, pre-ticking those who clock in', async () => {
    const w = mount(HolidayPostDialog, { props: { visible: true, holiday: XMAS }, global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="holiday-person-u-tech-a"]').exists()).toBe(true);
    expect(w.find('[data-testid="holiday-person-u-office"]').exists()).toBe(true);
    expect(w.find('[data-testid="holiday-person-u-former"]').exists()).toBe(false);
    expect(w.vm.selectedIds).toEqual(['u-tech-a']);
    expect(w.find('[data-testid="holiday-post-confirm"]').text()).toBe('Post for 1');
  });

  it('posts the date and the ticked ids, then shows the result and emits posted', async () => {
    apiPost.mockResolvedValue({ created: 1, skipped: ['u-office'] });
    const w = mount(HolidayPostDialog, { props: { visible: true, holiday: XMAS }, global: { stubs } });
    await flushPromises();
    await w.find('[data-testid="holiday-person-u-office"]').setValue(true);
    await flushPromises();
    expect(w.vm.selectedIds).toEqual(['u-tech-a', 'u-office']);

    await w.find('[data-testid="holiday-post-confirm"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith(
      '/api/timeclock/time-off/holidays/post',
      { date: '2026-12-25', technician_ids: ['u-tech-a', 'u-office'] },
      expect.objectContaining({ successMessage: expect.any(String) }),
    );
    const result = w.find('[data-testid="holiday-post-result"]').text();
    expect(result).toContain('Posted for 1 person');
    expect(result).toContain('Pat Office');
    expect(w.emitted('posted')).toBeTruthy();
    expect(w.find('[data-testid="holiday-post-close"]').exists()).toBe(true);
  });

  it('keeps a refusal on screen (date not on the calendar)', async () => {
    apiPost.mockRejectedValue({ status: 422, body: { detail: '2026-12-25 is not on the holiday calendar — add it in Settings first' } });
    const w = mount(HolidayPostDialog, { props: { visible: true, holiday: XMAS }, global: { stubs } });
    await flushPromises();
    await w.find('[data-testid="holiday-post-confirm"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="holiday-post-error"]').text()).toContain('not on the holiday calendar');
    expect(w.emitted('posted')).toBeFalsy();
  });
});

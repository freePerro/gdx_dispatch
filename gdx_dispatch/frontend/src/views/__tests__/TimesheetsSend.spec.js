/**
 * Timesheets — Send to payroll.
 *
 * An email leaves the building and cannot be recalled, so the failures here
 * are about honesty at the moment of sending:
 *
 *  - Sending without asking. One stray click on a toolbar button must not
 *    mail somebody's hours.
 *  - Sending a range other than the one on screen.
 *  - A 409 hold rendered as a generic error toast. The server returns the
 *    offending shifts so the operator can fix them; swallowing that turns a
 *    two-minute correction into a mystery.
 *  - Reporting "sent" on a partial delivery without naming who missed out.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: vi.fn() }),
}));
vi.mock('../../composables/useAuthedFile', () => ({
  downloadAuthedFile: vi.fn(() => Promise.resolve()),
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
  // Renders only when visible, like the real one — otherwise "the dialog did
  // not open" and "the dialog opened" look identical to a test.
  Dialog: {
    props: ['visible'],
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
];

const ENTRIES = [
  {
    id: 'e1', technician_id: 'u-michael', clock_in_at: '2026-08-25T13:00:00+00:00',
    clock_out_at: '2026-08-25T22:00:00+00:00', minutes: 540, break_minutes: 0,
    entry_type: 'clock', notes: null,
  },
];

function apiError(status, detail) {
  const err = new Error(typeof detail === 'string' ? detail : 'Conflict');
  err.status = status;
  err.body = { detail };
  return err;
}

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

async function openAndSend(wrapper) {
  await wrapper.find('[data-testid="timesheets-send"]').trigger('click');
  await flushPromises();
  await wrapper.find('[data-testid="send-confirm-button"]').trigger('click');
  await flushPromises();
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  toastAdd.mockReset();
  mockApi();
  apiPost.mockResolvedValue({
    sent: true,
    hours: 9,
    people: 1,
    delivered_to: ['bookkeeper@example.com'],
    failed_to: [],
    period: { start: '2026-08-24', end: '2026-09-06', label: '2026-08-24 – 2026-09-06' },
  });
});

describe('Timesheets — send to payroll', () => {
  it('asks before mailing anything', async () => {
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-send"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-testid="send-confirm"]').exists()).toBe(true);
    expect(apiPost).not.toHaveBeenCalled();
  });

  it('states the exact range before it goes', async () => {
    const wrapper = await mountPage();
    await wrapper.find('[data-testid="timesheets-send"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-testid="send-confirm"]').text())
      .toContain('2026-08-24 – 2026-09-06');
  });

  it('sends the range on screen', async () => {
    const wrapper = await mountPage();
    await openAndSend(wrapper);

    expect(apiPost).toHaveBeenCalledWith(
      '/api/timeclock/pay-period/send',
      expect.objectContaining({ start: '2026-08-24', end: '2026-09-06' }),
      expect.anything(),
    );
  });

  it('confirms who actually received it', async () => {
    const wrapper = await mountPage();
    await openAndSend(wrapper);

    const ok = wrapper.find('[data-testid="send-success"]');
    expect(ok.exists()).toBe(true);
    expect(ok.text()).toContain('bookkeeper@example.com');
  });

  it('names who missed out on a partial delivery', async () => {
    apiPost.mockResolvedValue({
      sent: true, hours: 9, people: 1,
      delivered_to: ['ok@example.com'],
      failed_to: ['bad@example.com'],
      detail: 'Mail was not accepted (rejected).',
      period: { label: '2026-08-24 – 2026-09-06' },
    });
    const wrapper = await mountPage();
    await openAndSend(wrapper);

    const text = wrapper.find('[data-testid="send-success"]').text();
    expect(text).toContain('bad@example.com');
    expect(text).toContain('Not delivered');
  });

  it('shows the held-back shifts instead of a generic error', async () => {
    apiPost.mockRejectedValue(apiError(409, {
      sent: false,
      blocked: 'flagged_shifts',
      detail: '1 shift still needs a look. Correct them on Timesheets and the hold clears itself.',
      flagged: [
        { name: 'Michael Tallman', date: '2026-08-25', reason: 'still clocked in', entry_id: 'e1' },
      ],
    }));
    const wrapper = await mountPage();
    await openAndSend(wrapper);

    const blocked = wrapper.find('[data-testid="send-blocked"]');
    expect(blocked.exists()).toBe(true);
    expect(blocked.text()).toContain('Michael Tallman');
    expect(blocked.text()).toContain('2026-08-25');
    expect(blocked.text()).toContain('still clocked in');
    expect(toastAdd).not.toHaveBeenCalled();
  });

  it('offers no way to send anyway', async () => {
    apiPost.mockRejectedValue(apiError(409, {
      sent: false, blocked: 'flagged_shifts', detail: 'held',
      flagged: [{ name: 'Michael Tallman', date: '2026-08-25', reason: 'still clocked in', entry_id: 'e1' }],
    }));
    const wrapper = await mountPage();
    await openAndSend(wrapper);

    const buttons = wrapper.findAll('button').map((b) => b.text().toLowerCase());
    expect(buttons.some((t) => t.includes('anyway') || t.includes('override') || t.includes('force')))
      .toBe(false);
    expect(wrapper.find('[data-testid="send-confirm-button"]').exists()).toBe(false);
  });

  it('a held-back row opens the correction for that shift', async () => {
    apiPost.mockRejectedValue(apiError(409, {
      sent: false, blocked: 'flagged_shifts', detail: 'held',
      flagged: [{ name: 'Michael Tallman', date: '2026-08-25', reason: 'still clocked in', entry_id: 'e1' }],
    }));
    const wrapper = await mountPage();
    await openAndSend(wrapper);

    await wrapper.find('.flag-link').trigger('click');
    await flushPromises();
    // The send dialog closes and the shared correction dialog takes over.
    expect(wrapper.find('[data-testid="send-blocked"]').exists()).toBe(false);
  });

  it('toasts a real failure rather than showing an empty dialog', async () => {
    apiPost.mockRejectedValue(apiError(502, {
      sent: false, detail: 'Mail was not accepted by the mail server.', failed_to: ['x@y.com'],
    }));
    const wrapper = await mountPage();
    await openAndSend(wrapper);

    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error' }),
    );
    expect(wrapper.find('[data-testid="timesheets-send-dialog"]').exists()).toBe(false);
  });

  it('will not send a range nobody worked', async () => {
    mockApi([]);
    const wrapper = await mountPage();
    expect(wrapper.find('[data-testid="timesheets-send"]').attributes('disabled')).toBeDefined();
  });
});

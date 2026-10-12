/**
 * MobileTimeclockView — "Vacation <year>" above the phone's Time off section
 * (GDXA-703, part 4 of GDXA-576). Techs reach the clock on their phone
 * (/timeclock redirects to /mobile/timeclock), so this is where they see it.
 *
 * Pins: my own balance is read (no technician named); left / used / still
 * ahead render; "no allowance set" is words, not 0h left; an overdrawn
 * balance is a danger tag; a failed read says so; a request change re-reads.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: vi.fn(), patch: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

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

import MobileTimeclockView from '../MobileTimeclockView.vue';

const stubs = {
  TimeOffRequestPanel: { emits: ['changed'], template: '<div class="panel" />' },
  TimeEntryDialog: { template: '<div />' },
  MobileReceiptCapture: { template: '<div />' },
  Button: {
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Select: { props: ['modelValue', 'options'], template: '<select />' },
  Tag: { props: ['value', 'severity'], template: '<span class="tag" :data-severity="severity">{{ value }}</span>' },
};

const MINE = {
  technician_id: 'me', technician_name: 'Me', year: 2026, as_of: '2026-05-20',
  allowance_hours: 40, allowance_source: 'person', used_hours: 16, taken_hours: 8,
  scheduled_hours: 8, remaining_hours: 24, overdrawn: false,
};

function mockApi({ balance = MINE, fail = false } = {}) {
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/timeclock/status')) return Promise.resolve({ clocked_in: false });
    if (url.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: null });
    if (url.startsWith('/api/timeclock/time-off/balances')) {
      return fail ? Promise.reject(new Error('503')) : Promise.resolve({ year: 2026, balances: [balance] });
    }
    return Promise.resolve([]);
  });
}

async function mountView(opts) {
  mockApi(opts);
  const w = mount(MobileTimeclockView, { global: { stubs, directives: { tooltip: {} } } });
  await flushPromises();
  return w;
}

function balanceCalls() {
  return apiGet.mock.calls.filter(([url]) => url.startsWith('/api/timeclock/time-off/balances'));
}

beforeEach(() => {
  apiGet.mockReset();
  tenantTz.value = null;
});

describe('MobileTimeclockView — my vacation balance', () => {
  it('reads my own balance and shows used, still ahead and left', async () => {
    const w = await mountView();
    expect(apiGet).toHaveBeenCalledWith('/api/timeclock/time-off/balances', { suppressErrorToast: true });
    const line = w.find('[data-test="mt-vacation-balance"]').text();
    expect(line).toContain('Vacation 2026');
    expect(line).toContain('16h used');
    expect(line).toContain('8h still ahead');
    expect(line).toContain('of 40h');
    const left = w.find('[data-test="mt-vacation-remaining"]');
    expect(left.text()).toBe('24h left');
    expect(left.attributes('data-severity')).toBe('success');
  });

  it('sits above the Time off panel', async () => {
    const w = await mountView();
    const html = w.html();
    expect(html.indexOf('mt-vacation-balance')).toBeGreaterThan(-1);
    expect(html.indexOf('mt-vacation-balance')).toBeLessThan(html.indexOf('class="panel'));
  });

  it('says "no allowance set" rather than 0h left', async () => {
    const w = await mountView({ balance: { ...MINE, allowance_hours: null, remaining_hours: null, scheduled_hours: 0 } });
    const line = w.find('[data-test="mt-vacation-balance"]').text();
    expect(line).toContain('no allowance set');
    expect(line).not.toContain('still ahead');
    expect(w.find('[data-test="mt-vacation-remaining"]').exists()).toBe(false);
  });

  it('shows an overdrawn balance as negative, in danger', async () => {
    const w = await mountView({ balance: { ...MINE, used_hours: 48, remaining_hours: -8, overdrawn: true } });
    const left = w.find('[data-test="mt-vacation-remaining"]');
    expect(left.text()).toBe('-8h left');
    expect(left.attributes('data-severity')).toBe('danger');
  });

  it('says the read failed instead of showing a balance', async () => {
    const w = await mountView({ fail: true });
    expect(w.find('[data-test="mt-vacation-error"]').exists()).toBe(true);
    expect(w.find('[data-test="mt-vacation-balance"]').exists()).toBe(false);
  });

  it('re-reads the balance when a time-off request changes', async () => {
    const w = await mountView();
    expect(balanceCalls()).toHaveLength(1);
    w.findComponent(stubs.TimeOffRequestPanel).vm.$emit('changed');
    await flushPromises();
    expect(balanceCalls()).toHaveLength(2);
  });
});

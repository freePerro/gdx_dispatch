/**
 * TimeclockView — "Vacation <year>" on the tech's own Time off card (GDXA-701).
 *
 * Pins: the tech's own balance is read (no technician named, so the server
 * answers for me); left / used / still-ahead render; "no allowance set" is
 * words, not 0h left; a failed read says so.
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

const stubs = {
  TimeOffRequestPanel: { template: '<div class="panel" />' },
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

const MINE = {
  technician_id: 'me', technician_name: 'Me', year: 2026, as_of: '2026-05-20',
  allowance_hours: 40, allowance_source: 'person', used_hours: 16, taken_hours: 8,
  scheduled_hours: 8, remaining_hours: 24, overdrawn: false,
};

function mockApi({ balance = MINE, fail = false } = {}) {
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/timeclock/status')) return Promise.resolve({ clocked_in: false });
    if (url.startsWith('/api/me/tech-mobile-settings')) return Promise.resolve({ settings: {}, tenant_timezone: null });
    if (url.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: null });
    if (url.startsWith('/api/timeclock/time-off/balances')) {
      return fail ? Promise.reject(new Error('503')) : Promise.resolve({ year: 2026, balances: [balance] });
    }
    return Promise.resolve([]);
  });
}

async function mountView(opts) {
  mockApi(opts);
  const w = mount(TimeclockView, { global: { stubs, directives: { tooltip: {} } } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  apiGet.mockReset();
  tenantTz.value = null;
});

describe('TimeclockView — my vacation balance', () => {
  it('reads my own balance and shows used, still ahead and left', async () => {
    const w = await mountView();
    expect(apiGet).toHaveBeenCalledWith('/api/timeclock/time-off/balances', expect.anything());
    const line = w.find('[data-testid="my-vacation-balance"]').text();
    expect(line).toContain('Vacation 2026');
    expect(line).toContain('16h used');
    expect(line).toContain('8h still ahead');
    expect(line).toContain('of 40h');
    expect(w.find('[data-testid="my-vacation-remaining"]').text()).toBe('24h left');
  });

  it('says "no allowance set" rather than 0h left', async () => {
    const w = await mountView({ balance: { ...MINE, allowance_hours: null, remaining_hours: null, scheduled_hours: 0 } });
    const line = w.find('[data-testid="my-vacation-balance"]').text();
    expect(line).toContain('no allowance set');
    expect(w.find('[data-testid="my-vacation-remaining"]').exists()).toBe(false);
  });

  it('says the read failed', async () => {
    const w = await mountView({ fail: true });
    expect(w.find('[data-testid="my-vacation-error"]').exists()).toBe(true);
    expect(w.find('[data-testid="my-vacation-balance"]').exists()).toBe(false);
  });
});

/**
 * MobileTimeclockView — the "This Week" section (the surface techs actually
 * see: /timeclock redirects to /mobile/timeclock on phones, so a desktop-only
 * timesheet would be invisible to its audience).
 *
 * Week machinery is pinned in useWeeklyTimesheet.spec.js and the dialog in
 * TimeEntryDialog.spec.js; here: the section renders with a break-netted
 * total, the whole entry row is the tap target, and it opens the shared
 * dialog in selfMode.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
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
import TimeEntryDialog from '../../components/TimeEntryDialog.vue';

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text', 'rounded', 'size', 'outlined'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  DatePicker: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input />' },
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  InputText: { props: ['value'], template: '<input />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<select />' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Textarea: { props: ['modelValue'], emits: ['update:modelValue'], template: '<textarea />' },
};

function todayAt(h) {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate(), h).toISOString();
}

const WEEK_FIXTURE = [
  {
    id: 'm1', technician_id: 'me',
    clock_in_at: todayAt(8), clock_out_at: todayAt(16),
    minutes: 480, break_minutes: 60, entry_type: 'manual', notes: 'added by me',
  },
];

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiPatch.mockReset();
  tenantTz.value = null;
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/timeclock/status')) return Promise.resolve({ clocked_in: false });
    if (url.startsWith('/api/timeclock/entries')) {
      return Promise.resolve(url.includes('include_breaks=true') ? WEEK_FIXTURE : []);
    }
    if (url.startsWith('/api/me/timezone')) return Promise.resolve({ tenant_timezone: null });
    return Promise.resolve([]);
  });
});

async function mountView() {
  const w = mount(MobileTimeclockView, {
    global: { stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return w;
}

describe('MobileTimeclockView — This Week', () => {
  it('renders the section with a break-netted week total', async () => {
    const w = await mountView();
    expect(w.find('[data-test="mt-week-section"]').exists()).toBe(true);
    // 480 gross − 60 break = 7.00h
    expect(w.find('[data-test="mt-week-total"]').text()).toContain('7.00h');
    expect(w.find('[data-test="mt-week-total"]').text()).toContain('1.00h breaks');
  });

  it('marks a manual entry as such', async () => {
    const w = await mountView();
    const row = w.find('[data-test="mt-week-entry"]');
    expect(row.text()).toContain('manual');
  });

  it('opens the shared dialog in selfMode when the row is tapped', async () => {
    const w = await mountView();
    await w.find('[data-test="mt-week-entry"]').trigger('click');
    await flushPromises();
    const dialog = w.findComponent(TimeEntryDialog);
    expect(dialog.props('selfMode')).toBe(true);
    expect(dialog.props('entry').id).toBe('m1');
    expect(w.find('[data-testid="entry-dialog"]').exists()).toBe(true);
  });

  it('offers "Add missed shift" anchored on yesterday', async () => {
    const w = await mountView();
    await w.find('[data-test="mt-add-entry"]').trigger('click');
    await flushPromises();
    const dialog = w.findComponent(TimeEntryDialog);
    expect(dialog.props('entry')).toBeNull();
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    expect(dialog.props('anchorDate').getDate()).toBe(yesterday.getDate());
  });

  it('cannot browse into a future week', async () => {
    const w = await mountView();
    expect(w.find('[data-test="mt-week-next"]').attributes('disabled')).toBeDefined();
  });
});

/**
 * Mobile dispatch board — a finished job that was never scheduled sits on
 * the day it was completed (2026-10-15).
 *
 * The board puts an undated job on today, because an undated job is a lead
 * waiting for a time. A completed job with no date is not a lead, and it
 * filled today's tech sections every day (16 such jobs on prod). This mounts
 * the REAL view — the MH-6 spec re-declares its helpers, which cannot catch
 * a change to the view's own copy.
 */
import { describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const TODAY = new Date();
const pad = (n) => String(n).padStart(2, '0');
const localKey = (v) => {
  const d = new Date(v);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const atDay = (offset, h) =>
  new Date(TODAY.getFullYear(), TODAY.getMonth(), TODAY.getDate() + offset, h, 0, 0).toISOString();

const undated = (id, extra) => ({
  id,
  title: id,
  customer_name: `Customer ${id}`,
  technician_id: 'tech-1',
  scheduled_at: null,
  ...extra,
});

const JOBS = [
  undated('done-today', { status: 'Completed', lifecycle_stage: 'completed', completed_at: atDay(0, 14) }),
  undated('done-last-week', { status: 'Completed', lifecycle_stage: 'completed', completed_at: atDay(-7, 14) }),
  undated('done-no-time', { status: 'Completed', lifecycle_stage: 'completed', completed_at: null }),
  // Finished by stage although its status string says otherwise (a QB-era label).
  undated('done-by-stage', { status: 'Service Call', lifecycle_stage: 'completed', completed_at: atDay(-3, 10) }),
  undated('live-lead', { status: 'New', lifecycle_stage: 'new' }),
];

const get = vi.fn((url) => {
  if (url.startsWith('/api/jobs?')) return Promise.resolve({ items: JOBS });
  if (url === '/api/technicians') return Promise.resolve([{ id: 'tech-1', name: 'Tech One', active: true }]);
  return Promise.resolve([]);
});

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: (...a) => get(...a), post: vi.fn().mockResolvedValue({}), patch: vi.fn().mockResolvedValue({}) }),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ tenantTimezone: { value: 'UTC' }, zonedDateKey: localKey }),
}));

describe('mobile dispatch board — finished undated jobs', () => {
  it('shows today only the finished job completed today, beside the live lead', async () => {
    const { default: MobileDispatchView } = await import('../MobileDispatchView.vue');
    const wrapper = mount(MobileDispatchView, {
      global: {
        directives: { tooltip: {} },
        stubs: {
          Button: true, Dialog: true, SelectButton: true, Select: true,
          InputText: true, DatePicker: true, Tag: true, MobileChatDialog: true,
        },
      },
    });
    await flushPromises();

    const section = wrapper.findAll('.bd-section').find((s) => s.text().includes('Tech One'));
    expect(section).toBeTruthy();
    await section.find('.tech-header').trigger('click');
    const text = section.text();

    expect(text).toContain('Customer done-today');
    expect(text).toContain('Customer live-lead');
    expect(text).not.toContain('Customer done-last-week');
    expect(text).not.toContain('Customer done-no-time');
    expect(text).not.toContain('Customer done-by-stage');
    expect(section.find('.bd-count').text()).toBe('2');
  });
});

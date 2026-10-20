/**
 * Mobile dispatch board — the badge reads the labels /api/jobs really sends
 * (GDXA-1942).
 *
 * The list sends status as jobs._STATUS_CANON's label ("In Progress",
 * "Complete", "Cancelled"); the board's table was keyed on the enum, so an
 * under-way job showed "pending" in amber. This mounts the REAL view with
 * those labels, as MobileDispatchFinishedJobDay.spec.js does.
 */
import { describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const TODAY = new Date();
const pad = (n) => String(n).padStart(2, '0');
const localKey = (v) => {
  const d = new Date(v);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const at = (h) => new Date(TODAY.getFullYear(), TODAY.getMonth(), TODAY.getDate(), h, 0, 0).toISOString();

// Every label jobs._STATUS_CANON emits, as the list sends it: status and
// lifecycle_stage both carry the label.
const job = (id, label, extra) => ({
  id,
  title: id,
  customer_name: `Customer ${id}`,
  scheduled_at: at(9),
  status: label,
  lifecycle_stage: label,
  ...extra,
});
const TECH = { technician_id: 'tech-1' };
const JOBS = [
  job('lead', 'Lead'),
  job('service-call', 'Service Call'),
  job('estimate', 'Estimate'),
  job('scheduled', 'Scheduled'),
  job('under-way', 'In Progress', TECH),
  job('finished', 'Complete', { ...TECH, completed_at: at(12) }),
  job('called-off', 'Cancelled', { ...TECH, completed_at: at(12) }),
  job('qb', 'QB Import'),
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

async function mountBoard() {
  const { default: MobileDispatchView } = await import('../MobileDispatchView.vue');
  const wrapper = mount(MobileDispatchView, {
    global: {
      directives: { tooltip: {} },
      stubs: {
        Button: true, Dialog: true, SelectButton: true, Select: true,
        InputText: true, DatePicker: true, MobileChatDialog: true,
        Tag: { props: ['value', 'severity'], template: '<span class="tag" :data-severity="severity">{{ value }}</span>' },
      },
    },
  });
  await flushPromises();
  return wrapper;
}

function badgeOf(scope, id) {
  const card = scope.findAll('.job-card').find((c) => c.text().includes(`Customer ${id}`));
  expect(card, id).toBeTruthy();
  const tag = card.find('.tag');
  return { value: tag.text(), severity: tag.attributes('data-severity') };
}

describe('mobile dispatch board — status labels from /api/jobs', () => {
  it('unassigned cards: every open stage label renders as itself, unknown words as Pending', async () => {
    const w = await mountBoard();
    const section = w.findAll('.bd-section').find((s) => s.text().includes('Unassigned'));
    expect(badgeOf(section, 'lead')).toEqual({ value: 'Lead', severity: 'warn' });
    expect(badgeOf(section, 'service-call')).toEqual({ value: 'Service call', severity: 'warn' });
    expect(badgeOf(section, 'estimate')).toEqual({ value: 'Estimate', severity: 'warn' });
    expect(badgeOf(section, 'scheduled')).toEqual({ value: 'Scheduled', severity: 'warn' });
    // MH-6 still holds: a word that is no status reads the neutral default.
    expect(badgeOf(section, 'qb')).toEqual({ value: 'Pending', severity: 'warn' });
  });

  it('tech cards: In Progress is info, Complete success, Cancelled danger', async () => {
    const w = await mountBoard();
    const section = w.findAll('.bd-section').find((s) => s.text().includes('Tech One'));
    await section.find('.tech-header').trigger('click');
    expect(badgeOf(section, 'under-way')).toEqual({ value: 'In progress', severity: 'info' });
    expect(badgeOf(section, 'finished')).toEqual({ value: 'Completed', severity: 'success' });
    expect(badgeOf(section, 'called-off')).toEqual({ value: 'Cancelled', severity: 'danger' });
  });

  it('a Complete or Cancelled job with no tech stays out of the Unassigned queue', async () => {
    JOBS.push(
      job('orphan-done', 'Complete', { completed_at: at(12) }),
      job('orphan-cancelled', 'Cancelled', { completed_at: at(12) }),
    );
    try {
      const w = await mountBoard();
      const section = w.findAll('.bd-section').find((s) => s.text().includes('Unassigned'));
      expect(section.text()).not.toContain('Customer orphan-done');
      expect(section.text()).not.toContain('Customer orphan-cancelled');
      expect(section.text()).toContain('Customer scheduled');
    } finally {
      JOBS.splice(-2);
    }
  });
});

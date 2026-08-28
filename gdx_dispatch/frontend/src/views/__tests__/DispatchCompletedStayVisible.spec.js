/**
 * Dispatch board — finished work stays on the board (Doug, 2026-08-28).
 *
 * The report: "after a job is finished it disappears from the dispatch board,
 * there is no way to go back and look."
 *
 * Cause: rangeFilteredJobs dropped every complete/completed/invoiced job
 * unless `showCompleted` was on, and the only control that set it was a
 * "Show Completed Jobs" button parked inside `v-if="skillOptions.length"` —
 * a guard written for the skill Select sharing that row. Every technician on
 * the tenant had skills = NULL, so /api/technicians/skills returned [], the
 * row never rendered, and the button was unreachable from the day it shipped.
 * The filter was therefore permanently on with no way to turn it off.
 *
 * This mounts the REAL DispatchView against the exact API shape that broke it
 * (skills: []) — the previous dispatch specs all re-implement a computed in an
 * isolated Host and then check the view by source text, which can only prove
 * that a line was written, never that a completed card reaches the screen.
 *
 * Pinned:
 *  1. A completed job assigned to a tech renders in that tech's column.
 *  2. It renders when the skill row is absent (skills: []) — the prod shape.
 *  3. It counts toward the tech's job badge and capacity hours.
 *  4. Completed jobs stay OUT of the "New Jobs to Schedule" intake queue,
 *     which is an action list, not history.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia } from 'pinia';

const TODAY = new Date();
const pad = (n) => String(n).padStart(2, '0');
const TODAY_KEY = `${TODAY.getFullYear()}-${pad(TODAY.getMonth() + 1)}-${pad(TODAY.getDate())}`;
// Midday local, so the tenant-zone bucket can't slip to an adjacent day.
const at = (h) => new Date(TODAY.getFullYear(), TODAY.getMonth(), TODAY.getDate(), h, 0, 0).toISOString();

// effective_workdays 127 = every day of the week, so this spec does not go red
// on a Saturday; without shift hours the column renders "Off today" and the
// capacity assertion has nothing to measure.
const TECH = {
  id: 'tech-1',
  name: 'Michael Tallman',
  active: true,
  skills: [],
  effective_workdays: 127,
  effective_shift_start: '07:00',
  effective_shift_end: '17:00',
};

const JOB_DONE = {
  id: 'job-done',
  job_number: 'JOB-2026-054',
  title: 'Broken spring — finished',
  status: 'Complete',
  lifecycle_stage: 'completed',
  technician_id: 'tech-1',
  assigned_tech_ids: ['tech-1'],
  scheduled_at: at(9),
  scheduled_duration_hours: 2,
  effective_duration_hours: 2,
  customer_name: 'A Customer',
  holding_area_id: null,
};

const JOB_OPEN = {
  id: 'job-open',
  job_number: 'JOB-2026-055',
  title: 'Opener swap — still open',
  status: 'Scheduled',
  lifecycle_stage: 'scheduled',
  technician_id: 'tech-1',
  assigned_tech_ids: ['tech-1'],
  scheduled_at: at(13),
  scheduled_duration_hours: 2,
  effective_duration_hours: 2,
  customer_name: 'B Customer',
  holding_area_id: null,
};

// An undated, un-teched completed job — the 189-row shape on prod. It must
// NOT surface in the intake queue, which is a list of work still owed a
// decision. (matchesDate treats undated jobs as matching whatever day is
// selected, so an unfiltered queue would redraw these on every date.)
const JOB_DONE_UNDATED = {
  id: 'job-done-undated',
  job_number: 'JOB-2025-001',
  title: 'Old import — finished, no date',
  status: 'Complete',
  lifecycle_stage: 'completed',
  technician_id: null,
  assigned_tech_ids: [],
  scheduled_at: null,
  holding_area_id: null,
};

const get = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: (...a) => get(...a),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  }),
}));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({ hasPermission: () => true }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({
    tenantTimezone: { value: 'UTC' },
    // Bucket by the LOCAL calendar day, matching the fixtures above.
    zonedDateKey: (v) => {
      const d = new Date(v);
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    },
  }),
}));
vi.mock('../../composables/usePollingRefresh', () => ({
  usePollingRefresh: () => ({ pause: vi.fn(), resume: vi.fn() }),
  shouldPoll: () => false,
}));

function routeApi(jobs) {
  get.mockImplementation((url) => {
    if (url.startsWith('/api/jobs?')) return Promise.resolve(jobs);
    if (url === '/api/technicians') return Promise.resolve([TECH]);
    // THE PROD SHAPE: no technician carries skills, so this is empty and the
    // skill-filter row does not render. The completed cards must not depend
    // on that row existing.
    if (url === '/api/technicians/skills') return Promise.resolve({ skills: [] });
    if (url === '/api/holding-areas') return Promise.resolve([]);
    if (url === '/api/dispatch-settings') return Promise.resolve({});
    if (url.startsWith('/api/dispatch/scheduled-unassigned')) return Promise.resolve({ items: [] });
    return Promise.resolve([]);
  });
}

async function mountBoard(jobs) {
  routeApi(jobs);
  const { default: DispatchView } = await import('../DispatchView.vue');
  const wrapper = mount(DispatchView, {
    attachTo: document.body,
    global: {
      // TechEfficiencyPanel reaches for useApi -> useAuthStore directly.
      plugins: [createPinia()],
      directives: { tooltip: {} },
    },
  });
  await flushPromises();
  await flushPromises();
  return wrapper;
}

describe('dispatch board keeps finished jobs visible', () => {
  beforeEach(() => {
    get.mockReset();
    vi.resetModules();
  });

  it('renders a completed job in its technician column', async () => {
    const wrapper = await mountBoard([JOB_DONE, JOB_OPEN]);

    // The regression: this card was absent, and no control could bring it back.
    expect(wrapper.find('[data-testid="timeline-job-job-done"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="timeline-job-job-open"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('Broken spring — finished');
  });

  it('does it with NO skill-filter row on screen (the shape that broke it)', async () => {
    const wrapper = await mountBoard([JOB_DONE, JOB_OPEN]);

    // Row absent — every tech has skills = NULL, exactly like prod.
    expect(wrapper.find('.skill-filter-row').exists()).toBe(false);
    // ...and the completed card is on screen anyway.
    expect(wrapper.find('[data-testid="timeline-job-job-done"]').exists()).toBe(true);
    // The toggle that used to hide in that row is gone for good; if it ever
    // comes back it must not be what makes finished work visible.
    expect(wrapper.find('[data-testid="dispatch-toggle-completed"]').exists()).toBe(false);
  });

  it('counts the finished job in the tech badge and capacity hours', async () => {
    const wrapper = await mountBoard([JOB_DONE, JOB_OPEN]);
    const col = wrapper.find('[data-testid="tech-column-tech-1"]');

    expect(col.exists()).toBe(true);
    // 4h of work on the day, not 2h — a tech who finished the morning job
    // should not read as a half-empty day.
    expect(wrapper.find('[data-testid="tech-capacity-tech-1"]').text()).toContain('4h');
  });

  it('keeps completed jobs OUT of the New Jobs to Schedule queue', async () => {
    const wrapper = await mountBoard([JOB_DONE, JOB_OPEN, JOB_DONE_UNDATED]);

    // Visible on the board as history, never in the queue of work still owed
    // a scheduling decision.
    expect(wrapper.text()).not.toContain('Old import — finished, no date');
  });
});

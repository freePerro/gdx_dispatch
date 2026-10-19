/**
 * The mobile dispatch board books a job into a tech's day through the same
 * overflow check as the desktop board (GDXA-1686).
 *
 * Assign on an undated job booked it at 09:00 and the quick-add booked it at
 * the picked time, both with no look at the tech's shift, so an 8 h job
 * landed past the end of the day with no warning. Mounts the real view and
 * the real DayOverflowDialog.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const TODAY = new Date();
const pad = (n) => String(n).padStart(2, '0');
const localKey = (v) => {
  const d = new Date(v);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const todayAt = (h, m = 0) => new Date(TODAY.getFullYear(), TODAY.getMonth(), TODAY.getDate(), h, m);

const UNDATED_8H = {
  id: 'job-1', title: 'Opener install', customer_name: 'Acme', status: 'New',
  scheduled_at: null, scheduled_duration_hours: 8, assigned_tech_ids: [], holding_area_id: 'lane-1',
};
// As the list sends it: the stage label, not the raw value.
const UNDER_WAY = { ...UNDATED_8H, id: 'job-2', lifecycle_stage: 'In Progress', status: 'In Progress' };
let techs;
const short = { id: 't1', name: 'Mike', effective_shift_start: '08:00', effective_shift_end: '12:00', effective_workdays: 127 };
const full = { ...short, effective_shift_start: '07:00', effective_shift_end: '17:00' };

const patch = vi.fn().mockResolvedValue({});
const post = vi.fn((url) => Promise.resolve(url === '/api/jobs' ? { id: 'new-1' } : {}));
const get = vi.fn((url) => {
  if (url.startsWith('/api/jobs?')) return Promise.resolve({ items: [UNDATED_8H, UNDER_WAY] });
  if (url === '/api/technicians') return Promise.resolve(techs);
  if (url.startsWith('/api/customers')) return Promise.resolve([{ id: 'c1', name: 'Acme' }]);
  return Promise.resolve([]);
});

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: (...a) => get(...a), post: (...a) => post(...a), patch: (...a) => patch(...a) }),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: (...a) => toastAdd(...a) }) }));
// No shop zone: wall clock is the test's own local time, as written.
vi.mock('../../composables/useTenantTimezone', async (orig) => ({
  ...(await orig()),
  useTenantTimezone: () => ({ tenantTimezone: { value: null }, zonedDateKey: localKey }),
}));

async function mountBoard() {
  const { default: MobileDispatchView } = await import('../MobileDispatchView.vue');
  const w = mount(MobileDispatchView, {
    global: {
      directives: { tooltip: {} },
      stubs: {
        Button: { props: ['label'], template: '<button v-bind="$attrs">{{ label }}</button>' },
        Dialog: { props: ['visible'], template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /><slot name="footer" /></div>', inheritAttrs: false },
        SelectButton: true, Select: true, InputText: true, DatePicker: true, Tag: true, MultiSelect: true, MobileChatDialog: true,
      },
    },
  });
  await flushPromises();
  return w;
}

async function assign(w, job, techId) {
  const s = w.vm.$.setupState;
  s.openAssignDialog(job);
  s.assignDialogTechId = techId;
  s.confirmAssign();
  await flushPromises();
}

async function createJob(w, { at, hours, techId = 't1' }) {
  const s = w.vm.$.setupState;
  await s.openNewJob();
  s.newJobForm = { customer_id: 'c1', title: 'New opener', scheduled_at: at, assigned_tech_id: techId, hours: String(hours) };
  s.submitNewJob();
  await flushPromises();
}

const prompt = (w) => w.find('[data-testid="day-overflow-prompt"]');
const jobPosts = () => post.mock.calls.filter(([url]) => url === '/api/jobs');

beforeEach(() => { patch.mockReset(); patch.mockResolvedValue({}); post.mockClear(); toastAdd.mockClear(); techs = [short]; });

describe('mobile dispatch Assign on an undated job', () => {
  it('asks before an 8 h job lands past the end of the tech\'s day; Cancel sends no PATCH', async () => {
    const w = await mountBoard();
    await assign(w, UNDATED_8H, 't1');
    expect(prompt(w).exists()).toBe(true);
    expect(prompt(w).find('[data-testid="day-overflow-summary"]').text()).toContain('3 h left');
    await prompt(w).find('[data-testid="day-overflow-cancel"]').trigger('click');
    await flushPromises();
    expect(patch).not.toHaveBeenCalled();
  });

  it('keeping it on the day books the crew the prompt names, at 09:00', async () => {
    const w = await mountBoard();
    await assign(w, UNDATED_8H, 't1');
    await prompt(w).find('[data-testid="day-overflow-one-day"]').trigger('click');
    await flushPromises();
    expect(patch).toHaveBeenCalledTimes(1);
    const [url, body] = patch.mock.calls[0];
    expect(url).toBe('/api/jobs/job-1');
    expect(body).toEqual({
      scheduled_at: todayAt(9).toISOString(), assigned_tech_ids: ['t1'], lead_tech_id: 't1', holding_area_id: null,
    });
  });

  it('a job that fits books as it did, with no prompt', async () => {
    techs = [full];
    const w = await mountBoard();
    await assign(w, UNDATED_8H, 't1');
    expect(prompt(w).exists()).toBe(false);
    expect(patch).toHaveBeenCalledWith('/api/jobs/job-1', expect.objectContaining({
      assigned_tech_id: 't1', scheduled_at: todayAt(9).toISOString(), holding_area_id: null,
    }));
  });

  it('a job under way keeps its crew and lane: no prompt, as on the desktop board', async () => {
    const w = await mountBoard();
    await assign(w, UNDER_WAY, 't1');
    expect(prompt(w).exists()).toBe(false);
    const body = patch.mock.calls[0][1];
    expect(body.scheduled_at).toBe(todayAt(9).toISOString());
    expect('holding_area_id' in body).toBe(false);
  });
});

describe('mobile dispatch quick-add with a tech', () => {
  it('asks before an 8 h job at 3:45 PM; Cancel creates nothing and keeps the form', async () => {
    techs = [full];
    const w = await mountBoard();
    await createJob(w, { at: todayAt(15, 45), hours: 8 });
    expect(prompt(w).exists()).toBe(true);
    expect(prompt(w).find('[data-testid="day-overflow-summary"]').text()).toContain('1.25 h left');
    await prompt(w).find('[data-testid="day-overflow-cancel"]').trigger('click');
    await flushPromises();
    expect(jobPosts()).toHaveLength(0);
    expect(patch).not.toHaveBeenCalled();
    expect(w.vm.$.setupState.newJobOpen).toBe(true);
  });

  it('a split creates the job, books its first day as a visit of what is left, and the rest as visits', async () => {
    techs = [full];
    const w = await mountBoard();
    await createJob(w, { at: todayAt(15, 45), hours: 8 });
    await prompt(w).find('[data-testid="day-overflow-split"]').trigger('click');
    await flushPromises();
    expect(jobPosts()).toHaveLength(1);
    const created = jobPosts()[0][1];
    expect(created.scheduled_at).toBeUndefined();
    expect(created).toMatchObject({ assigned_tech_ids: ['t1'], lead_tech_id: 't1', scheduled_duration_hours: 8 });
    expect(patch).toHaveBeenCalledWith('/api/jobs/new-1', {
      scheduled_at: todayAt(15, 45).toISOString(), assigned_tech_ids: ['t1'], lead_tech_id: 't1',
      visit_duration_minutes: 75, holding_area_id: null,
    }, { suppressErrorToast: true });
    const visits = post.mock.calls.filter(([url]) => url === '/api/jobs/new-1/visits');
    expect(visits).toHaveLength(1);
    expect(visits[0][1]).toMatchObject({ start_time: '07:00', duration_minutes: 405, tech_ids: ['t1'] });
    expect(visits[0][1].days).toHaveLength(1);
  });

  it('a split whose booking is refused says the job was created but not booked, and not "Job created"', async () => {
    techs = [full];
    patch.mockRejectedValueOnce(new Error('Refused'));
    const w = await mountBoard();
    await createJob(w, { at: todayAt(15, 45), hours: 8 });
    await prompt(w).find('[data-testid="day-overflow-split"]').trigger('click');
    await flushPromises();
    const summaries = toastAdd.mock.calls.map(([t]) => t.summary);
    expect(summaries).toContain('Acme was created but not booked');
    expect(summaries).not.toContain('Job created');
    expect(post.mock.calls.some(([url]) => url.endsWith('/visits'))).toBe(false);
  });

  it('a job with no hours has nothing to measure and creates as it did', async () => {
    techs = [short];
    const w = await mountBoard();
    await createJob(w, { at: todayAt(11, 30), hours: '' });
    expect(prompt(w).exists()).toBe(false);
    expect(jobPosts()).toHaveLength(1);
    expect(jobPosts()[0][1]).toMatchObject({ assigned_tech_id: 't1', scheduled_at: todayAt(11, 30).toISOString() });
    expect(jobPosts()[0][1].scheduled_duration_hours).toBeUndefined();
  });
});

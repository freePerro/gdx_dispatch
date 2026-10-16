/**
 * Mobile dispatch Reassign on a crew job keeps the crew (2026-10-15).
 *
 * The jobs PATCH reads a single `assigned_tech_id` as the whole crew, and
 * Reassign sent exactly that, so saving any pick on a two-tech job cut it to
 * one tech — even saving the pre-selected lead unchanged. Mounts the real
 * view and the real CrewChangeDialog.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const TODAY = new Date();
const pad = (n) => String(n).padStart(2, '0');
const localKey = (v) => {
  const d = new Date(v);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const at9 = new Date(TODAY.getFullYear(), TODAY.getMonth(), TODAY.getDate(), 9).toISOString();

const CREW_JOB = {
  id: 'job-1', title: 'Opener install', customer_name: 'Acme', status: 'Scheduled',
  scheduled_at: at9, technician_id: 't1', assigned_to: 't1',
  assigned_tech_ids: ['t1', 't2'], lead_tech_id: 't1',
};
const SOLO_JOB = { ...CREW_JOB, id: 'job-2', assigned_tech_ids: ['t1'] };
const TECHS = [{ id: 't1', name: 'Mike' }, { id: 't2', name: 'Sam' }, { id: 't3', name: 'Pat' }];

const patch = vi.fn().mockResolvedValue({});
const get = vi.fn((url) => {
  if (url.startsWith('/api/jobs?')) return Promise.resolve({ items: [CREW_JOB, SOLO_JOB] });
  if (url === '/api/technicians') return Promise.resolve(TECHS);
  return Promise.resolve([]);
});

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: (...a) => get(...a), post: vi.fn().mockResolvedValue({}), patch: (...a) => patch(...a) }),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
const confirmMock = vi.fn().mockResolvedValue(true);
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: (...a) => confirmMock(...a), confirmDestructive: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ tenantTimezone: { value: 'UTC' }, zonedDateKey: localKey }),
}));

async function mountBoard() {
  const { default: MobileDispatchView } = await import('../MobileDispatchView.vue');
  const w = mount(MobileDispatchView, {
    global: {
      directives: { tooltip: {} },
      stubs: {
        Button: { props: ['label'], template: '<button v-bind="$attrs">{{ label }}</button>' },
        Dialog: { props: ['visible'], template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /><slot name="footer" /></div>', inheritAttrs: false },
        SelectButton: true, Select: true, InputText: true, DatePicker: true, Tag: true, MobileChatDialog: true,
      },
    },
  });
  await flushPromises();
  return w;
}

// Reassign `job` to `techId` the way the dialog does: open, pick, Assign.
async function reassign(w, job, techId) {
  const s = w.vm.$.setupState;
  s.openAssignDialog(job);
  s.assignDialogTechId = techId;
  const done = s.confirmAssign();
  await flushPromises();
  return { done }; // wrapped: returning the promise itself would wait on the prompt
}

beforeEach(() => { patch.mockClear(); confirmMock.mockClear(); confirmMock.mockResolvedValue(true); });

describe('mobile dispatch Reassign on a crew job', () => {
  it('saving the lead unchanged sends nothing', async () => {
    const w = await mountBoard();
    await reassign(w, CREW_JOB, 't1');
    expect(patch).not.toHaveBeenCalled();
  });

  it('picking another crew member makes them lead and keeps the crew', async () => {
    const w = await mountBoard();
    await reassign(w, CREW_JOB, 't2');
    expect(patch).toHaveBeenCalledWith('/api/jobs/job-1', { assigned_tech_ids: ['t1', 't2'], lead_tech_id: 't2' });
  });

  it('picking an outsider asks, and Add keeps everyone', async () => {
    const w = await mountBoard();
    const { done } = await reassign(w, CREW_JOB, 't3');
    expect(patch).not.toHaveBeenCalled();
    expect(w.get('[data-testid="crew-change-prompt"]').text()).toContain('Acme has a crew: Mike, Sam');
    await w.get('[data-testid="crew-change-add"]').trigger('click');
    await done;
    expect(patch).toHaveBeenCalledWith('/api/jobs/job-1', { assigned_tech_ids: ['t1', 't2', 't3'], lead_tech_id: 't1' });
  });

  it('Swap replaces only that member', async () => {
    const w = await mountBoard();
    const { done } = await reassign(w, CREW_JOB, 't3');
    await w.get('[data-testid="crew-change-swap-t2"]').trigger('click');
    await done;
    expect(patch).toHaveBeenCalledWith('/api/jobs/job-1', { assigned_tech_ids: ['t1', 't3'], lead_tech_id: 't1' });
  });

  it('Cancel changes nothing', async () => {
    const w = await mountBoard();
    const { done } = await reassign(w, CREW_JOB, 't3');
    await w.get('[data-testid="crew-change-cancel"]').trigger('click');
    await done;
    expect(patch).not.toHaveBeenCalled();
    expect(w.find('[data-testid="crew-change-prompt"]').exists()).toBe(false);
  });

  it('a one-tech job still reassigns straight to the pick', async () => {
    const w = await mountBoard();
    await reassign(w, SOLO_JOB, 't3');
    expect(patch).toHaveBeenCalledWith('/api/jobs/job-2', { assigned_tech_id: 't3', assigned_to: 't3', technician_id: 't3' });
  });
});

describe('mobile dispatch Unassign', () => {
  it('asks first, naming the crew, then takes everyone off', async () => {
    const w = await mountBoard();
    await w.vm.$.setupState.unassignJob(CREW_JOB);
    expect(confirmMock).toHaveBeenCalledWith(expect.objectContaining({
      header: 'Remove every technician?',
      message: expect.stringContaining('Mike, Sam'),
    }));
    expect(patch).toHaveBeenCalledWith('/api/jobs/job-1', { assigned_tech_id: null, assigned_to: null, technician_id: null });
  });

  it('saying no sends nothing', async () => {
    confirmMock.mockResolvedValue(false);
    const w = await mountBoard();
    await w.vm.$.setupState.unassignJob(SOLO_JOB);
    expect(confirmMock).toHaveBeenCalledWith(expect.objectContaining({ header: 'Remove the technician?' }));
    expect(patch).not.toHaveBeenCalled();
  });
});

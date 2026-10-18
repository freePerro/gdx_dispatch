/**
 * JobsView edit dialog — an unread crew is never sent (GDXA-1499).
 *
 * The dialog opened with assigned_tech_ids: [] and filled it from
 * GET /api/jobs/{id}/assignments. The server takes assigned_tech_ids as the
 * whole crew, so a Save before that read landed emptied the crew, and a Save
 * after a failed read (which fell back to the row's one primary tech) cut a
 * crew of three to one. Save now waits for the read, and after a failed read
 * the crew fields are left out of the PATCH.
 *
 * Mounts the real view with the API mocked (the ?edit= deep link opens the
 * dialog, as in JobsViewEditDeepLink.spec.js).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPatch = vi.fn();
const route = { query: {}, path: '/jobs' };

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    patch: apiPatch,
    post: vi.fn(),
    del: vi.fn(),
  }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({ hasPermission: () => true }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import JobsView from '../JobsView.vue';

const JOB_ID = '11111111-2222-3333-4444-555555555555';
const CREW = [
  { tech_id: 'tech-1', is_lead: false },
  { tech_id: 'tech-2', is_lead: true },
  { tech_id: 'tech-3', is_lead: false },
];

function job() {
  return {
    id: JOB_ID,
    job_number: 'J-9001',
    title: 'Opener install',
    customer_id: 'cust-1',
    customer_name: 'Pat Customer',
    job_type: 'Service Call',
    priority: 'Normal',
    status: 'Scheduled',
    lifecycle_stage: 'Scheduled',
    scheduled_at: '2026-10-20T14:30:00.000Z',
    assigned_tech_id: 'tech-2',
    assigned_to: 'tech-2',
    notes: '',
  };
}

// `assignments` is what the crew read does: a value, a thrown error, or a
// promise the test settles itself.
function wireApi(assignments) {
  apiGet.mockImplementation(async (url) => {
    if (url.startsWith('/api/jobs?')) return { items: [], total: 0 };
    if (url === `/api/jobs/${JOB_ID}`) return job();
    if (url === `/api/jobs/${JOB_ID}/assignments`) {
      if (assignments instanceof Error) throw assignments;
      return assignments;
    }
    if (url.startsWith('/api/technicians')) {
      return ['tech-1', 'tech-2', 'tech-3'].map((id) => ({ id, name: id }));
    }
    return [];
  });
}

const MOUNT_GLOBAL = {
  stubs: { teleport: true, CatalogPickerDialog: true, PhoneInput: true, JobStateChip: true },
  directives: { tooltip: {} },
};

function saveButton(wrapper) {
  return wrapper.find('[data-testid="job-submit-btn"]');
}

describe('JobsView edit dialog sends only a crew it has read', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPatch.mockReset();
    apiPatch.mockResolvedValue({ id: JOB_ID });
    route.query = { edit: JOB_ID };
  });

  it('Save is disabled and sends nothing while the crew read is pending', async () => {
    let settle;
    wireApi(new Promise((resolve) => { settle = resolve; }));
    const wrapper = mount(JobsView, { global: MOUNT_GLOBAL });
    await flushPromises();

    expect(wrapper.find('[data-testid="job-crew-loading"]').exists()).toBe(true);
    expect(saveButton(wrapper).attributes('disabled')).toBeDefined();
    await wrapper.vm.submitForm();
    await flushPromises();
    expect(apiPatch).not.toHaveBeenCalled();

    // Control: once the read lands, Save sends the whole crew it read.
    settle(CREW);
    await flushPromises();
    expect(saveButton(wrapper).attributes('disabled')).toBeUndefined();
    await wrapper.vm.submitForm();
    await flushPromises();
    expect(apiPatch).toHaveBeenCalledTimes(1);
    const [, payload] = apiPatch.mock.calls[0];
    expect(payload.assigned_tech_ids).toEqual(['tech-1', 'tech-2', 'tech-3']);
    expect(payload.lead_tech_id).toBe('tech-2');
  });

  it('after a failed crew read the save leaves the crew out of the PATCH', async () => {
    wireApi(new Error('403'));
    const wrapper = mount(JobsView, { global: MOUNT_GLOBAL });
    await flushPromises();

    expect(wrapper.find('[data-testid="job-crew-unread"]').exists()).toBe(true);
    expect(saveButton(wrapper).attributes('disabled')).toBeUndefined();
    await wrapper.vm.submitForm();
    await flushPromises();
    expect(apiPatch).toHaveBeenCalledTimes(1);
    const [, payload] = apiPatch.mock.calls[0];
    // The one-tech fallback the picker shows is not sent as the crew.
    expect(payload).not.toHaveProperty('assigned_tech_ids');
    expect(payload).not.toHaveProperty('lead_tech_id');
    expect(payload).not.toHaveProperty('assigned_tech_id');
    // The rest of the edit still saves.
    expect(payload.title).toBe('Opener install');
  });

  it('a job read with no crew rows sends its legacy primary tech back unchanged', async () => {
    wireApi([]);
    const wrapper = mount(JobsView, { global: MOUNT_GLOBAL });
    await flushPromises();
    await wrapper.vm.submitForm();
    await flushPromises();
    const [, payload] = apiPatch.mock.calls[0];
    expect(payload.assigned_tech_ids).toEqual(['tech-2']);
  });
});

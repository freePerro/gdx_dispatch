/**
 * JobsView ?edit=<id> deep link — saving must not clear the job's date
 * (GDXA-371).
 *
 * The list rows are mapped (scheduled_at → scheduledDate) and openEditDialog
 * read the mapped field. When the job was not in the loaded list, the deep
 * link fell back to GET /api/jobs/{id} and handed the RAW row to
 * openEditDialog: no scheduledDate, so the form opened dateless and the edit
 * PATCH sent scheduled_at: null. Edit mode always sends status, which turns
 * off the backend's date→stage sync, and the backend accepts null, so any
 * save silently dropped the job's date and its appointment.
 *
 * Mounts the real view (not a re-implementation) with the API mocked.
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

const FAR_ID = '11111111-2222-3333-4444-555555555555';
const SCHEDULED = '2026-10-20T14:30:00.000Z';

// The shape GET /api/jobs/{id} returns: scheduled_at, no scheduledDate.
function rawJob(overrides = {}) {
  return {
    id: FAR_ID,
    job_number: 'J-9001',
    title: 'Spring replacement',
    customer_id: 'cust-1',
    customer_name: 'Pat Customer',
    job_type: 'Service Call',
    priority: 'Normal',
    status: 'Scheduled',
    lifecycle_stage: 'Scheduled',
    lifecycle_stage_raw: 'scheduled',
    scheduled_at: SCHEDULED,
    scheduled_duration_hours: 2,
    location_id: null,
    assigned_tech_id: 'tech-1',
    notes: '',
    ...overrides,
  };
}

function wireApi(detail, { inList = false } = {}) {
  apiGet.mockImplementation(async (url) => {
    // By default the list does not hold the deep-linked job, so the fallback
    // must run; inList puts it on the list instead.
    if (url.startsWith('/api/jobs?')) return { items: inList ? [detail] : [], total: inList ? 1 : 0 };
    if (url === `/api/jobs/${FAR_ID}`) return detail;
    if (url === `/api/jobs/${FAR_ID}/assignments`) return [{ tech_id: 'tech-1', is_lead: true }];
    if (url.startsWith('/api/technicians')) return [{ id: 'tech-1', name: 'Tech One' }];
    return [];
  });
}

// Child components with their own API/store wiring are not under test.
const MOUNT_GLOBAL = {
  stubs: { teleport: true, CatalogPickerDialog: true, PhoneInput: true, JobStateChip: true },
  directives: { tooltip: {} },
};

async function mountAndSave({ viaFallback = true } = {}) {
  const wrapper = mount(JobsView, {
    global: MOUNT_GLOBAL,
  });
  await flushPromises();
  if (viaFallback) expect(apiGet).toHaveBeenCalledWith(`/api/jobs/${FAR_ID}`);
  else expect(apiGet).not.toHaveBeenCalledWith(`/api/jobs/${FAR_ID}`);
  await wrapper.vm.submitForm();
  await flushPromises();
  expect(apiPatch).toHaveBeenCalledTimes(1);
  const [url, payload] = apiPatch.mock.calls[0];
  expect(url).toBe(`/api/jobs/${FAR_ID}`);
  return { wrapper, payload };
}

describe('JobsView ?edit= deep link for a job outside the loaded list', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPatch.mockReset();
    apiPatch.mockResolvedValue({ id: FAR_ID });
    route.query = { edit: FAR_ID };
  });

  it('seeds the form with the job date and sends it back unchanged', async () => {
    wireApi(rawJob());
    const { wrapper, payload } = await mountAndSave();
    expect(wrapper.vm.jobForm.scheduled_at).toBeInstanceOf(Date);
    expect(payload.scheduled_at).toBe(SCHEDULED);
  });

  it('a dateless job is saved without scheduled_at at all, never null', async () => {
    wireApi(rawJob({ scheduled_at: null, status: 'Service Call', lifecycle_stage: 'Service Call' }));
    const { payload } = await mountAndSave();
    expect('scheduled_at' in payload).toBe(false);
  });

  it('a date the operator clears is still sent as null', async () => {
    wireApi(rawJob());
    const wrapper = mount(JobsView, {
      global: MOUNT_GLOBAL,
    });
    await flushPromises();
    wrapper.vm.jobForm.scheduled_at = null;
    await wrapper.vm.submitForm();
    await flushPromises();
    const [, payload] = apiPatch.mock.calls[0];
    expect(payload).toHaveProperty('scheduled_at', null);
  });

  // The list shows a QB-import title as the job type; that is display only.
  // Seeding the form from it made Save rename the job (audit of GDXA-371).
  it('keeps a QB-import title on save from the fallback', async () => {
    wireApi(rawJob({ title: 'QuickBooks Import — Smith' }));
    const { payload } = await mountAndSave();
    expect(payload.title).toBe('QuickBooks Import — Smith');
  });

  it('keeps a QB-import title on save from a list row', async () => {
    wireApi(rawJob({ title: 'QuickBooks Import — Smith' }), { inList: true });
    const { payload } = await mountAndSave({ viaFallback: false });
    expect(payload.title).toBe('QuickBooks Import — Smith');
    expect(payload.scheduled_at).toBe(SCHEDULED);
  });
});

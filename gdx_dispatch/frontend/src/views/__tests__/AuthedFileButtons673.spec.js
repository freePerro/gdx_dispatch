/**
 * Buttons that open an authenticated file send the bearer token (#673 sweep).
 *
 * The Segments "Export CSV" button was one instance of a shape: a control that
 * navigates a new tab straight at an /api route. A new tab carries no
 * Authorization header, and `get_current_user` reads only that header, so the
 * tab received 401 JSON. Probed against a throwaway container 2026-09-13, both
 * of these answered 401 to a cookie-only request and 200 with a bearer token:
 *
 *   - Job page "Install Sheet"  → GET /api/jobs/{id}/install-sheet
 *   - Reports "Export CSV"      → GET /api/reports/export
 *
 * and, by the same get_current_user dependency, the job page's file/signature
 * downloads (GET /api/documents/{id}/download) and Resources downloads
 * (GET /api/resources/{id}/download — the list never carries another URL).
 *
 * Each now goes through composables/useAuthedFile, which fetches with the
 * token. Server-generated files (the sheet, the CSVs) may open in a tab; files
 * someone UPLOADED are saved under their own name and never opened, because a
 * blob tab renders in the app's origin, where uploaded HTML could script the
 * session. Pinned here: the right helper, the right URL, never window.open,
 * and a failure is said out loud.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const toastAdd = vi.fn();
const openAuthedFile = vi.fn();
const downloadAuthedFile = vi.fn();

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'job-123' }, query: {}, path: '/jobs/job-123' }),
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
  useApiWithToast: () => ({ get: apiGet }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    del: vi.fn(),
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
  }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(), confirmDestructive: vi.fn() }),
}));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ user: { role: 'admin' }, hasPermission: () => true }),
}));
vi.mock('../../composables/useAuthedFile', () => ({
  openAuthedFile: (...a) => openAuthedFile(...a),
  downloadAuthedFile: (...a) => downloadAuthedFile(...a),
}));
vi.mock('vue-chartjs', () => ({
  Bar: { name: 'BarStub', props: ['data', 'options'], template: '<div />' },
  Pie: { name: 'PieStub', props: ['data', 'options'], template: '<div />' },
}));

const Button = {
  props: ['label', 'icon', 'severity', 'text', 'rounded', 'outlined', 'disabled', 'loading'],
  emits: ['click'],
  template: '<button v-bind="$attrs" @click="$emit(\'click\')">{{ label }}</button>',
};

let windowOpen;
beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  openAuthedFile.mockResolvedValue(undefined);
  downloadAuthedFile.mockResolvedValue(undefined);
  windowOpen = vi.spyOn(window, 'open').mockImplementation(() => null);
});
afterEach(() => windowOpen.mockRestore());

describe('Job page — Install Sheet', () => {
  async function mountJob() {
    apiGet.mockImplementation((url) => {
      if (url === '/api/jobs/job-123') {
        return Promise.resolve({ id: 'job-123', title: 'Spring', status: 'Scheduled', lifecycle_stage: 'scheduled' });
      }
      if (url.startsWith('/api/documents?job_id=')) {
        // DocumentOut's real shape: no entity_type, a real original_name.
        return Promise.resolve([{ id: 'doc-9', filename: 'x.bin', original_name: 'Door quote.xlsx', content_type: 'text/html' }]);
      }
      return Promise.resolve([]);
    });
    const View = (await import('../JobDetailView.vue')).default;
    const w = mount(View, { shallow: true, global: { stubs: { Button }, directives: { tooltip: {} } } });
    await flushPromises();
    return w;
  }

  it('opens the sheet with the token, not a tokenless tab', async () => {
    const w = await mountJob();
    await w.find('[data-testid="job-detail-install-sheet"]').trigger('click');
    await flushPromises();
    expect(openAuthedFile).toHaveBeenCalledWith('/api/jobs/job-123/install-sheet');
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it('saves an uploaded job file under its own name — never opens it', async () => {
    const w = await mountJob();
    w.vm.activeTab = 'email';
    await flushPromises();
    await w.find('[aria-label="Download file"]').trigger('click');
    await flushPromises();
    expect(downloadAuthedFile).toHaveBeenCalledWith('/api/documents/doc-9/download', 'Door quote.xlsx');
    expect(openAuthedFile).not.toHaveBeenCalled();
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it('says so when the sheet cannot be opened', async () => {
    openAuthedFile.mockRejectedValue(new Error('Failed to load file (403)'));
    const w = await mountJob();
    await w.find('[data-testid="job-detail-install-sheet"]').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error', summary: 'Install sheet failed' }));
  });
});

describe('Reports — Export CSV', () => {
  async function mountReports() {
    apiGet.mockImplementation((url) =>
      Promise.resolve(url.includes('summary') ? { revenue_total: 0 } : { items: [], totals: {} }),
    );
    const View = (await import('../ReportsView.vue')).default;
    const w = mount(View, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } });
    await flushPromises();
    await flushPromises();
    return w;
  }

  it('downloads the report with the token, under a real filename', async () => {
    const w = await mountReports();
    await w.find('[data-testid="reports-export-btn"]').trigger('click');
    await flushPromises();
    expect(downloadAuthedFile).toHaveBeenCalledTimes(1);
    const [url, filename] = downloadAuthedFile.mock.calls[0];
    expect(url).toBe('/api/reports/export?format=csv');
    expect(filename).toMatch(/^report-\d{4}-\d{2}-\d{2}\.csv$/);
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it('says so when the export fails', async () => {
    downloadAuthedFile.mockRejectedValue(new Error('Failed to load file (500)'));
    const w = await mountReports();
    await w.find('[data-testid="reports-export-btn"]').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error', summary: 'Export failed' }));
  });
});

describe('Resources — Download', () => {
  const RowScope = {
    props: ['row'],
    provide() {
      return { dtRow: this.row };
    },
    template: '<div><slot /></div>',
  };
  const stubs = {
    // Passes the real event: the download button uses @click.stop, and .stop
    // calls stopPropagation on whatever is emitted.
    Button: { ...Button, template: '<button v-bind="$attrs" @click="$emit(\'click\', $event)">{{ label }}</button>' },
    Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
    DataTable: {
      props: ['value'],
      components: { RowScope },
      template: '<div><RowScope v-for="(row, i) in (value || [])" :key="i" :row="row"><slot /></RowScope></div>',
    },
    Column: { inject: { dtRow: { default: null } }, template: '<span><slot name="body" :data="dtRow" /></span>' },
    // A full mount, not shallow: shallow would also stub RowScope above and
    // the row slot would never render.
    Tag: { template: '<span />' },
    Dialog: { template: '<div />' },
    Select: { template: '<div />' },
    InputText: { template: '<input />' },
    ProgressSpinner: { template: '<div />' },
    EmptyState: { template: '<div />' },
  };

  async function mountResources(rows) {
    apiGet.mockImplementation((url) => Promise.resolve(url === '/api/resources' ? rows : []));
    const View = (await import('../ResourcesView.vue')).default;
    const w = mount(View, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    return w;
  }

  it('saves a resource with the token — never opens it', async () => {
    const w = await mountResources([{ id: 'res-1', name: 'Spring SOP.pdf', category: 'sop', mime_type: 'text/html' }]);
    await w.find('[data-testid="resource-download-btn"]').trigger('click');
    await flushPromises();
    expect(downloadAuthedFile).toHaveBeenCalledWith('/api/resources/res-1/download', 'Spring SOP.pdf');
    expect(openAuthedFile).not.toHaveBeenCalled();
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it('says so when a resource cannot be downloaded', async () => {
    downloadAuthedFile.mockRejectedValue(new Error('Failed to load file (404)'));
    const w = await mountResources([{ id: 'res-1', name: 'Spring SOP.pdf', category: 'sop' }]);
    await w.find('[data-testid="resource-download-btn"]').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error', summary: 'Download failed' }));
  });
});

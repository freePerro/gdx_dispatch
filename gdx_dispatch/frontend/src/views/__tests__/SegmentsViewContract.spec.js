/**
 * SegmentsView — the segment editor speaks the API's contract (#455).
 *
 * Before this, the view sent `{name, criteria, tags}` to both writers.
 * `criteria` and `tags` are not columns on the segments table
 * (`id, name, rules, created_at, deleted_at`) and `rules` is required, so
 * create answered 422 and edit answered 405 — PATCH was not registered at
 * all. The segment chips also sent `/api/customers?segment_id=...`, which
 * nothing serves, so a chip silently re-loaded every customer.
 *
 * These are behavioral pins: they drive the real component and assert the
 * request that leaves it. A source-text presence test would pass on a view
 * that never issues the call.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRouter, createMemoryHistory } from 'vue-router';
import SegmentsView from '../SegmentsView.vue';

const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: vi.fn() }) }));

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));

// The view reads only `isAdmin` from the store; each test sets the role it needs.
const authState = { isAdmin: true };
vi.mock('../../stores/auth', () => ({ useAuthStore: () => authState }));

const downloadAuthedFile = vi.fn();
vi.mock('../../composables/useAuthedFile', () => ({
  downloadAuthedFile: (...args) => downloadAuthedFile(...args),
}));

const confirmAsync = vi.fn(() => Promise.resolve(true));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync }),
}));

const BUILTIN = {
  id: 'at-risk',
  name: 'At Risk',
  rules: { field: 'last_job_date', operator: 'older_than', value: '180 days' },
  is_builtin: true,
  created_at: null,
  matching_customer_count: 7,
};

const CUSTOM = {
  id: '11111111-2222-3333-4444-555555555555',
  name: 'Winback 90d',
  rules: {
    match: 'all',
    rules: [{ field: 'last_job_date', operator: 'older_than', value: '90 days' }],
  },
  is_builtin: false,
  created_at: '2026-09-01T10:00:00Z',
  matching_customer_count: 3,
};

const RowScope = {
  props: ['row'],
  provide() {
    return { dtRow: this.row };
  },
  template: '<td><slot /></td>',
};

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
  Column: {
    inject: { dtRow: { default: null } },
    template: '<span><slot name="body" :data="dtRow" /></span>',
  },
  ProgressSpinner: { template: '<div />' },
  EmptyState: { template: '<div />' },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input :data-testid="$attrs['data-testid']" :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)" />`,
    inheritAttrs: false,
  },
  // Renders its real options: a stub with an empty <select> can only ever
  // emit '', so the rule builder's own controls would never be driven.
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue', 'change'],
    computed: {
      opts() {
        return (this.options || []).map((o) =>
          typeof o === 'object' ? o[this.optionValue || 'value'] : o
        );
      },
    },
    template: `<select :data-testid="$attrs['data-testid']" :value="modelValue"
      @change="$emit('update:modelValue', $event.target.value); $emit('change')">
        <option v-for="o in opts" :key="o" :value="o">{{ o }}</option>
      </select>`,
    inheritAttrs: false,
  },
  // SegmentsView puts its controls inside <Column><template #body>, so the
  // stubs have to render column bodies per row. RowScope hands each row down
  // to the Columns rendered inside it.
  // A table bound with v-model:selection gets a select control per row, so a
  // test can pick rows the way a click in the real DataTable would.
  DataTable: {
    props: ['value', 'selection'],
    emits: ['row-click', 'update:selection'],
    components: { RowScope },
    template: `<table><tbody>
        <tr v-for="(row, i) in (value || [])" :key="i" class="dt-row"
            @click="$emit('row-click', { data: row })">
          <td v-if="selection !== undefined"><button class="dt-select"
            @click.stop="$emit('update:selection', [...selection, row])">pick</button></td>
          <RowScope :row="row"><slot /></RowScope>
        </tr>
      </tbody></table>`,
  },
  Dialog: {
    props: ['visible'],
    template: `<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>`,
  },
  Button: {
    props: ['label', 'loading', 'disabled'],
    emits: ['click'],
    // Pass the real event through: the table's Edit/Delete use @click.stop,
    // and the .stop modifier calls stopPropagation on whatever is emitted.
    template: `<button :data-testid="$attrs['data-testid']" :disabled="disabled"
      @click="$emit('click', $event)">{{ label }}<slot /></button>`,
    inheritAttrs: false,
  },
};

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/customers/:id', name: 'customer', component: { template: '<div />' } },
    ],
  });
}

async function mountView() {
  const router = makeRouter();
  const wrapper = mount(SegmentsView, {
    global: { plugins: [router], stubs },
  });
  await flushPromises();
  return wrapper;
}

describe('SegmentsView — API contract', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    apiPatch.mockReset();
    apiDel.mockReset();
    confirmAsync.mockClear();
    confirmAsync.mockResolvedValue(true);
    apiGet.mockImplementation((url) => {
      if (url === '/api/segments') {
        return Promise.resolve({ items: [BUILTIN, CUSTOM] });
      }
      if (url.startsWith('/api/segments/')) {
        return Promise.resolve({ items: [], total: 0 });
      }
      return Promise.resolve({ items: [] });
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it('creates a segment with `rules`, never `criteria` or `tags`', async () => {
    const wrapper = await mountView();

    await wrapper.find('[data-testid="segments-open-dialog"]').trigger('click');
    await flushPromises();

    await wrapper.find('[data-testid="segments-dialog-name"]').setValue('Dormant 120d');
    await wrapper.find('[data-testid="segments-rule-value-0"]').setValue('120');
    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, payload] = apiPost.mock.calls[0];
    expect(url).toBe('/api/segments');
    expect(payload).toHaveProperty('rules');
    expect(payload).not.toHaveProperty('criteria');
    expect(payload).not.toHaveProperty('tags');
    expect(payload.name).toBe('Dormant 120d');
    expect(payload.rules.match).toBe('all');
    expect(payload.rules.rules).toEqual([
      { field: 'last_job_date', operator: 'older_than', value: '120 days' },
    ]);
  });

  it('edits a custom segment with PATCH carrying `rules`', async () => {
    const wrapper = await mountView();

    await wrapper.find('[data-testid="segments-edit-row"]').trigger('click');
    await flushPromises();

    // The dialog round-trips the stored rule: "90 days" -> the number 90.
    expect(wrapper.find('[data-testid="segments-rule-value-0"]').element.value).toBe('90');

    await wrapper.find('[data-testid="segments-rule-value-0"]').setValue('45');
    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledTimes(1);
    const [url, payload] = apiPatch.mock.calls[0];
    expect(url).toBe(`/api/segments/${CUSTOM.id}`);
    expect(payload.rules.rules[0].value).toBe('45 days');
    expect(payload).not.toHaveProperty('criteria');
  });

  it('offers no edit or delete on a built-in, which the API answers 400', async () => {
    const wrapper = await mountView();

    // One custom row -> exactly one Edit and one Delete control on the table.
    expect(wrapper.findAll('[data-testid="segments-edit-row"]')).toHaveLength(1);
    expect(wrapper.findAll('[data-testid="segments-delete-row"]')).toHaveLength(1);

    // ...and clicking the built-in row does not open the editor.
    const rows = wrapper.findAll('tr.dt-row');
    await rows[0].trigger('click');
    await flushPromises();
    expect(wrapper.find('.dlg').exists()).toBe(false);
  });

  it('deletes a custom segment through the route that exists', async () => {
    const wrapper = await mountView();

    await wrapper.find('[data-testid="segments-delete-row"]').trigger('click');
    await flushPromises();

    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(apiDel).toHaveBeenCalledWith(
      `/api/segments/${CUSTOM.id}`,
      expect.objectContaining({ successMessage: expect.any(String) })
    );
  });

  it('a segment chip asks the endpoint that filters, not ?segment_id=', async () => {
    const wrapper = await mountView();
    apiGet.mockClear();

    const chips = wrapper.findAll('[data-testid^="segment-chip-"]');
    // chip 0 is "All customers"; chip 1 is the first segment.
    await chips[1].trigger('click');
    await flushPromises();

    const urls = apiGet.mock.calls.map(([u]) => u);
    expect(urls).toContain(`/api/segments/${BUILTIN.id}/customers`);
    expect(urls.some((u) => u.includes('segment_id='))).toBe(false);
  });

  it('switching a rule to a numeric field re-picks a legal operator and sends a number', async () => {
    const wrapper = await mountView();
    await wrapper.find('[data-testid="segments-open-dialog"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="segments-dialog-name"]').setValue('Big spenders');

    // older_than is illegal on lifetime_value — the API answers 422 for it.
    await wrapper.find('[data-testid="segments-rule-field-0"]').setValue('lifetime_value');
    await flushPromises();
    await wrapper.find('[data-testid="segments-rule-value-0"]').setValue('5000');
    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();

    const [, payload] = apiPost.mock.calls[0];
    expect(payload.rules.rules[0]).toEqual({
      field: 'lifetime_value',
      operator: 'greater_than',
      value: 5000,
    });
  });

  it('will not save a rule with a blank value — that would match every customer', async () => {
    const wrapper = await mountView();
    await wrapper.find('[data-testid="segments-open-dialog"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="segments-dialog-name"]').setValue('Oops');
    await wrapper.find('[data-testid="segments-rule-value-0"]').setValue('');
    await flushPromises();

    expect(wrapper.find('[data-testid="segments-dialog-invalid"]').exists()).toBe(true);
    expect(
      wrapper.find('[data-testid="segments-dialog-save"]').attributes('disabled')
    ).toBeDefined();

    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();
    expect(apiPost).not.toHaveBeenCalled();
  });

  it('will not save a zero-day window — older_than 0 days is everyone', async () => {
    const wrapper = await mountView();
    await wrapper.find('[data-testid="segments-open-dialog"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="segments-dialog-name"]').setValue('Zero');
    await wrapper.find('[data-testid="segments-rule-value-0"]').setValue('0');
    await flushPromises();

    expect(wrapper.find('[data-testid="segments-dialog-invalid"]').exists()).toBe(true);
    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();
    expect(apiPost).not.toHaveBeenCalled();
  });

  it('sends multiple rules with the chosen match mode', async () => {
    const wrapper = await mountView();
    await wrapper.find('[data-testid="segments-open-dialog"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="segments-dialog-name"]').setValue('Either way');
    await wrapper.find('[data-testid="segments-dialog-match"]').setValue('any');

    await wrapper.find('[data-testid="segments-rule-add"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="segments-rule-field-1"]').setValue('created_at');
    await flushPromises();
    await wrapper.find('[data-testid="segments-rule-value-1"]').setValue('30');
    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();

    const [, payload] = apiPost.mock.calls[0];
    expect(payload.rules.match).toBe('any');
    expect(payload.rules.rules).toHaveLength(2);
    expect(payload.rules.rules[1].field).toBe('created_at');
  });

  it('removing a rule drops it from the payload, and the last one cannot be removed', async () => {
    const wrapper = await mountView();
    await wrapper.find('[data-testid="segments-open-dialog"]').trigger('click');
    await flushPromises();

    expect(
      wrapper.find('[data-testid="segments-rule-remove-0"]').attributes('disabled')
    ).toBeDefined();

    await wrapper.find('[data-testid="segments-rule-add"]').trigger('click');
    await flushPromises();
    expect(wrapper.findAll('[data-testid^="segments-rule-value-"]')).toHaveLength(2);

    await wrapper.find('[data-testid="segments-rule-remove-1"]').trigger('click');
    await flushPromises();
    expect(wrapper.findAll('[data-testid^="segments-rule-value-"]')).toHaveLength(1);
  });
});

// ── Export CSV on selected customers (#673) ─────────────────────────────────
// The button used to click a raw <a> to /api/customers/export — a route that
// does not exist, carrying no bearer token — so it did nothing on every path.
// It now downloads the admin-only /api/exports/customers?ids=… with the token,
// and is offered only to the roles that endpoint allows.

const CUSTOMERS = [
  { id: 'aaaaaaaa-0000-4000-8000-000000000001', name: 'Ada', phone: '555-0001' },
  { id: 'aaaaaaaa-0000-4000-8000-000000000002', name: 'Bo', phone: '555-0002' },
  { id: 'aaaaaaaa-0000-4000-8000-000000000003', name: 'Cy', phone: '555-0003' },
];

describe('SegmentsView — Export CSV on selected customers (#673)', () => {
  beforeEach(() => {
    apiGet.mockReset();
    toastAdd.mockClear();
    downloadAuthedFile.mockReset();
    downloadAuthedFile.mockResolvedValue(undefined);
    authState.isAdmin = true;
    apiGet.mockImplementation((url) => {
      if (url === '/api/segments') return Promise.resolve({ items: [] });
      if (url.startsWith('/api/customers')) return Promise.resolve({ items: CUSTOMERS, total: 3 });
      return Promise.resolve({ items: [] });
    });
  });

  const customersTable = (w) => w.find('[data-testid="segments-customers-table"]');

  async function pick(wrapper, indexes) {
    for (const i of indexes) {
      await customersTable(wrapper).findAll('.dt-select')[i].trigger('click');
      await flushPromises();
    }
  }

  it('downloads exactly the selected customers through the authed export', async () => {
    const wrapper = await mountView();
    await pick(wrapper, [0, 2]);

    await wrapper.find('[data-testid="segments-bulk-export"]').trigger('click');
    await flushPromises();

    expect(downloadAuthedFile).toHaveBeenCalledTimes(1);
    const [url, filename] = downloadAuthedFile.mock.calls[0];
    const parsed = new URL(url, 'http://x');
    expect(parsed.pathname).toBe('/api/exports/customers');
    expect(parsed.searchParams.get('ids')).toBe(`${CUSTOMERS[0].id},${CUSTOMERS[2].id}`);
    expect(filename).toMatch(/^customers-selected-\d{4}-\d{2}-\d{2}\.csv$/);
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success', detail: '2 selected customers' }));
  });

  it('is not offered to a role the export refuses', async () => {
    authState.isAdmin = false;
    const wrapper = await mountView();
    await pick(wrapper, [0]);

    // The toolbar is there (Add Tag still works) — only Export is withheld.
    expect(wrapper.find('[data-testid="segments-customer-bulk-toolbar"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="segments-bulk-add-tag"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="segments-bulk-export"]').exists()).toBe(false);
  });

  it('says why when the download is refused, instead of doing nothing', async () => {
    downloadAuthedFile.mockRejectedValue(Object.assign(new Error('Failed to load file (403)'), { status: 403 }));
    const wrapper = await mountView();
    await pick(wrapper, [1]);

    await wrapper.find('[data-testid="segments-bulk-export"]').trigger('click');
    await flushPromises();

    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'error',
      detail: 'Only admins and owners can export customers.',
    }));
  });

  it('refuses a selection larger than the server accepts, with a way forward', async () => {
    const many = Array.from({ length: 201 }, (_, i) => ({
      id: `bbbbbbbb-0000-4000-8000-${String(i).padStart(12, '0')}`,
      name: `C${i}`,
    }));
    apiGet.mockImplementation((url) => {
      if (url === '/api/segments') return Promise.resolve({ items: [] });
      if (url.startsWith('/api/customers')) return Promise.resolve({ items: many, total: many.length });
      return Promise.resolve({ items: [] });
    });
    const wrapper = await mountView();
    // The table pages at 15 rows, so a selection this size spans pages: emit
    // it from the table component directly.
    const table = wrapper
      .findAllComponents(stubs.DataTable)
      .find((c) => c.attributes('data-testid') === 'segments-customers-table');
    table.vm.$emit('update:selection', many);
    await flushPromises();

    await wrapper.find('[data-testid="segments-bulk-export"]').trigger('click');
    await flushPromises();

    expect(downloadAuthedFile).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'warn',
      detail: expect.stringContaining('Data Export'),
    }));
  });
});

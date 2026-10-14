/**
 * CustomersView — tags are readable and filterable from the list (GDXA-1046).
 *
 * Before this, a tag written by bulk-tag showed nowhere but one customer's
 * Tags tab: the list had no tag column and sent no tag filter. These drive the
 * real component and assert the request that leaves it and the chips it draws.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createRouter, createMemoryHistory } from 'vue-router';
import CustomersView from '../CustomersView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGet = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({ hasPermission: () => true }),
}));
const exportCsv = vi.fn();
vi.mock('../../composables/useTableExport', () => ({
  useTableExport: () => ({ exportCsv }),
}));

const VIP = { id: 'aaaaaaaa-1111-4111-8111-000000000001', name: 'VIP', color: '#ff0000' };
const NET30 = { id: 'aaaaaaaa-1111-4111-8111-000000000002', name: 'Net 30', color: '#eeeeee' };
const ALPHA = { id: 'cccccccc-0000-4000-8000-000000000001', name: 'Alpha', tags: [NET30, VIP] };
const BRAVO = { id: 'cccccccc-0000-4000-8000-000000000002', name: 'Bravo', tags: [] };

const RowScope = {
  props: ['row'],
  provide() {
    return { dtRow: this.row };
  },
  template: '<td><slot /></td>',
};

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Column: {
    inject: { dtRow: { default: null } },
    template: '<span><slot name="body" :data="dtRow" /></span>',
  },
  DataTable: {
    props: ['value'],
    components: { RowScope },
    template: `<table><tbody>
        <tr v-for="(row, i) in (value || [])" :key="i" class="dt-row">
          <RowScope :row="row"><slot /></RowScope>
        </tr>
      </tbody></table>`,
  },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue', 'change'],
    template: `<select :data-testid="$attrs['data-testid']" :value="modelValue"
      @change="$emit('update:modelValue', $event.target.value || null); $emit('change')">
        <option value=""></option>
        <option v-for="o in options || []" :key="o.id" :value="o.id">{{ o.name }}</option>
      </select>`,
    inheritAttrs: false,
  },
  Button: {
    props: ['label'],
    emits: ['click'],
    template: `<button :data-testid="$attrs['data-testid']" @click="$emit('click', $event)">{{ label }}</button>`,
    inheritAttrs: false,
  },
  ProgressSpinner: { template: '<div />' },
  EmptyState: { props: ['title'], template: '<div class="empty">{{ title }}</div>' },
  CustomerFormDialog: { template: '<div />' },
  Dialog: { template: '<div />' },
  Toast: { template: '<div />' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
};

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/customers/:id', component: { template: '<div />' } },
    ],
  });
  await router.push('/');
  const wrapper = mount(CustomersView, {
    global: { plugins: [router], stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return wrapper;
}

describe('CustomersView — tags', () => {
  beforeEach(() => {
    localStorage.clear();
    apiGet.mockReset();
    exportCsv.mockReset();
    apiGet.mockImplementation((url) => {
      if (url === '/api/tags') return Promise.resolve([NET30, VIP]);
      if (url.includes('tag_id=')) return Promise.resolve({ items: [ALPHA], total: 1 });
      return Promise.resolve({ items: [ALPHA, BRAVO], total: 2 });
    });
  });

  it('draws each customer\'s tags as chips, and a dash for none', async () => {
    const wrapper = await mountView();
    const rows = wrapper.findAll('tr.dt-row');
    expect(rows[0].findAll('[data-testid="customer-tag-chip"]').map((c) => c.text()))
      .toEqual(['Net 30', 'VIP']);
    expect(rows[1].findAll('[data-testid="customer-tag-chip"]')).toHaveLength(0);
    // A light tag gets dark text, a dark one white.
    const [light, dark] = rows[0].findAll('[data-testid="customer-tag-chip"]');
    expect(light.attributes('style')).toContain('color: rgb(31, 41, 55)');
    expect(dark.attributes('style')).toContain('color: rgb(255, 255, 255)');
  });

  it('asks the server for one tag\'s customers, and drops the filter when cleared', async () => {
    const wrapper = await mountView();
    expect(apiGet).toHaveBeenCalledWith('/api/tags');

    await wrapper.find('[data-testid="customers-tag-filter"]').setValue(VIP.id);
    await flushPromises();
    const filtered = apiGet.mock.calls.map((c) => c[0]).filter((u) => u.startsWith('/api/customers'));
    expect(filtered.at(-1)).toBe(`/api/customers?per_page=1000&tag_id=${VIP.id}`);
    expect(wrapper.findAll('tr.dt-row')).toHaveLength(1);

    await wrapper.find('[data-testid="customers-tag-filter"]').setValue('');
    await flushPromises();
    const cleared = apiGet.mock.calls.map((c) => c[0]).filter((u) => u.startsWith('/api/customers'));
    expect(cleared.at(-1)).toBe('/api/customers?per_page=1000');
  });

  it('exports the tags with the rows', async () => {
    const wrapper = await mountView();
    await wrapper.find('[data-testid="customers-export-btn"]').trigger('click');
    const [rows, columns] = exportCsv.mock.calls[0];
    expect(columns.map((c) => c.header)).toContain('Tags');
    expect(rows[0].tag_names).toBe('Net 30; VIP');
  });
});

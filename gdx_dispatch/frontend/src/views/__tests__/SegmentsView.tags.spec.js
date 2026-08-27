/**
 * SegmentsView — the list shows only what the segments API returns.
 *
 * `SegmentOut` is `{id, name, rules, is_builtin, matching_customer_count,
 * created_at}`. There is no `tags` field, so the page must not offer a
 * tag column or a tag filter: a control whose data the API never supplies
 * can only ever render "—" or an empty dropdown.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SegmentsView from '../SegmentsView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));

const apiGetMock = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGetMock, post: vi.fn(), patch: vi.fn() }),
}));

// Exactly the shape routers/segments.py `SegmentOut` returns — no `tags`.
const API_SEGMENTS = [
  {
    id: 'at-risk',
    name: 'At Risk',
    is_builtin: true,
    rules: { field: 'last_job_date', operator: 'older_than', value: '180 days' },
    matching_customer_count: 3,
    created_at: null,
  },
  {
    id: '11111111-2222-3333-4444-555555555555',
    name: 'Big spenders',
    is_builtin: false,
    rules: { match: 'all', rules: [{ field: 'lifetime_value', operator: 'greater_than', value: 5000 }] },
    matching_customer_count: 1,
    created_at: '2026-08-01T00:00:00Z',
  },
];

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
  DatePicker: { template: '<input />' },
  ToggleSwitch: { template: '<input type="checkbox" />' },
  ProgressSpinner: { template: '<div />' },
  EmptyState: { template: '<div />' },
  Dialog: { props: ['visible'], template: "<div v-if='visible'><slot /><slot name='footer' /></div>" },
  // Render each column's header so the test can see which columns the list offers.
  Column: { props: ['header'], template: '<div data-test="column">{{ header }}</div>' },
  DataTable: { props: ['value'], template: '<div><slot /></div>' },
  Select: {
    props: ['modelValue', 'options'],
    template: '<select><option v-for="o in options" :key="o.value">{{ o.label }}</option></select>',
  },
  InputText: { template: '<input />' },
  Button: { props: ['label'], template: '<button>{{ label }}</button>' },
};

function mountView() {
  apiGetMock.mockImplementation(async (url) => {
    if (url.startsWith('/api/segments')) return { items: API_SEGMENTS };
    return { items: [] };
  });
  return mount(SegmentsView, { global: { stubs } });
}

describe('SegmentsView — tags', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
  });

  it('offers no tag filter, because the API has no tags to filter on', async () => {
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.find('[data-testid="segments-tag-filter"]').exists()).toBe(false);
    // The rest of the filter row still works.
    expect(wrapper.find('[data-testid="segments-date-filter"]').exists()).toBe(true);
  });

  it('lists only columns the API can fill — no Tags column', async () => {
    const wrapper = mountView();
    await flushPromises();

    const headers = wrapper.findAll('[data-test="column"]').map((c) => c.text());
    expect(headers).not.toContain('Tags');
    expect(headers).toContain('Customers');
    expect(headers).toContain('Actions');
  });
});

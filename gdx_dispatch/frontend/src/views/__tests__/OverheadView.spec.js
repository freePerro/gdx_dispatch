import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));

// Stub the chart so jsdom doesn't need a canvas.
vi.mock('vue-chartjs', () => ({ Line: { template: '<div class="chart-stub" />' } }));
vi.mock('chart.js', () => ({
  Chart: { register: () => {} },
  LineElement: {}, PointElement: {}, LinearScale: {}, CategoryScale: {},
  Tooltip: {}, Legend: {}, Filler: {},
}));

import OverheadView from '../OverheadView.vue';

const LIST = {
  obligations: [
    { id: 'a', label: 'Shop rent', category: 'rent', amount: '2000.00', cadence: 'monthly',
      start_date: '2025-01-01', end_date: null, term_total_occurrences: null,
      scheduled_changes: [], cost_type: 'fixed', is_estimate: false, source: 'manual', active: true },
    { id: 'b', label: 'Truck loan', category: 'loan', amount: '500.00', cadence: 'monthly',
      start_date: '2025-01-01', end_date: '2026-09-01', term_total_occurrences: null,
      scheduled_changes: [], cost_type: 'fixed', is_estimate: false, source: 'manual', active: true },
  ],
  current_monthly_total: '2500.00',
  categories: ['rent', 'loan', 'other'],
  cadences: ['monthly', 'annual'],
  cost_types: ['fixed', 'variable'],
};

const PROJECTION = {
  months: [
    { year: 2026, month: 8, label: '2026-08', total: '2500.00', by_category: {} },
    { year: 2026, month: 9, label: '2026-09', total: '2000.00', by_category: {} },
    { year: 2026, month: 10, label: '2026-10', total: '2000.00', by_category: {} },
  ],
  current_monthly_total: '2500.00',
  horizon_total: '2000.00',
  categories: ['loan', 'rent'],
  step_downs: [{ year: 2026, month: 9, label: '2026-09', drop: '500.00', ended: ['Truck loan'] }],
  disclaimer: 'Outflow only — this is overhead you must pay, not runway.',
};

const SUGGESTIONS = {
  count: 1,
  suggestions: [
    { stream_id: 's1', label: 'ACME Insurance', payee_pattern: 'ACME INSURANCE',
      suggested_amount: '190.00', amount_min: '180.00', amount_max: '200.00',
      cadence: 'monthly', suggested_category: 'insurance', status: 'active',
      occurrences_seen: 6, next_expected_date: '2026-07-15', term_end_date: null,
      term_total_occurrences: null },
  ],
};

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Card: { template: '<div><slot name="title" /><slot name="content" /></div>' },
  Button: { props: ['label'], emits: ['click'], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  Select: { props: ['modelValue', 'options'], template: '<select />' },
  DataTable: { props: ['value'], template: '<table><tbody><tr v-for="(r,i) in value" :key="i"><slot name="body" :data="r" /></tr></tbody></table>' },
  Column: { template: '<td><slot name="body" :data="$attrs.data" /></td>' },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  InputText: { template: '<input />' },
  InputNumber: { template: '<input type="number" />' },
  Textarea: { template: '<textarea />' },
  ToggleSwitch: { template: '<input type="checkbox" />' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Message: { template: '<div><slot /></div>' },
};

describe('OverheadView', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiGet.mockImplementation((url) => {
      if (url.includes('/suggestions')) return Promise.resolve(SUGGESTIONS);
      if (url.includes('projection')) return Promise.resolve(PROJECTION);
      return Promise.resolve(LIST);
    });
  });

  it('renders the current monthly overhead total', async () => {
    const wrapper = mount(OverheadView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.text()).toContain('$2,500.00');
  });

  it('loads obligations and reflects the tracked count', async () => {
    const wrapper = mount(OverheadView, { global: { stubs } });
    await flushPromises();
    // both list + projection endpoints were fetched
    expect(apiGet).toHaveBeenCalledWith('/api/overhead');
    expect(apiGet.mock.calls.some(([u]) => u.includes('/api/overhead/projection'))).toBe(true);
    // KPI reflects the 2 loaded obligations
    expect(wrapper.text()).toContain('Tracked obligations');
    expect(wrapper.text()).toContain('2');
  });

  it('surfaces the loan-payoff step-down', async () => {
    const wrapper = mount(OverheadView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.text()).toContain('Truck loan');
    expect(wrapper.text()).toContain('$500.00');
    // step-down narration mentions the drop
    expect(wrapper.text().toLowerCase()).toContain('drops');
  });

  it('shows the outflow-not-runway scope note', async () => {
    const wrapper = mount(OverheadView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.text().toLowerCase()).toContain('outflow');
  });

  it('renders bank-detected suggestions', async () => {
    const wrapper = mount(OverheadView, { global: { stubs } });
    await flushPromises();
    expect(apiGet.mock.calls.some(([u]) => u.includes('/suggestions'))).toBe(true);
    expect(wrapper.text()).toContain('Suggested from bank activity');
    expect(wrapper.text()).toContain('ACME Insurance');
    expect(wrapper.text()).toContain('$190.00');
  });

  it('dismissing a suggestion hides it', async () => {
    const wrapper = mount(OverheadView, { global: { stubs } });
    await flushPromises();
    const dismiss = wrapper.findAll('button').find((b) => b.text() === 'Dismiss');
    expect(dismiss).toBeTruthy();
    await dismiss.trigger('click');
    await flushPromises();
    expect(wrapper.text()).not.toContain('ACME Insurance');
  });
});

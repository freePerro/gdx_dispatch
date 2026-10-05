/**
 * CustomFieldsView — the lead intake fields get their own group and option.
 *
 * Why this spec exists: bootstrap_app seeds five lead intake fields
 * (entity_type "lead", PR #817). The group header was a two-way ternary —
 * anything that was not a job read "Customer Fields" — and the entity
 * dropdown offered only Customer and Job. So the seeded fields showed up as
 * five unexplained customer fields, and an admin who deleted them as junk
 * lost them for good (the seed runs once ever).
 *
 * jsdom applies no media queries — this proves wiring, never layout.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

const apiGet = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));

import CustomFieldsView from '../CustomFieldsView.vue';

const defs = [
  { id: '1', entity_type: 'customer', field_key: 'gate_code', label: 'Gate code', field_type: 'text', options: null, required: false, sort_order: 0 },
  { id: '2', entity_type: 'job', field_key: 'permit', label: 'Permit', field_type: 'text', options: null, required: false, sort_order: 0 },
  { id: '3', entity_type: 'lead', field_key: 'door_size', label: 'Door size', field_type: 'text', options: null, required: false, sort_order: 3 },
];

function mountView() {
  return mount(CustomFieldsView, { global: { plugins: [PrimeVue] }, attachTo: document.body });
}

describe('CustomFieldsView lead group', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiGet.mockResolvedValue(defs);
  });

  it('labels each entity group by its own name — lead fields are not customer fields', async () => {
    const wrapper = mountView();
    await flushPromises();
    const headers = wrapper.findAll('.group-header strong').map((h) => h.text());
    expect(headers).toContain('Lead Intake Fields');
    expect(headers).toContain('Customer Fields');
    expect(headers).toContain('Job Fields');
    expect(headers.filter((h) => h === 'Customer Fields')).toHaveLength(1);
    wrapper.unmount();
  });

  it('offers Lead as an entity for a new field', async () => {
    const wrapper = mountView();
    await flushPromises();
    const options = wrapper.vm.$.setupState.entityOptions ?? wrapper.vm.entityOptions;
    expect(options.map((o) => o.value)).toContain('lead');
    wrapper.unmount();
  });

  it('binds the entity and type dropdowns to the option VALUE, not the option object', async () => {
    // Without option-label/option-value PrimeVue shows each option as raw JSON
    // and v-model receives the whole {label, value} object — the create POST
    // then sent entity_type: {label, value} and got a 422 (seen in a real
    // browser 2026-09-28, and broken on main for Customer and Job too).
    const wrapper = mountView();
    await flushPromises();
    await wrapper.find('button').trigger('click');
    await flushPromises();
    const selects = wrapper.findAllComponents({ name: 'Select' });
    expect(selects.length).toBeGreaterThanOrEqual(2);
    for (const s of selects) {
      expect(s.props('optionLabel')).toBe('label');
      expect(s.props('optionValue')).toBe('value');
    }
    wrapper.unmount();
  });
});

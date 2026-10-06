/**
 * GDXA-317 — MapsView used to read GET /api/maps, a ui_compat stub that
 * answered {"tech_locations": []} for every tenant, so the page never showed
 * a technician. It now reads the breadcrumb table the phones write
 * (GET /api/dispatch/locations) and names each row from /api/technicians.
 */
import { describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const get = vi.fn(async (url) => {
  if (url.startsWith('/api/dispatch/locations')) {
    return [
      { id: 'l1', user_id: 'u-1', technician_id: 't-1', lat: 46.86, lng: -96.81, accuracy_m: 8, recorded_at: '2026-10-06T18:00:00Z' },
      { id: 'l2', user_id: 'u-2', technician_id: null, lat: 46.87, lng: -96.79, accuracy_m: null, recorded_at: '2026-10-06T18:01:00Z' },
      { id: 'l3', user_id: 'u-unknown', technician_id: null, lat: 46.88, lng: -96.78, accuracy_m: null, recorded_at: '2026-10-06T18:02:00Z' },
    ];
  }
  if (url === '/api/technicians') {
    return [
      { id: 't-1', user_id: 'u-1', name: 'Pat Tech' },
      { id: 't-2', user_id: 'u-2', name: 'Sam Tech' },
    ];
  }
  if (url === '/api/settings/integrations/google-maps') return { key: '' };
  throw new Error(`unexpected GET ${url}`);
});

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get }),
}));

const DataTable = {
  props: ['value'],
  template: '<div data-testid="maps-tech-table"><div v-for="r in value" :key="r.id" class="row">{{ r.tech_name }}</div><slot v-if="!value.length" name="empty" /></div>',
};

const stubs = {
  Toolbar: { template: '<div><slot name="start" /></div>' },
  DataTable,
  Column: { template: '<div />' },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="filter" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Button: { template: '<button />' },
  ProgressSpinner: { template: '<div />' },
};

describe('MapsView reads real technician locations', () => {
  it('calls /api/dispatch/locations, never the retired /api/maps stub', async () => {
    const MapsView = (await import('../MapsView.vue')).default;
    const w = mount(MapsView, { global: { stubs } });
    await flushPromises();
    const urls = get.mock.calls.map((c) => c[0]);
    expect(urls).toContain('/api/dispatch/locations?minutes=30');
    expect(urls).not.toContain('/api/maps');
    // technician_id, then user_id, then the raw id — never anonymous.
    expect(w.findAll('.row').map((r) => r.text())).toEqual(['Pat Tech', 'Sam Tech', 'u-unknown']);
    expect(w.find('[data-testid="maps-tab-routes"]').exists()).toBe(false);
  });

  it('says the filter matched nothing, not that no tech reported', async () => {
    const MapsView = (await import('../MapsView.vue')).default;
    const w = mount(MapsView, { global: { stubs } });
    await flushPromises();
    await w.find('input.filter').setValue('nobody');
    expect(w.findAll('.row')).toHaveLength(0);
    expect(w.find('[data-testid="maps-tech-no-match"]').exists()).toBe(true);
    expect(w.find('[data-testid="maps-tech-empty"]').exists()).toBe(false);
  });

  it('shows an error state, not an empty table, when the read fails', async () => {
    const real = get.getMockImplementation();
    get.mockImplementation(async (url) => {
      if (url.startsWith('/api/dispatch/locations')) throw new Error('500');
      return real(url);
    });
    const MapsView = (await import('../MapsView.vue')).default;
    const w = mount(MapsView, { global: { stubs } });
    await flushPromises();
    get.mockImplementation(real);
    expect(w.find('[data-testid="maps-load-error"]').exists()).toBe(true);
    expect(w.find('[data-testid="maps-tech-table"]').exists()).toBe(false);
  });
});

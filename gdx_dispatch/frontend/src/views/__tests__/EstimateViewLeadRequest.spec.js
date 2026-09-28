import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const mockApiGet = vi.fn();
const mockApiRawGet = vi.fn();
const routeMock = { params: { id: 'est-456' }, query: {} };

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: mockApiGet,
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: mockApiGet,
    raw: { get: mockApiRawGet },
    suppressAuthRedirect: vi.fn(),
  }),
}));

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    hasPermission: vi.fn(() => true),
    user: { id: 'u1', role: 'owner' },
  }),
}));

vi.mock('../../composables/useEstimateSources', async () => {
  const { ref } = await import('vue');
  return {
    classifyPickerError: () => 'unknown',
    useEstimateSources: () => ({ sources: ref([]), discover: vi.fn() }),
  };
});

vi.mock('vue-router', () => ({
  useRoute: () => routeMock,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import EstimateView from '../EstimateView.vue';

const mockPanel = {
  name: 'EstimateLeadRequestPanel',
  props: ['estimateId'],
  template: '<div class="mock-lead-panel" :data-estimate-id="estimateId">Mock Lead Panel</div>',
};

const STUBS = {
  EstimateLeadRequestPanel: mockPanel,
  Button: { template: '<button><slot /></button>' },
  Tag: { template: '<span><slot /></span>' },
  Card: { template: '<div class="p-card"><slot name="content" /></div>' },
  Select: { template: '<select />' },
  InputText: { template: '<input />' },
  Textarea: { template: '<textarea />' },
  DatePicker: { template: '<input />' },
  RadioButton: { template: '<input type="radio" />' },
  ToggleSwitch: { template: '<input type="checkbox" />' },
  Dialog: { template: '<div v-if="visible"><slot /></div>', props: ['visible'] },
  Divider: { template: '<hr />' },
  InputNumber: { template: '<input type="number" />' },
  CatalogPickerDialog: { template: '<div />' },
  EstimateProfitPanel: { template: '<div />' },
  EstimateStatusContext: { template: '<div />' },
  CustomerFormDialog: { template: '<div />' },
  PluginScreen: { template: '<div />' },
  PhoneInput: { template: '<input />' },
  Toast: { template: '<div />' },
  routerLink: { template: '<a><slot /></a>' },
};

describe('EstimateView Lead Request Side Panel', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    routeMock.params = { id: 'est-456' };
  });

  it('renders EstimateLeadRequestPanel with current estimate id for existing estimate', async () => {
    mockApiGet.mockImplementation((url) => {
      if (url === '/api/customers') return Promise.resolve([]);
      if (url === '/api/estimates/pricing-categories') return Promise.resolve([]);
      if (url === '/api/estimates/est-456') {
        return Promise.resolve({
          id: 'est-456',
          estimate_number: 'EST-00456',
          customer_id: 'cust-1',
          status: 'Draft',
          line_items: [],
          tax_rate: 0,
        });
      }
      if (url.includes('/activity')) return Promise.resolve({ items: [], total: 0 });
      if (url.includes('/attachments')) return Promise.resolve([]);
      return Promise.resolve({});
    });
    mockApiRawGet.mockResolvedValue({ ok: true, json: async () => ({}) });

    const w = mount(EstimateView, {
      global: {
        stubs: STUBS,
        directives: { tooltip: () => {} },
      },
    });
    await flushPromises();

    const panel = w.findComponent(mockPanel);
    expect(panel.exists()).toBe(true);
    expect(panel.attributes('data-estimate-id')).toBe('est-456');
  });

  it('does not render EstimateLeadRequestPanel for new estimate (/estimates/new)', async () => {
    routeMock.params = {};
    mockApiGet.mockImplementation((url) => {
      if (url === '/api/customers') return Promise.resolve([]);
      if (url === '/api/estimates/pricing-categories') return Promise.resolve([]);
      return Promise.resolve({});
    });
    mockApiRawGet.mockResolvedValue({ ok: true, json: async () => ({}) });

    const w = mount(EstimateView, {
      global: {
        stubs: STUBS,
        directives: { tooltip: () => {} },
      },
    });
    await flushPromises();

    const panel = w.findComponent(mockPanel);
    expect(panel.exists()).toBe(false);
  });
});

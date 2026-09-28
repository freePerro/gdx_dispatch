import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import EstimateLeadRequestPanel from '../EstimateLeadRequestPanel.vue';

const mockApiGet = vi.fn();
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: mockApiGet,
  }),
}));

const mockHasPermission = vi.fn(() => true);
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    hasPermission: mockHasPermission,
  }),
}));

const STUBS = {
  Card: {
    template: '<div class="card-stub"><slot name="title" /><slot name="content" /></div>',
  },
  routerLink: {
    props: ['to'],
    template: '<a :href="to?.path || to"><slot /></a>',
  },
};

const LEAD_DATA = {
  id: 'lead-12345678-abcd',
  name: 'Sam Sample',
  source: 'Website Form',
  notes: 'Needs new springs and opener replacement',
  follow_up_date: 'Oct 15, 2026',
  created_at: '2026-10-01T12:00:00Z',
  // The server stores every answer as text, so these arrive as strings.
  custom_fields: [
    { field_key: 'door_count', label: 'Door Count', value: '2' },
    { field_key: 'has_opener', label: 'Has Opener', value: 'true' },
    { field_key: 'has_keypad', label: 'Has Keypad', value: 'false' },
    { field_key: 'empty_field', label: 'Empty Field', value: null },
  ],
};

describe('EstimateLeadRequestPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    mockHasPermission.mockReturnValue(true);
  });

  it('renders nothing when leads.read permission is missing', async () => {
    mockHasPermission.mockReturnValue(false);
    const w = mount(EstimateLeadRequestPanel, {
      props: { estimateId: 'est-1' },
      global: { stubs: STUBS },
    });
    await flushPromises();

    expect(w.find('[data-testid="estimate-lead-request-panel"]').exists()).toBe(false);
    expect(mockApiGet).not.toHaveBeenCalled();
  });

  it('renders nothing when API returns 404 (estimate not from lead)', async () => {
    mockApiGet.mockRejectedValue(new Error('Not found'));
    const w = mount(EstimateLeadRequestPanel, {
      props: { estimateId: 'est-1' },
      global: { stubs: STUBS },
    });
    await flushPromises();

    expect(w.find('[data-testid="estimate-lead-request-panel"]').exists()).toBe(false);
  });

  it('renders customer request notes, source, and follow-up date when lead exists', async () => {
    mockApiGet.mockResolvedValue(LEAD_DATA);
    const w = mount(EstimateLeadRequestPanel, {
      props: { estimateId: 'est-1' },
      global: { stubs: STUBS },
    });
    await flushPromises();

    expect(w.find('[data-testid="estimate-lead-request-panel"]').exists()).toBe(true);
    expect(w.find('[data-testid="lead-request-notes"]').text()).toContain('Needs new springs');
    expect(w.find('[data-testid="lead-request-source"]').text()).toBe('Website Form');
    expect(w.find('[data-testid="lead-request-followup"]').text()).toContain('Oct 15, 2026');
    expect(w.find('[data-testid="lead-request-link"]').text()).toContain('Lead #lead-123');
  });

  it('renders and formats intake custom fields cleanly, omitting null values', async () => {
    mockApiGet.mockResolvedValue(LEAD_DATA);
    const w = mount(EstimateLeadRequestPanel, {
      props: { estimateId: 'est-1' },
      global: { stubs: STUBS },
    });
    await flushPromises();

    expect(w.find('[data-testid="lead-cf-door_count"]').text()).toContain('2');
    expect(w.find('[data-testid="lead-cf-has_opener"]').text()).toContain('Yes');
    expect(w.find('[data-testid="lead-cf-has_keypad"]').text()).toContain('No');
    expect(w.find('[data-testid="lead-cf-empty_field"]').exists()).toBe(false);
  });

  it('asks without an error toast — a 404 is the normal answer for most estimates', async () => {
    mockApiGet.mockRejectedValue(new Error('Lead not found for estimate'));
    mount(EstimateLeadRequestPanel, { props: { estimateId: 'est-1' }, global: { stubs: STUBS } });
    await flushPromises();

    expect(mockApiGet).toHaveBeenCalledWith('/api/leads/by-estimate/est-1', { suppressErrorToast: true });
  });
});

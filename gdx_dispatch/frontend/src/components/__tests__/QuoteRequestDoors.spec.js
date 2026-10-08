/**
 * Staff view of a portal quote request's doors, on the lead it opened and the
 * estimate started from that lead. The request row is the only copy of the
 * doors, so this is the only place staff see them.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import QuoteRequestDoors from '../QuoteRequestDoors.vue';

const mockApiGet = vi.fn();
vi.mock('../../composables/useApi', () => ({ useApi: () => ({ get: mockApiGet }) }));
const mockHasPermission = vi.fn(() => true);
vi.mock('../../stores/auth', () => ({ useAuthStore: () => ({ hasPermission: mockHasPermission }) }));

const stubs = {
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
  AuthedImage: { props: ['src'], template: '<img :data-src="src" />' },
};

const REQUEST = {
  id: 'req-1',
  job_name: 'Lake cabin',
  site_address: '9 Shore Rd',
  notes: 'Gate code 1234',
  status: 'Received',
  created_at: '2026-10-07T12:00:00Z',
  doors: [
    {
      index: 0, quantity: 2, size_label: '16\' 0" x 7\' 0"',
      material: 'steel', style: 'carriage_house', insulation: 'not_sure', windows: 'top_row',
      placement: 'replace', opener: 'yes', color: 'White', notes: 'Dented bottom panel',
      photo_ids: ['ph-1'],
    },
    { index: 1, quantity: 1, size_label: '9\' 0" x 8\' 0"', material: 'not_sure', photo_ids: [] },
  ],
};

async function mountFor(leadId = 'lead-1') {
  const w = mount(QuoteRequestDoors, { props: { leadId }, global: { stubs } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockHasPermission.mockReturnValue(true);
});

describe('QuoteRequestDoors', () => {
  it('renders nothing for a lead that did not come from the portal', async () => {
    mockApiGet.mockResolvedValue(null);
    const w = await mountFor();
    expect(mockApiGet).toHaveBeenCalledWith('/api/quote-requests/by-lead/lead-1', { suppressErrorToast: true });
    expect(w.find('[data-testid="quote-request-doors"]').exists()).toBe(false);
  });

  it('does not ask without leads.read', async () => {
    mockHasPermission.mockReturnValue(false);
    await mountFor();
    expect(mockApiGet).not.toHaveBeenCalled();
  });

  it('shows the job, each door and its answers, leaving out "not sure"', async () => {
    mockApiGet.mockResolvedValue(REQUEST);
    const w = await mountFor();
    expect(w.get('[data-testid="quote-request-doors-job"]').text()).toBe('Lake cabin');
    const door0 = w.get('[data-testid="quote-request-door-0"]').text();
    expect(door0).toContain('2 × 16\' 0" x 7\' 0"');
    expect(door0).toContain('Steel');
    expect(door0).toContain('Carriage house');
    expect(door0).toContain('Top row');
    expect(door0).toContain('White');
    expect(door0).toContain('Dented bottom panel');
    expect(door0).not.toContain('Not sure');
    expect(w.get('[data-testid="quote-request-door-1"]').text()).toContain('9\' 0" x 8\' 0"');
    expect(w.get('[data-testid="quote-request-doors-notes"]').text()).toBe('Gate code 1234');
  });

  it('loads photos from the staff route, with auth', async () => {
    mockApiGet.mockResolvedValue(REQUEST);
    const w = await mountFor();
    const img = w.get('[data-testid="quote-request-photo-ph-1"] img');
    expect(img.attributes('data-src')).toBe('/api/quote-requests/req-1/photos/ph-1');
  });

  it('shows when the customer changed or withdrew the request', async () => {
    mockApiGet.mockResolvedValue({
      ...REQUEST, status: 'Withdrawn',
      edited_at: '2026-10-07T13:00:00Z', withdrawn_at: '2026-10-07T14:30:00Z',
    });
    const w = await mountFor();
    expect(w.get('[data-testid="quote-request-doors-withdrawn"]').text()).toMatch(/Withdrawn by the customer .*2026/);
    expect(w.get('[data-testid="quote-request-doors-edited"]').text()).toMatch(/Changed by the customer .*2026/);
  });

  it('shows neither stamp on a request that was not changed', async () => {
    mockApiGet.mockResolvedValue({ ...REQUEST, edited_at: null, withdrawn_at: null });
    const w = await mountFor();
    expect(w.find('[data-testid="quote-request-doors-withdrawn"]').exists()).toBe(false);
    expect(w.find('[data-testid="quote-request-doors-edited"]').exists()).toBe(false);
  });
});

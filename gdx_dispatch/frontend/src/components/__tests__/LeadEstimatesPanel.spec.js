/**
 * LeadEstimatesPanel — the lead dialog's list of its estimates and the
 * "counts as won" pick.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPut = vi.fn();
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, put: apiPut }),
}));

import LeadEstimatesPanel from '../LeadEstimatesPanel.vue';

const BODY = {
  lead_id: 'lead-1',
  selected_estimate_id: 'e1',
  estimates: [
    { id: 'e1', estimate_number: 'EST-0001', label: 'Front door', status: 'accepted', total: 2400 },
    { id: 'e2', estimate_number: 'EST-0001-1', label: 'Opener too', status: 'accepted', total: 3100 },
    { id: 'e3', estimate_number: 'EST-0001-2', label: 'Budget', status: 'draft', total: 1800 },
  ],
};

const stubs = {
  RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
  Tag: {
    props: ['value'],
    template: '<span :data-testid="$attrs[\'data-testid\']">{{ value }}</span>',
    inheritAttrs: false,
  },
  Button: {
    props: ['label', 'loading'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
};

function mountPanel(canWrite = true) {
  return mount(LeadEstimatesPanel, { props: { leadId: 'lead-1', canWrite }, global: { stubs } });
}

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue(BODY);
  apiPut.mockReset();
});

describe('LeadEstimatesPanel', () => {
  it("lists the lead's estimates with a link to each and marks the one that counts", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/leads/lead-1/estimates', { suppressErrorToast: true });
    expect(w.find('[data-testid="lead-estimate-e1"] a').attributes('href')).toBe('/estimates/e1');
    expect(w.find('[data-testid="lead-estimate-e1"] [data-testid="lead-estimate-selected"]').exists()).toBe(true);
    expect(w.find('[data-testid="lead-estimate-e2"] [data-testid="lead-estimate-selected"]').exists()).toBe(false);
  });

  it('offers the pick only on other ACCEPTED estimates, and only to a writer', async () => {
    const w = mountPanel(true);
    await flushPromises();
    expect(w.find('[data-testid="lead-estimate-select-e2"]').exists()).toBe(true);
    expect(w.find('[data-testid="lead-estimate-select-e3"]').exists()).toBe(false); // draft
    expect(w.find('[data-testid="lead-estimate-select-e1"]').exists()).toBe(false); // already it
    const ro = mountPanel(false);
    await flushPromises();
    expect(ro.find('[data-testid="lead-estimate-select-e2"]').exists()).toBe(false);
  });

  it('moving the pick PUTs it, shows the new one, and tells the parent', async () => {
    apiPut.mockResolvedValue({ id: 'lead-1', selected_estimate_id: 'e2' });
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-testid="lead-estimate-select-e2"]').trigger('click');
    await flushPromises();
    expect(apiPut).toHaveBeenCalledWith(
      '/api/leads/lead-1/selected-estimate', { estimate_id: 'e2' }, { successMessage: 'Won estimate updated' },
    );
    expect(w.find('[data-testid="lead-estimate-e2"] [data-testid="lead-estimate-selected"]').exists()).toBe(true);
    expect(w.emitted('selected')[0][0]).toEqual({ id: 'lead-1', selected_estimate_id: 'e2' });
  });

  it('a lead with no estimates says how to get one, and a failed load says so', async () => {
    apiGet.mockResolvedValueOnce({ lead_id: 'lead-1', selected_estimate_id: null, estimates: [] });
    const empty = mountPanel();
    await flushPromises();
    expect(empty.find('[data-testid="lead-estimates-empty"]').exists()).toBe(true);
    apiGet.mockRejectedValueOnce(new Error('500'));
    const broken = mountPanel();
    await flushPromises();
    expect(broken.find('[data-testid="lead-estimates-error"]').exists()).toBe(true);
  });
});

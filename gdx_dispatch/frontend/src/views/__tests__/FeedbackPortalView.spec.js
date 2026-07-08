/**
 * FeedbackPortal list — 2026-07-08 throwaway-container verification catch.
 *
 * The view called api.get("/api/support/my", { params }) and read
 * res.data.items. useApi.get() has no `params` option (silently ignored)
 * and returns the parsed body directly — so the category filter never
 * applied and the success path threw on undefined `.data`, landing in the
 * catch: the portal showed "Could not load your submissions." on EVERY
 * load, even when the API returned tickets. Pins:
 *   1. Tickets from the response body render (res.items).
 *   2. The category filter reaches the URL as a query string.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const getMock = vi.fn();
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: getMock, post: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

import FeedbackPortalView from '../FeedbackPortalView.vue';

const STUBS = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Button: { template: '<button><slot /></button>' },
  InputText: { template: '<input />' },
  Textarea: { template: '<textarea />' },
  Select: { template: '<select></select>' },
  DataTable: {
    props: ['value'],
    template: '<table><tr v-for="row in value" :key="row.id"><td>{{ row.subject }}</td></tr></table>',
  },
  Column: { template: '<col />' },
  Tag: { template: '<span />' },
};

function mountView() {
  return mount(FeedbackPortalView, { global: { stubs: STUBS, plugins: [createPinia()] } });
}

describe('FeedbackPortalView list loading', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
  });

  it('renders tickets from the response body (res.items, not res.data.items)', async () => {
    getMock.mockResolvedValue({
      items: [
        { id: 't1', subject: 'First ticket', category: 'feature', status: 'open', priority: 'medium', created_at: '2026-07-08T02:00:00Z', closed_at: null, resolution_summary: null },
      ],
    });
    const w = mountView();
    await flushPromises();
    expect(w.text()).toContain('First ticket');
    expect(w.text()).not.toContain('Could not load');
    expect(getMock).toHaveBeenCalledWith('/api/support/my');
  });

  it('applies the category filter as a query string', async () => {
    getMock.mockResolvedValue({ items: [] });
    const w = mountView();
    await flushPromises();
    w.vm.categoryFilter = 'bug';
    await w.vm.fetchTickets();
    expect(getMock).toHaveBeenLastCalledWith('/api/support/my?category=bug');
  });
});

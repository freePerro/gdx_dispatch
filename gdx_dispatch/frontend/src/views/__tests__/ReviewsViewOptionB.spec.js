/**
 * #473, option B (owner, 2026-09-01): reviews live on Google.
 *
 * The page used to promise that "Customer reviews from Google, Yelp, and
 * Facebook will land in this inbox", offer a platform filter over sources
 * nothing ingests, and a "Flagged" tab over a column that does not exist.
 * Pins:
 *   1. The empty state and the page note say where reviews actually live and
 *      point at Settings for the Google link.
 *   2. The platform filter and the flagged toggle are gone, not hidden.
 *   3. Rows still render, and a row with no source reads "Unknown" — never a
 *      platform name it does not carry.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const getMock = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: getMock, post: vi.fn() }),
}));

import ReviewsView from '../ReviewsView.vue';

const STUBS = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Button: { template: '<button><slot /></button>' },
  DatePicker: { template: '<input />' },
  ProgressSpinner: { template: '<span />' },
  Rating: { props: ['value'], template: '<span class="rating">{{ value }}</span>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<button><slot /></button>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
  EmptyState: {
    props: ['title', 'message'],
    template: '<div data-testid="empty-state"><h3>{{ title }}</h3><p>{{ message }}</p></div>',
  },
  DataTable: {
    props: ['value'],
    template:
      '<div><slot v-if="!value || !value.length" name="empty" /><table><tr v-for="row in value" :key="row.id"><td>{{ row.customer }}</td><td>{{ row.content }}</td></tr></table></div>',
  },
  Column: { template: '<col />' },
  RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
};

function mountView() {
  return mount(ReviewsView, { global: { stubs: STUBS } });
}

describe('ReviewsView under option B', () => {
  beforeEach(() => {
    getMock.mockReset();
  });

  it('says reviews live on Google and points at Settings when there are none', async () => {
    getMock.mockResolvedValue({ items: [] });
    const w = mountView();
    await flushPromises();
    expect(w.find('[data-testid="empty-state"]').text()).toMatch(/live on Google/);
    expect(w.text()).not.toMatch(/Yelp|Facebook|inbox/i);
    const note = w.find('[data-testid="reviews-note"]');
    expect(note.exists()).toBe(true);
    expect(note.text()).toMatch(/ratings your office has recorded/);
    expect(w.find('[data-testid="reviews-settings-link"]').attributes('href')).toBe('/settings');
  });

  it('has no platform filter and no flagged toggle', async () => {
    getMock.mockResolvedValue({ items: [] });
    const w = mountView();
    await flushPromises();
    expect(w.find('[data-testid="reviews-source-filter"]').exists()).toBe(false);
    expect(w.find('[data-testid="reviews-flagged-toggle"]').exists()).toBe(false);
    expect(w.text()).not.toMatch(/Flagged/);
  });

  it('renders the office-recorded ratings the API returns', async () => {
    getMock.mockResolvedValue({
      items: [
        { id: 'r1', rating: 5, customer: 'Page Customer', content: 'Great door', source: null, created_at: '2026-08-31T00:00:00Z' },
        { id: 'r2', rating: 4, customer: 'Second', content: '', source: 'google', created_at: '2026-08-30T00:00:00Z' },
      ],
    });
    const w = mountView();
    await flushPromises();
    expect(getMock).toHaveBeenCalledWith('/api/reviews');
    expect(w.text()).toContain('Page Customer');
    expect(w.text()).toContain('Great door');
    expect(w.find('[data-testid="empty-state"]').exists()).toBe(false);
    expect(w.vm.sourceLabel(null)).toBe('Unknown');
    expect(w.vm.sourceLabel('google')).toBe('Google');
  });
});

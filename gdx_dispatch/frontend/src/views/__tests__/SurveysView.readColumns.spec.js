/**
 * Surveys list columns read only fields the router returns (GDXA-1709).
 *
 * Respondent read respondent_name || customer_name and Last Link read
 * last_sent_at; the router sent neither respondent_name nor last_sent_at, so
 * both columns were always "—". The router now returns both (test_surveys.py
 * guards that). Here: the view renders them, and no longer reads the names
 * the router never sends (customer_name, response, template.name).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

const apiGet = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    del: vi.fn().mockResolvedValue({}),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
  }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(), confirmDestructive: vi.fn() }),
}));

import SurveysView from '../SurveysView.vue';

const TEMPLATE = {
  id: 'tpl-1', name: 'Post-job NPS', kind: 'nps', question: 'Recommend us?', active: true,
  last_sent_at: '2026-10-18T14:00:00+00:00',
};
const RESPONSE = {
  id: 'r-1', template_name: 'Post-job NPS', respondent_name: 'Pat Example',
  score: 9, comment: 'Quick and tidy', created_at: '2026-10-18T15:00:00+00:00',
};

function mountView() {
  return mount(SurveysView, {
    global: { plugins: [PrimeVue], directives: { tooltip: {} } },
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/surveys/templates')) return Promise.resolve([TEMPLATE]);
    if (url.startsWith('/api/surveys/responses')) return Promise.resolve([RESPONSE]);
    return Promise.resolve({});
  });
});

describe('SurveysView list columns', () => {
  it('shows the last link date on the template row', async () => {
    const w = mountView();
    await flushPromises();
    expect(w.text()).toContain('2026-10-18');
  });

  it('shows the respondent name on the response row', async () => {
    const w = mountView();
    await flushPromises();
    const tab = w.findAll('[role="tab"]').find((t) => t.text() === 'Responses');
    await tab.trigger('click');
    await flushPromises();
    expect(w.text()).toContain('Pat Example');
    expect(w.text()).toContain('Quick and tidy');
  });

  it('does not read fields the router never returns', async () => {
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/surveys/templates')) return Promise.resolve([TEMPLATE]);
      if (url.startsWith('/api/surveys/responses')) {
        return Promise.resolve([{
          id: 'r-2', score: 7, created_at: '2026-10-18T15:00:00+00:00',
          customer_name: 'Ghost Name', response: 'Ghost comment', template: { name: 'Ghost template' },
        }]);
      }
      return Promise.resolve({});
    });
    const w = mountView();
    await flushPromises();
    const tab = w.findAll('[role="tab"]').find((t) => t.text() === 'Responses');
    await tab.trigger('click');
    await flushPromises();
    expect(w.text()).not.toContain('Ghost');
  });
});

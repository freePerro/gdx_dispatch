/**
 * Surveys template Delete asks before it deletes (GDXA-1689).
 *
 * deleteTemplate called `confirmAsync` without importing useDestructiveConfirm,
 * so the click threw a ReferenceError and Delete did nothing. Pinned here: the
 * click reaches the confirm, and the DELETE fires only when it resolves true.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

const apiGet = vi.fn();
const apiDel = vi.fn();
const confirmAsync = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    del: apiDel,
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
  }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync, confirmDestructive: vi.fn() }),
}));

import SurveysView from '../SurveysView.vue';

const TEMPLATE = { id: 'tpl-1', name: 'Post-job NPS', template_type: 'nps', send_channel: 'email' };

function mountView() {
  return mount(SurveysView, {
    global: {
      plugins: [PrimeVue],
      directives: { tooltip: {} },
    },
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiDel.mockReset().mockResolvedValue({});
  confirmAsync.mockReset();
  apiGet.mockImplementation((url) => Promise.resolve(url.startsWith('/api/surveys/templates') ? [TEMPLATE] : []));
});

describe('SurveysView template Delete', () => {
  it('asks to confirm and deletes when the user accepts', async () => {
    confirmAsync.mockResolvedValue(true);
    const w = mountView();
    await flushPromises();
    await w.find('button[aria-label="Delete"]').trigger('click');
    await flushPromises();
    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(apiDel).toHaveBeenCalledWith('/api/surveys/templates/tpl-1', { successMessage: 'Template deleted' });
  });

  it('does not delete when the user cancels', async () => {
    confirmAsync.mockResolvedValue(false);
    const w = mountView();
    await flushPromises();
    await w.find('button[aria-label="Delete"]').trigger('click');
    await flushPromises();
    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(apiDel).not.toHaveBeenCalled();
  });
});

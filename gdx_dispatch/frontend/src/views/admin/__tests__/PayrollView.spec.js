/**
 * GDXA-312 — admin PayrollView row Delete must ask before it deletes.
 *
 * useDestructiveConfirm is mocked to capture the options instead of running
 * accept: the real composable auto-accepts when no ConfirmationService is
 * registered, so an unmocked test could not tell "asked first" from "deleted
 * immediately". The dialog itself is only provable in a browser.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import Tooltip from 'primevue/tooltip';

const apiGet = vi.fn();
const apiDelete = vi.fn();
vi.mock('../../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    post: vi.fn(),
    patch: vi.fn(),
    delete: apiDelete,
  }),
}));

const confirmDestructive = vi.fn();
vi.mock('../../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive }),
}));

import PayrollView from '../PayrollView.vue';

const ENTRY = {
  id: 'entry-1',
  tech_user_id: 'tech-42',
  period_start: '2026-09-01T00:00:00',
  period_end: '2026-09-14T00:00:00',
  hours_paid: 80,
  gross_pay: 2400,
  source: 'manual',
};

async function mountView() {
  const wrapper = mount(PayrollView, {
    global: { plugins: [PrimeVue], directives: { tooltip: Tooltip } },
  });
  await flushPromises();
  return wrapper;
}

describe('admin PayrollView delete', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiGet.mockImplementation(async (url) =>
      url === '/api/payroll/entries'
        ? { items: [ENTRY] }
        : { payroll_source: 'manual', candidates: ['manual'] });
    apiDelete.mockResolvedValue({});
  });

  it('asks for confirmation and does not delete on click', async () => {
    const wrapper = await mountView();
    await wrapper.find('button[aria-label="Delete"]').trigger('click');
    await flushPromises();

    expect(confirmDestructive).toHaveBeenCalledTimes(1);
    expect(apiDelete).not.toHaveBeenCalled();
    const opts = confirmDestructive.mock.calls[0][0];
    expect(opts.header).toBe('Delete payroll entry?');
    expect(opts.message).toContain('tech-42');
    expect(opts.message).toContain('80 h, $2,400.00');
  });

  it('deletes the entry only when the confirmation is accepted', async () => {
    const wrapper = await mountView();
    await wrapper.find('button[aria-label="Delete"]').trigger('click');
    await confirmDestructive.mock.calls[0][0].accept();
    await flushPromises();

    expect(apiDelete).toHaveBeenCalledTimes(1);
    expect(apiDelete.mock.calls[0][0]).toBe('/api/payroll/entries/entry-1');
  });
});

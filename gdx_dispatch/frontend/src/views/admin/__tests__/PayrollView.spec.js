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
const apiPatch = vi.fn();
vi.mock('../../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    post: vi.fn(),
    patch: apiPatch,
    delete: apiDelete,
  }),
}));

const confirmDestructive = vi.fn();
vi.mock('../../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive }),
}));

import Select from 'primevue/select';
import Tag from 'primevue/tag';
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

// GDXA-1519 — the source Select must not keep showing a value the server
// rejected, and an accepted change says so.
describe('admin PayrollView payroll source', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiGet.mockImplementation(async (url) =>
      url === '/api/payroll/entries'
        ? { items: [] }
        : { payroll_source: 'manual', candidates: ['manual', 'timeclock'] });
  });

  // updateModel is what PrimeVue's own option click calls: it writes the
  // v-model and fires @change, the same order the page sees in a browser.
  async function pickSource(wrapper, value) {
    const select = wrapper.findComponent(Select);
    select.vm.updateModel(null, value);
    await flushPromises();
    return select;
  }

  const shownLabel = (wrapper) =>
    wrapper.findComponent(Select).find('[data-pc-section="label"]').text();

  it('reverts the Select and header Tag when the PATCH is rejected', async () => {
    apiPatch.mockRejectedValue(new Error('422 invalid payroll_source'));
    const wrapper = await mountView();
    const select = await pickSource(wrapper, 'timeclock');

    expect(apiPatch).toHaveBeenCalledTimes(1);
    expect(apiPatch.mock.calls[0][1]).toEqual({ payroll_source: 'timeclock' });
    expect(select.props('modelValue')).toBe('manual');
    expect(shownLabel(wrapper)).toBe('manual');
    expect(wrapper.findComponent(Tag).props('value')).toBe('manual');
  });

  it('passes a success toast and keeps the new value when the PATCH succeeds', async () => {
    apiPatch.mockResolvedValue({ payroll_source: 'timeclock' });
    const wrapper = await mountView();
    const select = await pickSource(wrapper, 'timeclock');

    expect(apiPatch.mock.calls[0][2]).toEqual({ successMessage: 'Payroll source set to timeclock' });
    expect(select.props('modelValue')).toBe('timeclock');
    expect(shownLabel(wrapper)).toBe('timeclock');
    expect(wrapper.findComponent(Tag).props('value')).toBe('timeclock');
  });

  it('reverts to the last accepted source, not the page default', async () => {
    apiPatch.mockResolvedValueOnce({}).mockRejectedValueOnce(new Error('500'));
    const wrapper = await mountView();
    await pickSource(wrapper, 'timeclock');
    const select = await pickSource(wrapper, 'manual');

    expect(select.props('modelValue')).toBe('timeclock');
  });

  it('disables the Select while a save is in flight so saves cannot overlap', async () => {
    let settle;
    apiPatch.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const wrapper = await mountView();
    const select = await pickSource(wrapper, 'timeclock');
    expect(select.props('disabled')).toBe(true);

    settle({});
    await flushPromises();
    expect(select.props('disabled')).toBe(false);
  });

  it('keeps the saved source when a Refresh lands while the save is in flight', async () => {
    let settle;
    apiPatch.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const wrapper = await mountView();
    await pickSource(wrapper, 'timeclock');

    // The refresh reads the config before the PATCH has landed server-side.
    await wrapper.find('button[aria-label="Refresh"]').trigger('click');
    await flushPromises();
    expect(shownLabel(wrapper)).toBe('manual');

    settle({});
    await flushPromises();
    expect(shownLabel(wrapper)).toBe('timeclock');
    expect(wrapper.findComponent(Tag).props('value')).toBe('timeclock');
  });
});

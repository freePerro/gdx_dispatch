/**
 * EstimateView — the header autosave carries the version it loaded (GDXA-450).
 *
 * The autosave PATCHes the whole header on every flush, so an editor left open
 * overwrote a colleague's later save. It now sends expected_version; a 409
 * stops autosave and offers Reload. Every write bumps the version, so the view
 * follows its OWN writes ({before, after} in each response) and never 409s
 * itself, and loading the page sends nothing (a no-op flush would bump the
 * version and 409 a colleague who has the estimate open).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();
const routeMock = { params: { id: 'est-1' }, query: {} };

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useAuthedFile', () => ({
  openAuthedFile: vi.fn(),
  createAuthedBlobUrl: vi.fn(),
}));
vi.mock('../../composables/useEstimateSources', async () => {
  const { ref } = await import('vue');
  return {
    classifyPickerError: () => 'unknown',
    useEstimateSources: () => ({ sources: ref([]), discover: vi.fn() }),
  };
});
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => routeMock,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import EstimateView from '../EstimateView.vue';

const ESTIMATE = {
  id: 'est-1', estimate_number: 'EST-0001', status: 'draft', customer_id: 'cust-1',
  lines: [], created_at: '2026-09-01T12:00:00Z', valid_until: null, version: 3,
};

let estimateRow;

function routeGet(url) {
  if (url === '/api/estimates/est-1') return estimateRow;
  if (url.startsWith('/api/estimates/est-1/activity')) return { items: [], total: 0, context: null };
  if (url === '/api/estimates-features') return { estimate_expiry_days: 45 };
  if (url === '/api/tax/config' || url === '/api/pricing-engine/settings') return {};
  return [];
}

const stub = (tag = 'div') => ({ template: `<${tag}><slot /></${tag}>` });
const stubs = {
  RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
  Button: { props: ['label'], template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>', inheritAttrs: false },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
  Select: { props: ['modelValue', 'options'], template: '<select></select>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  ToggleSwitch: { props: ['modelValue'], template: '<input type="checkbox" />' },
  Checkbox: { props: ['modelValue'], template: '<input type="checkbox" />' },
  RadioButton: { props: ['modelValue'], template: '<input type="radio" />' },
  DatePicker: {
    props: ['modelValue', 'placeholder'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :placeholder="placeholder" />',
    inheritAttrs: false,
  },
  DataTable: stub(), Column: stub('span'), Divider: { template: '<hr />' },
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Toast: stub(), EstimateProfitPanel: stub(), CatalogPickerDialog: stub(),
  ComposerPdfPreview: stub(), EstimateStatusContext: stub(), PluginScreen: stub(),
  CustomerFormDialog: stub(), PhoneInput: { template: '<input />' }, PaymentCaptureForm: stub(),
};

async function settle() {
  await flushPromises();
  await vi.advanceTimersByTimeAsync(1000);
  await flushPromises();
}

async function mountAndSettle() {
  const wrapper = mount(EstimateView, { global: { stubs, directives: { tooltip: {} } } });
  await flushPromises();
  // The load reassigns the form, which arms the 800 ms autosave debounce.
  await vi.advanceTimersByTimeAsync(1000);
  await flushPromises();
  return wrapper;
}

function headerPatches() {
  return apiPatch.mock.calls.filter(([url]) => url === '/api/estimates/est-1').map(([, body]) => body);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  setActivePinia(createPinia());
  estimateRow = { ...ESTIMATE };
  apiGet.mockReset().mockImplementation(async (url) => routeGet(url));
  apiPatch.mockReset().mockResolvedValue({});
  routeMock.params = { id: 'est-1' };
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

function conflict() {
  const err = new Error('This estimate was changed by someone else. Reload to see their changes.');
  err.status = 409;
  err.code = 'estimate_version_conflict';
  return err;
}

describe('EstimateView — optimistic concurrency on the header autosave', () => {
  it('loading the estimate sends no write at all', async () => {
    await mountAndSettle();
    expect(apiPatch).not.toHaveBeenCalled();
    expect(apiPost).not.toHaveBeenCalled();
  });

  it('an edit sends the loaded version as expected_version', async () => {
    const wrapper = await mountAndSettle();
    wrapper.vm.form.notes = 'edit';
    await settle();
    expect(headerPatches().at(-1)).toMatchObject({ notes: 'edit', expected_version: 3 });
  });

  it("follows its own header and line writes, so the next save does not 409 itself", async () => {
    apiPatch.mockImplementation(async (url) => (
      url === '/api/estimates/est-1' ? { version_before: 3, version: 4 } : {}
    ));
    apiPost.mockImplementation(async (url) => (
      url === '/api/estimates/est-1/lines'
        ? { id: 'line-1', estimate_version_before: 4, estimate_version: 5 }
        : {}
    ));
    const wrapper = await mountAndSettle();
    wrapper.vm.form.line_items[0].description = 'Spring';
    wrapper.vm.form.line_items[0].unit_price = 50;
    await settle();
    expect(headerPatches().at(-1).expected_version).toBe(3);

    apiPatch.mockClear();
    wrapper.vm.form.notes = 'next';
    await settle();
    expect(headerPatches().at(-1).expected_version).toBe(5);
  });

  it("a write that started from someone else's version is not adopted", async () => {
    // The colleague moved 3 -> 4; this view's line write went 4 -> 5.
    apiPost.mockImplementation(async (url) => (
      url === '/api/estimates/est-1/lines'
        ? { id: 'line-1', estimate_version_before: 4, estimate_version: 5 }
        : {}
    ));
    const wrapper = await mountAndSettle();
    wrapper.vm.form.line_items[0].description = 'Spring';
    wrapper.vm.form.line_items[0].unit_price = 50;
    await settle();
    apiPatch.mockClear();
    wrapper.vm.form.notes = 'next';
    await settle();
    expect(headerPatches().at(-1).expected_version).toBe(3);
  });

  it('emailing the estimate is followed, so the next edit does not 409 against the send', async () => {
    apiPost.mockImplementation(async (url) => (
      url === '/api/estimates/est-1/send'
        ? { email_sent: true, status: 'sent', version_before: 3, version: 4 }
        : {}
    ));
    const wrapper = await mountAndSettle();
    wrapper.vm.composer.customer_id = 'cust-1';
    wrapper.vm.composer.to = 'customer@example.com';
    await wrapper.vm.sendComposer();
    await settle();
    expect(apiPost).toHaveBeenCalledWith('/api/estimates/est-1/send', expect.anything());

    wrapper.vm.form.notes = 'typo fix after sending';
    await settle();
    expect(headerPatches().at(-1).expected_version).toBe(4);
  });

  it('decline then reopen are both followed, so the edit after reopening does not 409', async () => {
    apiPost.mockImplementation(async (url) => {
      if (url === '/api/estimates/est-1/decline') return { status: 'declined', declined_reason: 'Price', version_before: 3, version: 4 };
      if (url === '/api/estimates/est-1/reopen') return { status: 'draft', price_drift: [], version_before: 4, version: 5 };
      return {};
    });
    const wrapper = await mountAndSettle();
    wrapper.vm.declineReason = 'Price';
    await wrapper.vm.doDeclineEstimate();
    await wrapper.vm.reopenEstimate();
    await settle();

    wrapper.vm.form.notes = 'edit after reopening';
    await settle();
    expect(headerPatches().at(-1).expected_version).toBe(5);
  });

  it('typing pending at a proposal-mode toggle is saved before it, under the token it held', async () => {
    let version = 3;
    apiPatch.mockImplementation(async (url) => {
      if (url !== '/api/estimates/est-1') return {};
      version += 1;
      return { version_before: version - 1, version };
    });
    const wrapper = await mountAndSettle();
    wrapper.vm.form.notes = 'typed just before the toggle';
    await wrapper.vm.onProposalModeToggle(true);
    const [typing, toggle] = headerPatches();
    expect(typing).toMatchObject({ notes: 'typed just before the toggle', expected_version: 3 });
    expect(toggle).toEqual({ proposal_mode: true });

    wrapper.vm.form.notes = 'after the toggle';
    await settle();
    expect(headerPatches().at(-1).expected_version).toBe(5);
  });

  it('a save armed while the toggle is in flight waits for its new token', async () => {
    let releaseToggle;
    apiPatch.mockImplementation((url, body) => {
      if (url === '/api/estimates/est-1' && 'proposal_mode' in body) {
        return new Promise((resolve) => { releaseToggle = () => resolve({ version_before: 3, version: 4 }); });
      }
      return Promise.resolve({});
    });
    const wrapper = await mountAndSettle();
    const toggling = wrapper.vm.onProposalModeToggle(true);
    await flushPromises();
    wrapper.vm.form.notes = 'typed during the toggle';
    await settle();
    expect(headerPatches().filter((b) => 'notes' in b)).toHaveLength(0);

    releaseToggle();
    await toggling;
    await settle();
    expect(headerPatches().at(-1)).toMatchObject({ notes: 'typed during the toggle', expected_version: 4 });
  });

  it('a toggle whose flush outlives the view is never sent to the next estimate', async () => {
    let releaseTyping;
    apiPatch.mockImplementation((url, body) => {
      if (url === '/api/estimates/est-1' && 'notes' in body) {
        return new Promise((resolve) => { releaseTyping = () => resolve({}); });
      }
      return Promise.resolve({});
    });
    const wrapper = await mountAndSettle();
    wrapper.vm.form.notes = 'typed';
    const toggling = wrapper.vm.onProposalModeToggle(true);
    await flushPromises();
    routeMock.params = { id: 'est-2' };
    wrapper.unmount();
    releaseTyping();
    await toggling;
    expect(apiPatch.mock.calls.filter(([, body]) => 'proposal_mode' in body)).toHaveLength(0);
  });

  it('a save waits for every pending toggle, even when they answer out of order', async () => {
    const release = [];
    apiPatch.mockImplementation((url, body) => {
      if (url === '/api/estimates/est-1' && 'proposal_mode' in body) {
        return new Promise((resolve) => { release.push(resolve); });
      }
      return Promise.resolve({});
    });
    const wrapper = await mountAndSettle();
    const first = wrapper.vm.onProposalModeToggle(true);
    await flushPromises();
    const second = wrapper.vm.onProposalModeToggle(false);
    await flushPromises();
    release[1]({ version_before: 3, version: 4 });
    await second;
    wrapper.vm.form.notes = 'typed between the answers';
    await settle();
    expect(headerPatches().filter((b) => 'notes' in b)).toHaveLength(0);

    release[0]({ version_before: 4, version: 5 });
    await first;
    await settle();
    expect(headerPatches().at(-1)).toMatchObject({ notes: 'typed between the answers', expected_version: 5 });
  });

  it('after a refused save the toggle is not sent and the switch goes back', async () => {
    apiPatch.mockImplementation(async (url, body) => {
      if (url === '/api/estimates/est-1' && 'notes' in body) throw conflict();
      return {};
    });
    const wrapper = await mountAndSettle();
    wrapper.vm.form.notes = 'stale edit';
    wrapper.vm.proposalMode = true;
    await wrapper.vm.onProposalModeToggle(true);
    expect(wrapper.vm.autosaveState).toBe('error');
    expect(apiPatch.mock.calls.filter(([, body]) => 'proposal_mode' in body)).toHaveLength(0);
    expect(wrapper.vm.proposalMode).toBe(false);
  });

  it('a 409 stops autosave and offers Reload, which reloads and saves again', async () => {
    apiPatch.mockImplementation(async (url) => {
      if (url === '/api/estimates/est-1') throw conflict();
      return {};
    });
    const wrapper = await mountAndSettle();
    wrapper.vm.form.notes = 'stale edit';
    await settle();
    expect(wrapper.vm.autosaveState).toBe('error');
    expect(wrapper.vm.autosaveLabel).toContain('changed by someone else');
    const reload = wrapper.get('[data-testid="estimate-version-conflict-reload"]');

    apiPatch.mockClear();
    wrapper.vm.form.notes = 'more typing';
    await settle();
    expect(apiPatch).not.toHaveBeenCalled();

    estimateRow = { ...ESTIMATE, notes: 'their edit', version: 7 };
    apiPatch.mockReset().mockResolvedValue({});
    await reload.trigger('click');
    await settle();
    expect(wrapper.vm.form.notes).toBe('their edit');
    expect(wrapper.find('[data-testid="estimate-version-conflict-reload"]').exists()).toBe(false);
    expect(apiPatch).not.toHaveBeenCalled();

    wrapper.vm.form.notes = 'after reload';
    await settle();
    expect(headerPatches().at(-1).expected_version).toBe(7);
  });
});

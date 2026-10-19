/**
 * Custom field Delete asks before it deletes (GDXA-1691).
 *
 * confirmDelete called `confirmAsync` without importing useDestructiveConfirm,
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

import CustomFieldsView from '../CustomFieldsView.vue';

const DEF = { id: 'cf-1', entity_type: 'customer', field_key: 'gate_code', label: 'Gate code', field_type: 'text', options: null, required: false, sort_order: 0 };

function mountView() {
  return mount(CustomFieldsView, {
    global: {
      plugins: [PrimeVue],
      directives: { tooltip: {} },
    },
  });
}

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue([DEF]);
  apiDel.mockReset().mockResolvedValue({});
  confirmAsync.mockReset();
});

describe('CustomFieldsView Delete', () => {
  it('asks to confirm and deletes when the user accepts', async () => {
    confirmAsync.mockResolvedValue(true);
    const w = mountView();
    await flushPromises();
    await w.find('button[aria-label="Delete"]').trigger('click');
    await flushPromises();
    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(apiDel).toHaveBeenCalledWith('/api/custom-fields/cf-1', { successMessage: 'Custom field deleted' });
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

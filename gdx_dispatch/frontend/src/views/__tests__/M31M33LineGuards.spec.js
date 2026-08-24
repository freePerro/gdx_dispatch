/**
 * M31+M33 (money audit 2026-08-04): the on-screen total and the submitted
 * payload must never disagree silently.
 *
 * - M31: a cleared quantity (InputNumber leaves null) rendered $0.00 in the
 *   on-screen total and then submitted as quantity: 1 — the invoice was
 *   created $650 higher than the total the operator approved.
 * - M33: the submit filter dropped zero/negative lines the displayed subtotal
 *   summed — screen $450, invoice $500.
 *
 * ChangeOrdersView is driven behaviorally here. EstimateView has no mount
 * harness (its own specs say so) — its guards are source-pinned, and each pin
 * was counterfactually verified to fail with the guard reverted.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: vi.fn() }),
}));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {}, params: {} }),
  useRouter: () => ({ push: vi.fn() }),
}));

import ChangeOrdersView from '../ChangeOrdersView.vue';

const stubs = {
  Button: { props: ['label', 'disabled', 'loading'], emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>' },
  Dialog: { props: ['visible'], template: '<div><slot /><slot name="footer" /></div>' },
  DataTable: { template: '<div><slot /></div>' },
  Column: { template: '<div />' },
  InputText: { props: ['modelValue'], template: '<input />' },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  Select: { props: ['modelValue'], template: '<select />' },
  Tag: { template: '<span />' },
  Message: { template: '<div><slot /></div>' },
};

describe('ChangeOrdersView — M31+M33 guards', () => {
  beforeEach(() => {
    apiGet.mockReset(); apiPost.mockReset(); toastAdd.mockReset();
    apiGet.mockResolvedValue([]);
    apiPost.mockResolvedValue({ id: 'co-1' });
  });

  async function mountWithLines(lines) {
    const wrapper = mount(ChangeOrdersView, { global: { stubs } });
    await flushPromises();
    wrapper.vm.form.title = 'Extra spring';
    wrapper.vm.form.job_id = 'job-1';
    wrapper.vm.form.line_items = lines;
    return wrapper;
  }

  it('a cleared quantity refuses the save — never billed as 1', async () => {
    const wrapper = await mountWithLines([
      { description: 'Strut', quantity: null, unit_price: 85 },
    ]);
    await wrapper.vm.saveCo();
    expect(apiPost).not.toHaveBeenCalled();
    const warn = toastAdd.mock.calls.find(([t]) => t.severity === 'warn');
    expect(warn).toBeTruthy();
    expect(warn[0].detail).toContain('Strut');
  });

  it('a negative line the subtotal sums refuses instead of vanishing', async () => {
    const wrapper = await mountWithLines([
      { description: 'Goodwill credit', quantity: 1, unit_price: -40 },
      { description: 'Strut', quantity: 1, unit_price: 85 },
    ]);
    await wrapper.vm.saveCo();
    expect(apiPost).not.toHaveBeenCalled();
  });

  it('a typed flat amount survives zero-priced-only lines (audit regression)', async () => {
    const wrapper = await mountWithLines([
      { description: 'Scope note', quantity: 1, unit_price: 0 },
    ]);
    wrapper.vm.form.amount = 250;
    await wrapper.vm.saveCo();
    expect(apiPost).toHaveBeenCalled();
    const [, payload] = apiPost.mock.calls[0];
    expect(payload.amount).toBe(250);
    expect(payload.line_items).toEqual([]);
  });

  it('a zero-priced described line now SUBMITS (the display always summed it)', async () => {
    const wrapper = await mountWithLines([
      { description: 'No-charge adjustment', quantity: 3, unit_price: 0 },
    ]);
    await wrapper.vm.saveCo();
    expect(apiPost).toHaveBeenCalled();
    const [, payload] = apiPost.mock.calls[0];
    expect(payload.line_items[0]).toMatchObject({ description: 'No-charge adjustment', quantity: 3, unit_price: 0 });
  });
});

describe('EstimateView — M31+M33 pins (no mount harness; CF-verified)', () => {
  const SRC = readFileSync(join(__dirname, '..', 'EstimateView.vue'), 'utf8');

  it('the payload build is preceded by the divergence refusal (CODE, not a comment)', () => {
    // Audit: the first pin anchored a comment string — deleting the guard
    // while keeping its comment passed. Anchor the executable check.
    const guard = SRC.indexOf('const evBadQty = described.filter(');
    const returnStmt = SRC.indexOf('return;', guard);
    const payload = SRC.indexOf('line_items: form.value.line_items');
    expect(guard).toBeGreaterThan(-1);
    expect(returnStmt).toBeGreaterThan(guard);
    expect(payload).toBeGreaterThan(returnStmt);
  });

  it('the AUTOSAVE flush gate holds a cleared quantity (the sixth surface)', () => {
    // The audit found autosave — the dominant path for existing estimates —
    // unguarded: a cleared quantity PATCHed null into a NOT NULL column and
    // 500d mid-flush while the screen showed $0.00.
    expect(SRC).toContain('unit_price != null && Number(li.quantity) > 0');
  });

  it('the payload filter keeps zero-priced lines the display sums', () => {
    expect(SRC).toContain('.filter((li) => li.description && Number(li.unit_price || 0) >= 0)');
    expect(SRC).not.toContain('.filter((li) => li.description && li.unit_price > 0)');
  });

  it('tier-line saves throw the quantity guard instead of || 1', () => {
    expect(SRC).not.toContain('quantity: Number(draft.quantity || 1)');
    expect(SRC).not.toContain('quantity: Number(line.quantity || 1)');
    expect((SRC.match(/_qtyGuard: true/g) || []).length).toBeGreaterThanOrEqual(2);
  });
});

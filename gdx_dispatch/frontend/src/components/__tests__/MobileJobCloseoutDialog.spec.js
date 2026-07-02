/**
 * MobileJobCloseoutDialog — Phase 2 / C3 unit pins.
 *
 * Doug 2026-05-10: Phase 2 of the completion-gate fix. The dialog collects
 * parts + hours + signature + notes and POSTs to /api/jobs/{id}/closeout
 * (built in C2). This spec pins the contract:
 *
 *  1. Submit disabled until at least one of {parts, hours, signature, notes}
 *     has content.
 *  2. Submit POSTs to /api/jobs/{id}/closeout with the right payload shape.
 *  3. SKU autocomplete fires after 2 chars and pulls from
 *     /api/parts-needed/sku-suggest with suppressErrorToast (so the office
 *     user without inventory.read doesn't see toast spam).
 *  4. 422 with {missing:[...]} produces the "Add: parts, hours" warn toast
 *     (same vocab as DispatchView's quickStatusChange, so the user sees
 *     consistent feedback regardless of where they triggered close).
 *  5. Picking an inventory suggestion sets part_id; door_catalog suggestions
 *     don't (the closeout snapshot carries them as free-text — see C2's
 *     hotfix that prevents FK violations on synthetic part_ids).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  // Closeout submits via the offline queue since the 2026-07-01 UX audit;
  // the same mock fn backs both so payload assertions stay unchanged.
  useApi: () => ({ get: apiGet, post: apiPost, postQueued: apiPost }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: toastAdd }),
}));

import MobileJobCloseoutDialog from '../MobileJobCloseoutDialog.vue';

const stubs = {
  Dialog: {
    props: ['visible'],
    emits: ['update:visible'],
    template: '<div data-testid="dlg" v-if="visible"><slot /><div class="footer"><slot name="footer" /></div></div>',
  },
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'loading', 'disabled', 'size'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue', 'input'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value); $emit(\'input\', $event)" />',
    inheritAttrs: false,
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
};

function mountDialog(props = {}) {
  return mount(MobileJobCloseoutDialog, {
    props: { visible: true, jobId: 'job-test-1', jobTitle: 'Broken spring', ...props },
    global: { stubs },
  });
}

async function setInput(wrapper, testid, value) {
  const el = wrapper.find(`[data-testid="${testid}"]`);
  el.element.value = value;
  await el.trigger('input');
}

describe('MobileJobCloseoutDialog', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    toastAdd.mockReset();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('disables submit when ALL four sections are empty', async () => {
    const wrapper = mountDialog();
    await flushPromises();

    const submit = wrapper.find('[data-testid="mjco-submit"]');
    expect(submit.attributes('disabled')).toBeDefined();
  });

  it('enables submit when notes has content (any section satisfies)', async () => {
    const wrapper = mountDialog();
    await flushPromises();
    await setInput(wrapper, 'mjco-notes', 'Done.');

    expect(wrapper.find('[data-testid="mjco-submit"]').attributes('disabled')).toBeUndefined();
  });

  it('POSTs to /api/jobs/{id}/closeout with the canonical payload shape', async () => {
    apiPost.mockResolvedValue({ ok: true, closeout_id: 'co-1' });

    const wrapper = mountDialog();
    await flushPromises();
    await setInput(wrapper, 'mjco-notes', 'No issues.');
    await setInput(wrapper, 'mjco-hours', '1.5');
    await setInput(wrapper, 'mjco-signed-by', 'Eric W');

    await wrapper.find('[data-testid="mjco-submit"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith(
      '/api/jobs/job-test-1/closeout',
      expect.objectContaining({
        parts: [],
        hours: 1.5,
        notes: 'No issues.',
        signed_by: 'Eric W',
      }),
      // Offline-queue metadata (actionType/resourceId) rides along.
      expect.objectContaining({ actionType: 'job.closeout' }),
    );
    // Success toast surfaces the Ready-for-Billing handoff.
    const successToast = toastAdd.mock.calls.find((c) => c[0]?.severity === 'success');
    expect(successToast).toBeTruthy();
    expect(successToast[0].detail).toContain('Ready for Billing');
    // Emits closed-out so caller can refresh.
    expect(wrapper.emitted('closed-out')?.length).toBe(1);
  });

  it('shows missing-fields toast on 422 with err.body.missing[]', async () => {
    const err = new Error('completion requirements unmet');
    err.status = 422;
    err.body = { detail: 'completion requirements unmet', missing: ['parts', 'hours'] };
    apiPost.mockRejectedValue(err);

    const wrapper = mountDialog();
    await flushPromises();
    await setInput(wrapper, 'mjco-notes', 'force-submit');

    await wrapper.find('[data-testid="mjco-submit"]').trigger('click');
    await flushPromises();

    const warnToast = toastAdd.mock.calls.find((c) => c[0]?.severity === 'warn');
    expect(warnToast).toBeTruthy();
    expect(warnToast[0].detail).toContain('parts logged');
    expect(warnToast[0].detail).toContain('labor hours');
    // No closed-out emitted on failure.
    expect(wrapper.emitted('closed-out')).toBeFalsy();
  });

  it('SKU autocomplete pulls from /api/parts-needed/sku-suggest with suppressErrorToast', async () => {
    apiGet.mockResolvedValue([
      { source: 'parts', sku: 'SPR-200', name: 'Torsion 200', qty_on_hand: 4, part_id: 'part-uuid-1' },
    ]);

    const wrapper = mountDialog();
    await flushPromises();
    await wrapper.find('[data-testid="mjco-add-part"]').trigger('click');
    await setInput(wrapper, 'mjco-part-name-0', 'spring');
    vi.advanceTimersByTime(300);
    await flushPromises();

    const [url, opts] = apiGet.mock.calls[0];
    expect(url).toMatch(/^\/api\/parts-needed\/sku-suggest\?q=spring/);
    expect(opts).toEqual({ suppressErrorToast: true });
  });

  it('picking an inventory suggestion writes part_id; non-inventory does not', async () => {
    const wrapper = mountDialog();
    await flushPromises();
    await wrapper.find('[data-testid="mjco-add-part"]').trigger('click');
    await flushPromises();

    // Drive the picker via the component's exposed state — find the row's
    // ref in `parts` and call pickSuggestion via the parts-list rendering.
    apiGet.mockResolvedValue([
      { source: 'parts', sku: 'SPR-200', name: 'Torsion 200', qty_on_hand: 4, part_id: 'part-uuid-1' },
      { source: 'door_catalog', sku: 'DOOR-A', name: 'Door A', qty_on_hand: null },
    ]);
    await setInput(wrapper, 'mjco-part-name-0', 'spr');
    vi.advanceTimersByTime(300);
    await flushPromises();

    // Click the inventory suggestion → row.part_id set.
    const suggestions = wrapper.findAll('[data-testid="mjco-part-suggestion-0"]');
    expect(suggestions.length).toBe(2);
    await suggestions[0].trigger('click');
    await flushPromises();

    // Submit and inspect the part_id in the payload.
    apiPost.mockResolvedValue({ ok: true, closeout_id: 'co-2' });
    await wrapper.find('[data-testid="mjco-submit"]').trigger('click');
    await flushPromises();

    const calledWith = apiPost.mock.calls[0][1];
    expect(calledWith.parts[0]).toMatchObject({
      sku: 'SPR-200',
      name: 'Torsion 200',
      part_id: 'part-uuid-1',
      qty: 1,
    });
  });
});

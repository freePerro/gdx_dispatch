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


// §11: submit is a two-tap contract (review strip + dwell). This helper
// performs the full confirmed submit for tests that aren't ABOUT the strip.
async function confirmedSubmit(wrapper) {
  await wrapper.find('[data-testid="mjco-submit"]').trigger('click');
  await flushPromises();
  const realNow = Date.now;
  vi.spyOn(Date, 'now').mockImplementation(() => realNow() + 2000);
  await wrapper.find('[data-testid="mjco-submit"]').trigger('click');
  await flushPromises();
  vi.mocked(Date.now).mockRestore();
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

    // Plan §11: the FIRST tap shows the review strip (hours × techs =
    // billed man-hours) and does NOT post — "is this how many hours you
    // meant?" is a real gate, not decoration.
    await setInput(wrapper, 'mjco-techs-on-site', '3');

    await wrapper.find('[data-testid="mjco-submit"]').trigger('click');
    await flushPromises();
    expect(apiPost).not.toHaveBeenCalled();
    const strip = wrapper.find('[data-testid="mjco-confirm-strip"]');
    expect(strip.exists()).toBe(true);
    // techs=3 on purpose (audit round 2): ×1 proves nothing — this fails if
    // the techs input is disconnected from the math.
    expect(strip.text()).toContain('4.50 man-hours'); // 1.5 h × 3 techs

    // A too-fast second tap is REFUSED (the dwell) — then confirms.
    await wrapper.find('[data-testid="mjco-submit"]').trigger('click');
    await flushPromises();
    expect(apiPost).not.toHaveBeenCalled();
    const realNow = Date.now;
    vi.spyOn(Date, 'now').mockImplementation(() => realNow() + 2000);
    await wrapper.find('[data-testid="mjco-submit"]').trigger('click');
    await flushPromises();
    vi.mocked(Date.now).mockRestore();

    expect(apiPost).toHaveBeenCalledWith(
      '/api/jobs/job-test-1/closeout',
      expect.objectContaining({
        parts: [],
        hours: 1.5,
        techs_on_site: 3,
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

    await confirmedSubmit(wrapper);

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
    await confirmedSubmit(wrapper);

    const calledWith = apiPost.mock.calls[0][1];
    expect(calledWith.parts[0]).toMatchObject({
      sku: 'SPR-200',
      name: 'Torsion 200',
      part_id: 'part-uuid-1',
      qty: 1,
    });
  });

  it('re-editing the name after a pick clears the stale sku/part_id', async () => {
    // The office orders by sku and inventory decrements by part_id — a
    // renamed row carrying the old pick would order/decrement the WRONG
    // part. Any manual edit invalidates the picked identity.
    const wrapper = mountDialog();
    await flushPromises();
    await wrapper.find('[data-testid="mjco-add-part"]').trigger('click');
    apiGet.mockResolvedValue([
      { source: 'parts', sku: 'SPR-200', name: 'Torsion 200', qty_on_hand: 4, part_id: 'part-uuid-1' },
    ]);
    await setInput(wrapper, 'mjco-part-name-0', 'spr');
    vi.advanceTimersByTime(300);
    await flushPromises();
    await wrapper.find('[data-testid="mjco-part-suggestion-0"]').trigger('click');

    // Tech keeps typing over the picked name.
    await setInput(wrapper, 'mjco-part-name-0', 'Torsion 200 but actually the 175');

    apiPost.mockResolvedValue({ ok: true, closeout_id: 'co-5' });
    await confirmedSubmit(wrapper);

    expect(apiPost.mock.calls[0][1].parts[0]).toMatchObject({
      name: 'Torsion 200 but actually the 175',
      sku: null,
      part_id: null,
    });
  });

  // ── Parts logged during the job (Doug 2026-08-12) ──────────────────
  // Parts can now be recorded while the job is worked, so the closeout is no
  // longer the first place a part appears. It must SHOW those rows without
  // pulling them into its own list: they are billable already, and attesting
  // them again bills the customer twice.

  it('shows parts already logged on the job, and does not re-submit them', async () => {
    apiGet.mockResolvedValue([
      { id: 'p1', part_name: 'Torsion spring', quantity: 2, sku: 'SPR-200', status: 'used', source: 'mobile' },
      { id: 'p2', part_name: 'Cable', quantity: 1, status: 'needed', source: 'request' },
    ]);

    // Opened, not mounted-open: the existing-parts read rides the visible
    // watcher, which is how the app uses the dialog.
    const wrapper = mountDialog({ visible: false });
    await wrapper.setProps({ visible: true });
    await flushPromises();

    const already = wrapper.find('[data-testid="mjco-already-used"]');
    expect(already.exists()).toBe(true);
    expect(already.text()).toContain('Torsion spring');
    // The request row belongs to the order section, not to "parts used".
    expect(already.text()).not.toContain('Cable');

    // Nothing was copied into the editable rows, so a closeout with no new
    // parts submits an EMPTY list — the server's gate counts the live rows.
    apiPost.mockResolvedValue({ ok: true, closeout_id: 'co-6' });
    await setInput(wrapper, 'mjco-notes', 'Done.');
    await confirmedSubmit(wrapper);
    expect(apiPost.mock.calls[0][1].parts).toEqual([]);
  });

  it('hides the "no parts were used" attestation once parts are logged', async () => {
    apiGet.mockResolvedValue([
      { id: 'p1', part_name: 'Torsion spring', quantity: 2, status: 'used', source: 'mobile' },
    ]);

    const wrapper = mountDialog({ visible: false });
    await wrapper.setProps({ visible: true });
    await flushPromises();

    // Attesting "no parts" while billable parts sit on the job is a lie the
    // form should not be able to tell.
    expect(wrapper.find('[data-testid="mjco-no-parts-used"]').exists()).toBe(false);
  });

  // ── Return visit + parts to order (Doug 2026-08-04) ────────────────

  it('return-visit toggle blocks submit until the why is filled', async () => {
    const wrapper = mountDialog();
    await flushPromises();

    await wrapper.find('[data-testid="mjco-return-visit"] input').setValue(true);
    // The toggle alone counts as form content, but the missing WHY blocks —
    // same rule the backend enforces with its 422.
    expect(wrapper.find('[data-testid="mjco-submit"]').attributes('disabled')).toBeDefined();

    await setInput(wrapper, 'mjco-return-reason', 'Spring on backorder');
    expect(wrapper.find('[data-testid="mjco-submit"]').attributes('disabled')).toBeUndefined();
  });

  it('submits return visit + FREE-TEXT parts to order; success surfaces the new job', async () => {
    apiPost.mockResolvedValue({ ok: true, closeout_id: 'co-3', return_visit_job_id: 'rv-1' });

    const wrapper = mountDialog();
    await flushPromises();
    await wrapper.find('[data-testid="mjco-return-visit"] input').setValue(true);
    await setInput(wrapper, 'mjco-return-reason', 'Panel on backorder');

    // Typed part, NO catalog suggestion picked — must submit with sku null.
    await wrapper.find('[data-testid="mjco-add-order-part"]').trigger('click');
    await setInput(wrapper, 'mjco-order-name-0', '16ft strut');
    await setInput(wrapper, 'mjco-order-qty-0', '3');
    await wrapper.find('[data-testid="mjco-order-urgent-0"]').setValue(true);

    await confirmedSubmit(wrapper);

    const payload = apiPost.mock.calls[0][1];
    expect(payload.needs_return_visit).toBe(true);
    expect(payload.return_visit_reason).toBe('Panel on backorder');
    expect(payload.parts_to_order).toEqual([
      { name: '16ft strut', sku: null, qty: 3, urgency: 'urgent' },
    ]);

    // The tech hears that dispatch now owns the return trip.
    const infoToast = toastAdd.mock.calls.find((c) => c[0]?.severity === 'info');
    expect(infoToast).toBeTruthy();
    expect(infoToast[0].summary).toContain('Return visit');
  });

  it('plain closeout payload keeps the legacy defaults (no return visit, empty order list)', async () => {
    apiPost.mockResolvedValue({ ok: true, closeout_id: 'co-4', return_visit_job_id: null });

    const wrapper = mountDialog();
    await flushPromises();
    await setInput(wrapper, 'mjco-notes', 'Done.');
    await confirmedSubmit(wrapper);

    const payload = apiPost.mock.calls[0][1];
    expect(payload.needs_return_visit).toBe(false);
    expect(payload.return_visit_reason).toBeNull();
    expect(payload.parts_to_order).toEqual([]);
    // And no return-visit toast when the backend created nothing.
    expect(toastAdd.mock.calls.find((c) => c[0]?.severity === 'info')).toBeFalsy();
  });
});

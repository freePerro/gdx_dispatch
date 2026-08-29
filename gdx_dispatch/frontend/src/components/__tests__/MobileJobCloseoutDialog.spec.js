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
import { ref } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const toastAdd = vi.fn();
// Photos ride usePhotoQueue (IndexedDB-first), not the closeout POST. Mocked
// so jsdom never touches Dexie, and so the contract "capturePhoto(jobId, File)
// per picked file" is pinned by argument.
const pendingPhotosRef = ref(0);
const capturePhotoMock = vi.fn();

vi.mock('../../composables/usePhotoQueue', () => ({
  usePhotoQueue: () => ({
    pendingPhotos: pendingPhotosRef,
    uploadingPhotos: ref(false),
    capturePhoto: capturePhotoMock,
    drainPhotos: vi.fn(),
  }),
}));

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
  // The real one fetches its src with a Bearer token; here it just has to exist.
  AuthedImage: { props: ['src', 'alt'], template: '<img :src="src" :alt="alt" data-testid="authed-img" />' },
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
    capturePhotoMock.mockReset();
    capturePhotoMock.mockResolvedValue({ queued: false, id: 'p-1' });
    pendingPhotosRef.value = 0;
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

  // ─── Photos (2026-08-28) ───────────────────────────────────────────
  // Before this section existed the dialog was the ONE completion surface
  // with no camera: modal over the detail screen's Photos card, so a tech
  // had to Cancel (losing parts, hours, signature) to photograph the door.
  describe('photos', () => {
    function pick(wrapper, files) {
      const input = wrapper.find('[data-testid="mjco-photo-add"] input[type="file"]');
      Object.defineProperty(input.element, 'files', { value: files, configurable: true });
      return input.trigger('change');
    }
    const file = (name = 'door.jpg') => new File(['x'], name, { type: 'image/jpeg' });

    it('offers a camera control — accept image/*, multiple, and NO capture attribute', async () => {
      const wrapper = mountDialog();
      await flushPromises();
      const input = wrapper.find('[data-testid="mjco-photo-add"] input[type="file"]');
      expect(input.exists()).toBe(true);
      expect(input.attributes('accept')).toBe('image/*');
      expect(input.attributes('multiple')).toBeDefined();
      // Android honours `capture` by forcing a single shot straight to the
      // lens — kills `multiple` and locks the tech out of the gallery.
      expect(input.attributes('capture')).toBeUndefined();
    });

    it('loads the job\'s existing photos on open so the tech does not re-shoot', async () => {
      apiGet.mockImplementation(async (url) =>
        url === '/api/jobs/job-test-1/photos'
          ? [{ id: 'ph-1', url: '/api/documents/ph-1/download', filename: 'before.jpg' }]
          : [],
      );
      // Opened the way production opens it: both parents render the dialog
      // permanently with visible=false and flip it — the load rides that flip.
      const wrapper = mountDialog({ visible: false });
      await wrapper.setProps({ visible: true });
      await flushPromises();
      expect(apiGet).toHaveBeenCalledWith('/api/jobs/job-test-1/photos', { suppressErrorToast: true });
      expect(wrapper.findAll('[data-testid="mjco-photo-strip"] [data-testid="authed-img"]')).toHaveLength(1);
      expect(wrapper.find('[data-testid="mjco-no-photos"]').exists()).toBe(false);
    });

    it('says so when the job has no photos yet — only once the server said so', async () => {
      apiGet.mockResolvedValue([]);
      const wrapper = mountDialog({ visible: false });
      // Not loaded yet: no claim either way.
      expect(wrapper.find('[data-testid="mjco-no-photos"]').exists()).toBe(false);
      await wrapper.setProps({ visible: true });
      await flushPromises();
      expect(wrapper.find('[data-testid="mjco-no-photos"]').exists()).toBe(true);
      expect(wrapper.find('[data-testid="mjco-photos-unavailable"]').exists()).toBe(false);
    });

    it('stores each picked file through the offline queue for THIS job, then refetches and tells the parent', async () => {
      const wrapper = mountDialog();
      await flushPromises();
      apiGet.mockClear();
      await pick(wrapper, [file('a.jpg'), file('b.jpg')]);
      await flushPromises();
      expect(capturePhotoMock).toHaveBeenCalledTimes(2);
      expect(capturePhotoMock).toHaveBeenNthCalledWith(1, 'job-test-1', expect.any(File));
      expect(capturePhotoMock).toHaveBeenNthCalledWith(2, 'job-test-1', expect.any(File));
      expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success', summary: 'Photos added' }));
      // The 201 carries no url — the strip renders only after a refetch.
      expect(apiGet).toHaveBeenCalledWith('/api/jobs/job-test-1/photos', { suppressErrorToast: true });
      expect(wrapper.emitted('photo-added')).toHaveLength(1);
      // Never bundled into the closeout POST.
      expect(apiPost).not.toHaveBeenCalled();
    });

    it("says 'saved on your phone' with no signal — never 'uploaded'", async () => {
      capturePhotoMock.mockResolvedValue({ queued: true, id: 'p-q' });
      const wrapper = mountDialog();
      await flushPromises();
      await pick(wrapper, [file()]);
      await flushPromises();
      expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'warn', summary: 'Saved on your phone' }));
    });

    it('a full offline backlog is refused with a reason, not a generic error', async () => {
      capturePhotoMock.mockRejectedValue(Object.assign(new Error('Too many photos still waiting to upload'), { code: 'photo_backlog_full' }));
      const wrapper = mountDialog();
      await flushPromises();
      await pick(wrapper, [file()]);
      await flushPromises();
      expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error', summary: 'Too many photos waiting' }));
      expect(wrapper.emitted('photo-added')).toBeUndefined();
    });

    it('a photo alone neither enables Close out nor makes the form dirty', async () => {
      const wrapper = mountDialog();
      await flushPromises();
      await pick(wrapper, [file()]);
      await flushPromises();
      // Photos are saved already; a closeout with nothing but a photo would be
      // a bare /complete, and cancelling must not warn about losing anything.
      expect(wrapper.find('[data-testid="mjco-submit"]').attributes('disabled')).toBeDefined();
      const confirmSpy = vi.spyOn(window, 'confirm');
      await wrapper.find('[data-testid="mjco-cancel"]').trigger('click');
      expect(confirmSpy).not.toHaveBeenCalled();
      confirmSpy.mockRestore();
    });

    it('shows how many photos are still waiting for signal', async () => {
      pendingPhotosRef.value = 3;
      const wrapper = mountDialog();
      await flushPromises();
      expect(wrapper.find('[data-testid="mjco-photo-pending"]').text()).toContain('3 waiting for signal');
    });

    // Audit 2026-08-28: DispatchView nulls closeoutJob (jobId → '') when the
    // sheet closes or the closeout submits. A multi-file loop still running
    // would have POSTed job_id='' — accepted by the server as an orphan.
    it('keeps uploading to the job the sheet was opened for, even if the parent re-points it mid-upload', async () => {
      let releaseFirst;
      capturePhotoMock
        .mockImplementationOnce(() => new Promise((res) => { releaseFirst = () => res({ queued: false, id: 'p-a' }); }))
        .mockResolvedValueOnce({ queued: false, id: 'p-b' });
      const wrapper = mountDialog();
      await flushPromises();
      await pick(wrapper, [file('a.jpg'), file('b.jpg')]);
      await flushPromises();
      expect(capturePhotoMock).toHaveBeenCalledTimes(1);
      await wrapper.setProps({ jobId: '' });
      releaseFirst();
      await flushPromises();
      expect(capturePhotoMock).toHaveBeenCalledTimes(2);
      expect(capturePhotoMock).toHaveBeenNthCalledWith(2, 'job-test-1', expect.any(File));
    });

    it('Close out is disabled while photos are still saving', async () => {
      let release;
      capturePhotoMock.mockImplementationOnce(() => new Promise((res) => { release = () => res({ queued: false, id: 'p-a' }); }));
      const wrapper = mountDialog();
      await flushPromises();
      await setInput(wrapper, 'mjco-notes', 'Done.');
      expect(wrapper.find('[data-testid="mjco-submit"]').attributes('disabled')).toBeUndefined();
      await pick(wrapper, [file()]);
      await flushPromises();
      expect(wrapper.find('[data-testid="mjco-submit"]').attributes('disabled')).toBeDefined();
      release();
      await flushPromises();
      expect(wrapper.find('[data-testid="mjco-submit"]').attributes('disabled')).toBeUndefined();
    });

    it('a photo list that failed to load is NOT reported as "no photos yet"', async () => {
      apiGet.mockImplementation(async (url) => {
        if (url === '/api/jobs/job-test-1/photos') throw new Error('offline');
        return [];
      });
      const wrapper = mountDialog({ visible: false });
      await wrapper.setProps({ visible: true });
      await flushPromises();
      expect(wrapper.find('[data-testid="mjco-no-photos"]').exists()).toBe(false);
      expect(wrapper.find('[data-testid="mjco-photos-unavailable"]').exists()).toBe(true);
      // The camera control is still there — listing is not a precondition.
      expect(wrapper.find('[data-testid="mjco-photo-add"] input[type="file"]').exists()).toBe(true);
    });

    it('a slow photo list for the previous job never paints on the next one', async () => {
      let releaseA;
      apiGet.mockImplementation((url) => {
        if (url === '/api/jobs/job-A/photos') return new Promise((res) => { releaseA = () => res([{ id: 'ph-A', url: '/x', filename: 'a.jpg' }]); });
        if (url === '/api/jobs/job-B/photos') return Promise.resolve([]);
        return Promise.resolve([]);
      });
      const wrapper = mountDialog({ visible: false, jobId: 'job-A' });
      await wrapper.setProps({ visible: true });
      await wrapper.setProps({ visible: false });
      await wrapper.setProps({ jobId: 'job-B' });
      await wrapper.setProps({ visible: true });
      await flushPromises();
      releaseA();
      await flushPromises();
      expect(wrapper.findAll('[data-testid="authed-img"]')).toHaveLength(0);
      expect(wrapper.find('[data-testid="mjco-no-photos"]').exists()).toBe(true);
    });
  });
});

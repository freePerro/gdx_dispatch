/**
 * TimeEntryDialog — the ONE writer to PATCH/POST /api/timeclock/entries,
 * extracted from TimesheetsView (2026-08-13) so the tech's own weekly
 * timesheet shares it.
 *
 * The office-mode behavior (shop wall clock round trip, canSave rules,
 * deep-link flow) stays pinned in TimesheetsView.spec.js through the parent.
 * This file pins what SELF MODE adds:
 *
 *  - no technician field, and the POST body carries no technician_id — the
 *    backend resolves "me", and never trusting the client with an id is the
 *    point;
 *  - the note is REQUIRED before Save enables, mirroring the server's 422 —
 *    a tech's correction to their own clock record needs the why on the row;
 *  - a server rejection (e.g. the self-service window) renders in the dialog
 *    instead of vanishing.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn(() => Promise.resolve({}));
const apiPost = vi.fn();
const apiPatch = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));

const tenantTz = ref('America/Chicago');
vi.mock('../../composables/useTenantTimezone', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    useTenantTimezone: () => ({
      tenantTimezone: tenantTz,
      ensureLoaded: () => Promise.resolve(tenantTz.value),
      zonedDateKey: (v) => actual.dateKeyInZone(v, tenantTz.value),
    }),
  };
});

import TimeEntryDialog from '../TimeEntryDialog.vue';

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  DatePicker: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input />' },
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  InputText: { props: ['value'], template: '<input />' },
  Message: { template: '<div class="msg"><slot /></div>' },
  Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<select />' },
  Textarea: { props: ['modelValue'], emits: ['update:modelValue'], template: '<textarea />' },
};

const ENTRY = {
  id: 'mine-1', technician_id: 'u-me',
  clock_in_at: '2026-05-11T13:00:00Z', clock_out_at: null,
  minutes: null, entry_type: 'clock', notes: null,
};

const wall = (y, m, d, hh, mm = 0) => new Date(y, m - 1, d, hh, mm, 0, 0);

async function mountDialog(props = {}) {
  const w = mount(TimeEntryDialog, {
    props: { visible: false, ...props },
    global: { stubs },
  });
  await w.setProps({ visible: true });
  await flushPromises();
  return w;
}

beforeEach(() => {
  apiPost.mockReset();
  apiPatch.mockReset();
  tenantTz.value = 'America/Chicago';
});

describe('TimeEntryDialog — self mode', () => {
  it('shows no technician field at all', async () => {
    const w = await mountDialog({ selfMode: true, entry: null });
    expect(w.find('[data-testid="entry-tech-select"]').exists()).toBe(false);
    expect(w.find('[data-testid="entry-tech-readonly"]').exists()).toBe(false);
  });

  it('keeps Save disabled until a note says why', async () => {
    const w = await mountDialog({ selfMode: true, entry: ENTRY });
    w.vm.editing.clockOut = wall(2026, 5, 11, 16);
    await flushPromises();
    expect(w.vm.canSave).toBe(false);

    w.vm.editing.notes = 'Forgot to clock out — left at 4.';
    await flushPromises();
    expect(w.vm.canSave).toBe(true);
  });

  it('wires the note through the actual Textarea, not just vm state', async () => {
    // The other tests poke vm.editing directly (audit 2026-08-13: that leaves
    // the template's v-model unproven). Drive the stub the way a user would.
    const w = await mountDialog({ selfMode: true, entry: ENTRY });
    w.vm.editing.clockOut = wall(2026, 5, 11, 16);
    await flushPromises();
    expect(w.vm.canSave).toBe(false);

    w.findComponent('[data-testid="entry-notes"]').vm.$emit('update:modelValue', 'Forgot to clock out.');
    await flushPromises();
    expect(w.vm.editing.notes).toBe('Forgot to clock out.');
    expect(w.vm.canSave).toBe(true);
  });

  it('a whitespace note does not count', async () => {
    const w = await mountDialog({ selfMode: true, entry: ENTRY });
    w.vm.editing.clockOut = wall(2026, 5, 11, 16);
    w.vm.editing.notes = '   ';
    await flushPromises();
    expect(w.vm.canSave).toBe(false);
  });

  it('POSTs a new entry WITHOUT technician_id — the backend resolves "me"', async () => {
    apiPost.mockResolvedValue({});
    const w = await mountDialog({ selfMode: true, entry: null, anchorDate: wall(2026, 5, 11, 0) });
    w.vm.editing.notes = 'Forgot to clock in — worked the Hendricks install.';
    await flushPromises();
    await w.vm.save();

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, body] = apiPost.mock.calls[0];
    expect(url).toBe('/api/timeclock/entries');
    expect('technician_id' in body).toBe(false);
    expect(body.notes).toContain('Hendricks');
  });

  it('anchors a new entry on the given day, 8am–4pm', async () => {
    const w = await mountDialog({ selfMode: true, entry: null, anchorDate: wall(2026, 5, 11, 0) });
    expect(w.vm.editing.clockIn.getDate()).toBe(11);
    expect(w.vm.editing.clockIn.getHours()).toBe(8);
    expect(w.vm.editing.clockOut.getHours()).toBe(16);
  });

  it('renders a server rejection instead of swallowing it', async () => {
    // The 14-day window and the note rule are enforced server-side too; if a
    // row slips past the page's affordance gating, the 422 must be visible.
    apiPatch.mockRejectedValue(new Error('shifts older than 14 days are corrected by the office'));
    const w = await mountDialog({ selfMode: true, entry: ENTRY });
    w.vm.editing.clockOut = wall(2026, 5, 11, 16);
    w.vm.editing.notes = 'trying anyway';
    await flushPromises();
    await w.vm.save();

    expect(w.find('[data-testid="entry-error"]').text()).toContain('corrected by the office');
    // and the dialog stayed open with the user's input intact
    expect(w.emitted('saved')).toBeUndefined();
  });

  it('emits saved and closes after a successful write', async () => {
    apiPatch.mockResolvedValue({});
    const w = await mountDialog({ selfMode: true, entry: ENTRY });
    w.vm.editing.clockOut = wall(2026, 5, 11, 16);
    w.vm.editing.notes = 'Forgot to clock out.';
    await flushPromises();
    await w.vm.save();

    expect(w.emitted('saved')).toHaveLength(1);
    const visibleEvents = w.emitted('update:visible');
    expect(visibleEvents[visibleEvents.length - 1]).toEqual([false]);
  });
});

describe('TimeEntryDialog — office mode unchanged', () => {
  it('requires a technician on create and sends the picked id', async () => {
    apiPost.mockResolvedValue({});
    const w = await mountDialog({
      entry: null,
      rosterOptions: [{ label: 'Dana Ruiz', value: 'u-tech-a' }],
    });
    // No technician picked yet → no save.
    expect(w.vm.canSave).toBe(false);

    w.vm.editing.technicianId = 'u-tech-a';
    await flushPromises();
    expect(w.vm.canSave).toBe(true);
    await w.vm.save();
    expect(apiPost.mock.calls[0][1].technician_id).toBe('u-tech-a');
  });

  it('does not demand a note from the office', async () => {
    const w = await mountDialog({ entry: { ...ENTRY, clock_out_at: '2026-05-11T21:00:00Z', minutes: 480 } });
    expect(w.vm.editing.notes).toBe('');
    expect(w.vm.canSave).toBe(true);
  });

  it('snapshots the row on open, including the open-shift flag', async () => {
    const w = await mountDialog({ entry: ENTRY });
    expect(w.vm.editing.id).toBe('mine-1');
    expect(w.vm.editing.wasOpen).toBe(true);
    expect(w.vm.editing.clockIn.getHours()).toBe(8); // 13:00Z = 08:00 Chicago
  });
});

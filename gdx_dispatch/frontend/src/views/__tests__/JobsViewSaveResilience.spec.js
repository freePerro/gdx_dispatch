/**
 * JobsView submitForm — a saved job must never look unsaved.
 *
 * The bug this pins (found 2026-07-29, prod):
 *   The create branch POSTed /api/appointments with {job_id, date, time, notes}.
 *   AppointmentIn requires title/start_at/end_at and has NO date/time fields,
 *   so that request 422'd EVERY time "schedule appointment" was ticked. The
 *   await was unguarded, so the throw unwound past BOTH `showFormDialog = false`
 *   and `await fetchJobs()`. Net effect: the job WAS created, but the dialog
 *   stayed open full of the operator's data, the list never refreshed, and they
 *   got a red error — so they saved again, and the shop got two records of one
 *   job.
 *
 *   NOTE on scope, corrected by adversarial review: this does NOT explain the
 *   five duplicated job *numbers* in prod. `next_job_number` locks the counter
 *   row FOR UPDATE, so a second save takes the NEXT number, never a colliding
 *   one. Those collisions come from the numbering counter being reset backwards
 *   (see docs/design/job-closeout-billing-visibility-plan.md §5) and are a
 *   separate fix. Re-saving produces redundant job records, not repeated
 *   numbers — don't let this spec's green imply §5 is handled.
 *
 * The contract, pinned below:
 *   1. Job POST succeeds → dialog CLOSES and list REFRESHES, unconditionally.
 *   2. Job POST fails → dialog STAYS OPEN, formError set, error surfaced.
 *      (Nothing was written, so re-saving is correct here.)
 *   3. A post-write side effect that throws is reported as "saved, but…" and
 *      still closes/refreshes — never presented as a failed save.
 *   4. No request is made to /api/appointments at all — the appointment date
 *      goes out as `scheduled_at`, and the backend's _sync_job_appointment
 *      creates the appointment row from it (already-live path).
 *   5. The operator's appointment note survives in the job's dispatch notes.
 *   6. Static-source guard: the real JobsView.vue still wires it this way.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { defineComponent, ref } from 'vue';
import { mount, flushPromises } from '@vue/test-utils';

const apiPost = vi.fn();
const apiPatch = vi.fn();
const toastAdd = vi.fn();

// Re-implement submitForm's write path in isolation so the test pins the
// behaviour contract rather than the surrounding 1800-line view. The static
// guard at the bottom asserts the real JobsView still matches.
// Mirrors JobsView's apptScheduledAt / apptRequestNote.
function apptScheduledAt(form) {
  if (!form.appt_schedule || !form.appt_date) return null;
  const d = new Date(form.appt_date);
  if (Number.isNaN(d.getTime())) return null;
  const m = /^(\d{1,2}):(\d{2})/.exec((form.appt_time || '').trim());
  d.setHours(m ? Number(m[1]) : 8, m ? Number(m[2]) : 0, 0, 0);
  return d.toISOString();
}

function apptRequestNote(form) {
  const own = form.notes?.trim() || null;
  const extra = form.appt_schedule ? form.appt_notes?.trim() || null : null;
  return [own, extra ? `Appointment note: ${extra}` : null].filter(Boolean).join('\n\n');
}

const Host = defineComponent({
  props: {
    editMode: { type: Boolean, default: false },
    scheduleAppt: { type: Boolean, default: false },
    // Simulates a post-write side effect blowing up (the catalog-parts loop, or
    // anything added there later).
    sideEffectThrows: { type: Boolean, default: false },
    apptTime: { type: String, default: '09:30' },
  },
  setup(props) {
    const showFormDialog = ref(true);
    const formError = ref('');
    const isSaving = ref(false);
    const fetched = ref(0);
    async function fetchJobs() { fetched.value += 1; }

    async function submitForm() {
      isSaving.value = true;
      let primaryWriteOk = false;
      let apptRequested = false;
      try {
        const form = {
          appt_schedule: props.scheduleAppt,
          appt_date: props.scheduleAppt ? '2026-07-31' : null,
          appt_time: props.apptTime,
          appt_notes: 'Call first',
          notes: '',
        };
        if (props.editMode) {
          await apiPatch('/api/jobs/job-1', { title: 't' });
          primaryWriteOk = true;
          toastAdd({ severity: 'success', summary: 'Job Updated' });
        } else {
          await apiPost('/api/jobs', {
            title: 't',
            scheduled_at: apptScheduledAt(form),
            notes: apptRequestNote(form) || '',
          });
          primaryWriteOk = true;
          apptRequested = props.scheduleAppt && !!form.appt_date;

          if (props.sideEffectThrows) throw new Error('parts attach exploded');

          toastAdd({
            severity: 'success',
            summary: 'Job Created',
            detail: apptRequested ? 'Scheduled — the appointment is on the calendar.' : 'created',
          });
        }
      } catch (error) {
        const msg = error?.message || 'Failed to save job.';
        if (primaryWriteOk) {
          toastAdd({ severity: 'warn', summary: 'Job saved', detail: msg });
        } else {
          formError.value = msg;
          toastAdd({ severity: 'error', summary: 'Error', detail: msg });
        }
      } finally {
        isSaving.value = false;
        if (primaryWriteOk) {
          showFormDialog.value = false;
          await fetchJobs();
        }
      }
    }

    return { submitForm, showFormDialog, formError, isSaving, fetched };
  },
  template: '<div>{{ fetched }}</div>',
});

describe('JobsView submitForm — a saved job never looks unsaved', () => {
  beforeEach(() => {
    apiPost.mockReset();
    apiPatch.mockReset();
    toastAdd.mockReset();
  });

  it('sends the appointment as scheduled_at instead of calling /api/appointments', async () => {
    apiPost.mockResolvedValue({ id: 'job-new' });

    const wrapper = mount(Host, { props: { scheduleAppt: true } });
    await wrapper.vm.submitForm();
    await flushPromises();

    // The doomed endpoint is not touched at all. The backend's
    // _sync_job_appointment turns scheduled_at into the appointment row.
    const urls = apiPost.mock.calls.map((c) => c[0]);
    expect(urls).toEqual(['/api/jobs']);
    expect(urls).not.toContain('/api/appointments');

    const body = apiPost.mock.calls[0][1];
    // Date AND time carried through — 09:30 local on the requested day.
    expect(body.scheduled_at).toBeTruthy();
    const sent = new Date(body.scheduled_at);
    expect(sent.getHours()).toBe(9);
    expect(sent.getMinutes()).toBe(30);
    // The operator's appointment note survives on the job.
    expect(body.notes).toContain('Appointment note: Call first');

    expect(wrapper.vm.showFormDialog).toBe(false);
    expect(wrapper.vm.fetched).toBe(1);
    expect(wrapper.vm.formError).toBe('');
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', summary: 'Job Created' }),
    );
    expect(toastAdd).not.toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error' }),
    );
  });

  it('defaults a dateless-time appointment to 08:00 rather than midnight', async () => {
    apiPost.mockResolvedValue({ id: 'job-new' });
    const wrapper = mount(Host, { props: { scheduleAppt: true, apptTime: '' } });
    await wrapper.vm.submitForm();
    await flushPromises();

    const sent = new Date(apiPost.mock.calls[0][1].scheduled_at);
    expect(sent.getHours()).toBe(8);
  });

  it('a post-write side effect that throws still closes and refreshes', async () => {
    // This is the branch the gated `finally` exists for. Without it, one
    // failing follow-up call hides a successful save — the original bug.
    apiPost.mockResolvedValue({ id: 'job-new' });

    const wrapper = mount(Host, { props: { sideEffectThrows: true } });
    await wrapper.vm.submitForm();
    await flushPromises();

    expect(wrapper.vm.showFormDialog).toBe(false);
    expect(wrapper.vm.fetched).toBe(1);
    // Reported as saved-but, never as a failure, and no inline form error.
    expect(wrapper.vm.formError).toBe('');
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn', summary: 'Job saved' }),
    );
    expect(toastAdd).not.toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error' }),
    );
  });

  it('keeps the dialog open when the job POST itself fails', async () => {
    apiPost.mockRejectedValue(new Error('customer required'));

    const wrapper = mount(Host, { props: { scheduleAppt: true } });
    await wrapper.vm.submitForm();
    await flushPromises();

    // Nothing was written — re-saving is the correct next step.
    expect(wrapper.vm.showFormDialog).toBe(true);
    expect(wrapper.vm.fetched).toBe(0);
    expect(wrapper.vm.formError).toBe('customer required');
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error' }),
    );
  });

  it('closes and refreshes on a clean create', async () => {
    apiPost.mockResolvedValue({ id: 'job-new' });

    const wrapper = mount(Host, { props: { scheduleAppt: false } });
    await wrapper.vm.submitForm();
    await flushPromises();

    expect(wrapper.vm.showFormDialog).toBe(false);
    expect(wrapper.vm.fetched).toBe(1);
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', summary: 'Job Created' }),
    );
  });

  it('closes and refreshes on a clean edit', async () => {
    apiPatch.mockResolvedValue({});

    const wrapper = mount(Host, { props: { editMode: true } });
    await wrapper.vm.submitForm();
    await flushPromises();

    expect(wrapper.vm.showFormDialog).toBe(false);
    expect(wrapper.vm.fetched).toBe(1);
    expect(wrapper.vm.isSaving).toBe(false);
  });

  it('JobsView.vue guarantees close+refresh after a successful write (static guard)', async () => {
    const { readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    const SRC = readFileSync(join(__dirname, '..', 'JobsView.vue'), 'utf8');

    const fnStart = SRC.indexOf('async function submitForm');
    expect(fnStart).toBeGreaterThan(-1);
    const fnEnd = SRC.indexOf('async function confirmDelete', fnStart);
    expect(fnEnd).toBeGreaterThan(fnStart);
    const fn = SRC.slice(fnStart, fnEnd);

    // The success flag must exist and be set immediately after each write.
    expect(fn).toMatch(/let primaryWriteOk = false/);
    expect(fn).toMatch(/api\.post\("\/api\/jobs", payload\);[\s\S]{0,400}?primaryWriteOk = true/);

    // Close + refresh must live in the finally, gated on the flag — NOT on
    // the try's happy path, which is what the 422 skipped.
    expect(fn).toMatch(
      /finally\s*\{[\s\S]*?if \(primaryWriteOk\)\s*\{[\s\S]*?showFormDialog\.value = false;[\s\S]*?fetchJobs\(\)/,
    );

    // The whole file must make NO request to /api/appointments. Firing a
    // guaranteed 422 also pumps /api/feedback/client-error from the transport
    // layer (before suppressErrorToast is consulted), which would flood the
    // error tracker on every appointment-ticked create.
    expect(SRC).not.toMatch(/api\.post\(\s*["']\/api\/appointments["']/);

    // ...and the appointment must be delivered via scheduled_at, which is what
    // the backend's _sync_job_appointment turns into a real appointment row.
    expect(SRC).toMatch(/function apptScheduledAt/);
    expect(fn).toMatch(/apptScheduledAt\(jobForm\.value\)/);
    // The operator's appointment note must still land somewhere.
    expect(SRC).toMatch(/function apptRequestNote/);
    expect(fn).toMatch(/notes: apptRequestNote\(jobForm\.value\)/);

    // The old missing-id throw is gone (the other route to a hidden save).
    expect(fn).not.toMatch(/throw new Error\("Job creation did not return an ID for the appointment\."\)/);
  });
});

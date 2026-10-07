/**
 * "Is this job finished?" — multi-day jobs PR 3, plan §5.4a "The sheet" and
 * the Sheet items of its Tests list. The Yes form's own pins live in
 * MobileJobCloseoutDialog.spec.js; this file covers the question, the No half
 * (MobileDayCloseSection) and the Yes-side changes the day log drives.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const toastAdd = vi.fn();
const authState = { user: { role: 'technician' } };

vi.mock('../../composables/usePhotoQueue', () => ({
  usePhotoQueue: () => ({
    pendingPhotos: ref(0),
    failedPhotos: ref(0),
    failedRows: ref([]),
    uploadingPhotos: ref(false),
    capturePhoto: vi.fn(),
    drainPhotos: vi.fn(),
    retryFailedPhotos: vi.fn(),
    discardFailedPhotos: vi.fn(),
    describePhotoRefusal: () => 'refused',
  }),
}));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, postQueued: apiPost }),
}));
vi.mock('../../stores/auth', () => ({ useAuthStore: () => authState }));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));

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
  AuthedImage: { props: ['src', 'alt'], template: '<img :src="src" :alt="alt" />' },
};

const ME = 'u-me';
const PAL = 'u-pal';

function dayLog(over = {}) {
  return {
    rows: [],
    logged_hours_total: 0,
    today_has_day_row: false,
    earlier_day_open: null,
    open_day: {
      date: '2026-10-06',
      visits: [
        { id: 'v1', tech_name: 'Ann', start_at: '2026-10-06T13:00:00Z', state: 'on_site' },
        { id: 'v2', tech_name: 'Bob', start_at: '2026-10-06T14:00:00Z', state: 'on_site' },
      ],
      people: [
        { user_id: PAL, name: 'Bob', mine: false, logged: [] },
        { user_id: ME, name: 'Ann', mine: true, logged: [] },
      ],
    },
    ...over,
  };
}

function routeGets(log) {
  apiGet.mockImplementation(async (url) => {
    if (url.endsWith('/day-log')) {
      if (log instanceof Error) throw log;
      return log;
    }
    return [];
  });
}

async function openSheet(log = dayLog()) {
  routeGets(log);
  const w = mount(MobileJobCloseoutDialog, {
    props: { visible: false, jobId: 'job-1', jobTitle: 'Install' },
    global: { stubs },
  });
  await w.setProps({ visible: true });
  await flushPromises();
  return w;
}

const q = (w, id) => w.find(`[data-testid="${id}"]`);

async function type(w, id, value) {
  const el = q(w, id);
  el.element.value = value;
  await el.trigger('input');
}

// The Yes submit is two taps (review strip + dwell); see the Dialog spec.
async function confirmedSubmit(w) {
  await q(w, 'mjco-submit').trigger('click');
  await flushPromises();
  const realNow = Date.now;
  vi.spyOn(Date, 'now').mockImplementation(() => realNow() + 2000);
  await q(w, 'mjco-submit').trigger('click');
  await flushPromises();
  vi.mocked(Date.now).mockRestore();
}

describe('Is this job finished? (PR 3 sheet)', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    toastAdd.mockReset();
    authState.user = { role: 'technician' };
    localStorage.clear();
  });

  it('asks the question first and shows neither half until answered', async () => {
    const w = await openSheet();
    expect(q(w, 'mjco-finished-question').exists()).toBe(true);
    expect(q(w, 'mjco-day-close').exists()).toBe(false);
    expect(q(w, 'mjco-hours').exists()).toBe(false);
    expect(apiGet).toHaveBeenCalledWith('/api/jobs/job-1/day-log', { suppressErrorToast: true });
  });

  it('Yes shows the closeout form with the relabelled follow-up toggle; No does not', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-yes').trigger('click');
    expect(q(w, 'mjco-hours').exists()).toBe(true);
    expect(w.text()).toContain('Needs a follow-up job (new work)');
    expect(q(w, 'mjco-return-visit-hint').text()).toContain('Answer No above instead');
    expect(q(w, 'mjco-submit').exists()).toBe(true);

    await q(w, 'mjco-finished-no').trigger('click');
    expect(q(w, 'mjco-hours').exists()).toBe(false);
    expect(w.text()).not.toContain('Needs a follow-up job (new work)');
    expect(q(w, 'mjco-day-close').exists()).toBe(true);
    expect(q(w, 'mjco-day-submit').exists()).toBe(true);
  });

  it('No lists visits as checkboxes with no hours field, and one hours row per person, You first', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-no').trigger('click');
    const visits = q(w, 'mjco-day-visits');
    expect(visits.findAll('input[type="checkbox"]')).toHaveLength(2);
    expect(visits.findAll('input[type="number"]')).toHaveLength(0);

    const people = q(w, 'mjco-day-people').findAll('[data-testid^="mjco-day-person-u-"]');
    expect(people).toHaveLength(2);
    expect(people[0].attributes('data-testid')).toBe(`mjco-day-person-${ME}`);
    expect(people[0].text()).toContain('You');
  });

  it('unchecking a visit warns the job stays on the board', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-no').trigger('click');
    expect(q(w, 'mjco-day-visit-warning').exists()).toBe(false);
    await q(w, 'mjco-day-visit-v2').find('input').setValue(false);
    expect(q(w, 'mjco-day-visit-warning').text()).toContain('stays on the board');
  });

  it('other rows copy the You value until edited; the submit carries one entry per person', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-no').trigger('click');
    await type(w, `mjco-day-hours-${ME}`, '8');
    expect(q(w, `mjco-day-hours-${PAL}`).element.value).toBe('8');
    await type(w, `mjco-day-hours-${PAL}`, '6');
    await type(w, `mjco-day-hours-${ME}`, '7.5');
    expect(q(w, `mjco-day-hours-${PAL}`).element.value).toBe('6');

    apiPost.mockResolvedValue({ ok: true, next_visit: null });
    await q(w, 'mjco-day-submit').trigger('click');
    await flushPromises();

    const [url, body, opts] = apiPost.mock.calls[0];
    expect(url).toBe('/api/jobs/job-1/day-close');
    expect(body.day).toBe('2026-10-06');
    expect(body.visits).toEqual(['v1', 'v2']);
    expect(body.people).toEqual([
      { user_id: ME, hours: 7.5 },
      { user_id: PAL, hours: 6 },
    ]);
    expect(body.added).toEqual([]);
    expect(typeof body.closed_at).toBe('string');
    expect(opts).toMatchObject({ actionType: 'job.day_close', resourceId: 'job-1', conflictIsError: true });
    expect(w.emitted('day-closed')).toBeTruthy();
    expect(w.emitted('closed-out')).toBeFalsy();
  });

  it('a desk sheet with no You row starts every row blank and shows no no-timer warning', async () => {
    authState.user = { role: 'office' };
    const log = dayLog();
    log.open_day.people = [
      { user_id: 'a', name: 'Ann', mine: false, logged: [] },
      { user_id: 'b', name: 'Bob', mine: false, logged: [] },
    ];
    const w = await openSheet(log);
    await q(w, 'mjco-finished-no').trigger('click');
    expect(q(w, 'mjco-day-hours-a').element.value).toBe('');
    expect(q(w, 'mjco-day-hours-b').element.value).toBe('');
    expect(q(w, 'mjco-day-no-timer').exists()).toBe(false);
    expect(q(w, 'mjco-day-submit').attributes('disabled')).toBeDefined();
  });

  it('a technician with no row of their own is told so', async () => {
    const log = dayLog();
    log.open_day.people = [{ user_id: PAL, name: 'Bob', mine: false, logged: [] }];
    const w = await openSheet(log);
    await q(w, 'mjco-finished-no').trigger('click');
    expect(q(w, 'mjco-day-no-timer').exists()).toBe(true);
  });

  it('a person with hours already logged that day starts blank and says what was logged', async () => {
    const log = dayLog();
    log.open_day.people[0].logged = [{ hours: 3, closed_by: 'Ann' }];
    const w = await openSheet(log);
    await q(w, 'mjco-finished-no').trigger('click');
    await type(w, `mjco-day-hours-${ME}`, '8');
    expect(q(w, `mjco-day-hours-${PAL}`).element.value).toBe('');
    expect(q(w, `mjco-day-logged-${PAL}`).text()).toContain('3 h (by Ann)');
  });

  it('+ Add a helper adds a row and the body carries `added`', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-no').trigger('click');
    await type(w, `mjco-day-hours-${ME}`, '8');
    await q(w, 'mjco-day-add-helper').trigger('click');
    expect(q(w, 'mjco-day-added-0').exists()).toBe(true);
    // A blank helper row blocks the submit.
    expect(q(w, 'mjco-day-submit').attributes('disabled')).toBeDefined();
    await q(w, 'mjco-day-added-hours-0').setValue('4');
    apiPost.mockResolvedValue({ ok: true });
    await q(w, 'mjco-day-submit').trigger('click');
    await flushPromises();
    expect(apiPost.mock.calls[0][1].added).toEqual([{ hours: 4 }]);
  });

  it('unchecking a person drops them from the body and warns their time stays open', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-no').trigger('click');
    await type(w, `mjco-day-hours-${ME}`, '8');
    await q(w, `mjco-day-person-check-${PAL}`).setValue(false);
    expect(q(w, `mjco-day-person-warning-${PAL}`).text()).toContain("Bob's time stays open");
    apiPost.mockResolvedValue({ ok: true });
    await q(w, 'mjco-day-submit').trigger('click');
    await flushPromises();
    expect(apiPost.mock.calls[0][1].people).toEqual([{ user_id: ME, hours: 8 }]);
  });

  it('nothing booked today: says so and offers no submit that could work', async () => {
    const w = await openSheet(dayLog({ open_day: { date: '2026-10-06', visits: [], people: [] } }));
    await q(w, 'mjco-finished-no').trigger('click');
    expect(q(w, 'mjco-day-nothing').text()).toContain('office books the next day');
    expect(q(w, 'mjco-day-submit').attributes('disabled')).toBeDefined();
  });

  it('a 409 already_closed stays on the sheet with who closed it and the hours', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-no').trigger('click');
    await type(w, `mjco-day-hours-${ME}`, '8');
    const err = Object.assign(new Error('closed'), {
      status: 409,
      body: { detail: 'Already closed', code: 'already_closed', already_closed: { rows: [{ hours: 7, closed_by: 'Bob' }] } },
    });
    apiPost.mockRejectedValue(err);
    await q(w, 'mjco-day-submit').trigger('click');
    await flushPromises();
    const text = q(w, 'mjco-day-refusal').text();
    expect(text).toContain('Already closed by Bob with 7 h');
    expect(text).toContain('your 8 h');
    expect(w.emitted('day-closed')).toBeFalsy();
  });

  it('reads a refusal nested under detail too (FastAPI HTTPException shape)', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-no').trigger('click');
    await type(w, `mjco-day-hours-${ME}`, '8');
    apiPost.mockRejectedValue(Object.assign(new Error('x'), {
      status: 409,
      body: { detail: { code: 'job_finished', detail: 'finished' } },
    }));
    await q(w, 'mjco-day-submit').trigger('click');
    await flushPromises();
    expect(q(w, 'mjco-day-refusal').text()).toContain('the job was already finished');
  });

  it('offline: the queued day is announced, not claimed as closed', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-no').trigger('click');
    await type(w, `mjco-day-hours-${ME}`, '8');
    apiPost.mockResolvedValue({ queued: true });
    await q(w, 'mjco-day-submit').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Saved offline' }));
  });

  it('offline open reads the rows from the last cached day-log', async () => {
    await openSheet(); // caches
    const w = await openSheet(new Error('offline'));
    await q(w, 'mjco-finished-no').trigger('click');
    expect(q(w, `mjco-day-hours-${ME}`).exists()).toBe(true);
  });

  it('labels hours "Hours worked today" and warns about logged techs when day rows exist', async () => {
    const w = await openSheet(dayLog({ rows: [{ id: 'r1' }], logged_hours_total: 16, today_has_day_row: true }));
    await q(w, 'mjco-finished-yes').trigger('click');
    expect(q(w, 'mjco-hours-label').text()).toBe('Hours worked today');
    expect(q(w, 'mjco-already-logged').text()).toContain('16 h');
    expect(q(w, 'mjco-techs-hint-logged').exists()).toBe(true);
  });

  it('keeps "Hours worked" with no day rows', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-yes').trigger('click');
    expect(q(w, 'mjco-hours-label').text()).toBe('Hours worked');
    expect(q(w, 'mjco-already-logged').exists()).toBe(false);
  });

  it('Yes is disabled while an earlier worked day is open, and says which', async () => {
    const w = await openSheet(dayLog({ earlier_day_open: '2026-10-05' }));
    expect(q(w, 'mjco-finished-yes').attributes('disabled')).toBeDefined();
    expect(q(w, 'mjco-yes-blocked').text()).toContain('Close Monday, Oct 5 first');
    await q(w, 'mjco-finished-yes').trigger('click');
    expect(q(w, 'mjco-hours').exists()).toBe(false);
  });

  it('the Yes closeout sends tapped_at', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-yes').trigger('click');
    await type(w, 'mjco-hours', '2');
    apiPost.mockResolvedValue({ ok: true });
    await confirmedSubmit(w);
    const [url, body] = apiPost.mock.calls[0];
    expect(url).toBe('/api/jobs/job-1/closeout');
    expect(typeof body.tapped_at).toBe('string');
    expect(Number.isNaN(Date.parse(body.tapped_at))).toBe(false);
  });

  it('a Yes refused with earlier_day_open re-reads the day log', async () => {
    const w = await openSheet();
    await q(w, 'mjco-finished-yes').trigger('click');
    await type(w, 'mjco-hours', '2');
    apiPost.mockRejectedValue(Object.assign(new Error('x'), {
      status: 409, body: { detail: 'open', code: 'earlier_day_open', date: '2026-10-05' },
    }));
    const before = apiGet.mock.calls.filter(([u]) => u.endsWith('/day-log')).length;
    await confirmedSubmit(w);
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Cannot finish yet' }));
    expect(apiGet.mock.calls.filter(([u]) => u.endsWith('/day-log')).length).toBe(before + 1);
  });
});

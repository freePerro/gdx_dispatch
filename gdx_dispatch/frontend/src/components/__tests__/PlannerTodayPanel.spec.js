/**
 * PlannerTodayPanel — the Planner's Today tab (the 2026-09-28 planner Today plan).
 *
 * The note is the part that can lose words: it autosaves with the version it
 * last saw, and a 409 means the AI or another tab saved in between. These
 * tests pin that the base version never moves under unsaved typing, and that
 * a conflict shows both texts and resolves only on the user's choice.
 *
 * COUNTERFACTUAL: drop the `!dirty.value` guard in load() and
 * "a refresh while typing keeps the draft" goes red; send `base_updated_at`
 * from the refreshed copy and "Keep mine re-saves on the version it was shown"
 * goes red.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlannerTodayPanel from '../PlannerTodayPanel.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const get = vi.fn();
const post = vi.fn();
const put = vi.fn();
const patch = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get, post, put, patch }),
}));

const stubs = {
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue', 'blur'],
    template: `<textarea data-testid="today-note" :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)" @blur="$emit('blur')" />`,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input data-testid="today-add-input" :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)" />`,
  },
  Checkbox: {
    props: ['modelValue'],
    emits: ['update:modelValue', 'change'],
    template: `<input type="checkbox" class="cb" :checked="modelValue"
      @change="$emit('update:modelValue', $event.target.checked); $emit('change')" />`,
  },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Button: {
    props: ['label', 'type'],
    emits: ['click'],
    template: `<button :type="type || 'button'" class="pbtn" @click="$emit('click', $event)">{{ label }}</button>`,
  },
};

const NOTE_V1 = { date: '2026-09-28', body: 'Call the supplier', updated_at: '2026-09-28T14:00:00+00:00', updated_via: 'user' };

function view(over = {}) {
  return {
    date: '2026-09-28',
    tasks: [{ id: 't1', title: 'Quote Smith', status: 'todo', priority: 'low', source: null }],
    carried: [{ id: 't2', title: 'Order rollers', status: 'todo', priority: 'low', today_date: '2026-09-25' }],
    due: [{ id: 't3', title: 'Invoice Jones', status: 'todo', priority: 'high', due_date: '2026-09-20', source: 'ai' }],
    due_more: 0,
    note: { ...NOTE_V1 },
    ...over,
  };
}

// Every panel listens on window/document; unmount them all after each test
// or an earlier test's panel answers this test's events.
const mounted = [];
const mountPanel = () => {
  const w = mount(PlannerTodayPanel, { global: { stubs } });
  mounted.push(w);
  return w;
};
const noteBox = (w) => w.find('[data-testid="today-note"]');

beforeEach(() => {
  vi.useFakeTimers();
  get.mockReset().mockResolvedValue(view());
  post.mockReset().mockResolvedValue({ id: 'new' });
  put.mockReset().mockResolvedValue({});
  patch.mockReset().mockResolvedValue({});
});
afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount());
  vi.useRealTimers();
});

describe('PlannerTodayPanel — tasks', () => {
  it('lists pinned tasks and both suggestion groups, with the AI tag', async () => {
    const w = mountPanel();
    await flushPromises();
    expect(get).toHaveBeenCalledWith('/api/planner/today');
    expect(w.findAll('[data-testid="today-task"]').map((r) => r.text())).toEqual([expect.stringContaining('Quote Smith')]);
    expect(w.find('[data-testid="suggest-carried"]').text()).toContain('Order rollers');
    const dueRow = w.find('[data-testid="suggest-due"]');
    expect(dueRow.text()).toContain('Invoice Jones');
    expect(dueRow.text()).toContain('Overdue');
    expect(dueRow.find('.tag').text()).toBe('AI');
  });

  it('adds a suggestion to Today and removes a pinned task', async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-testid="suggest-carried"] .pbtn').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenCalledWith('/api/planner/tasks/t2/today', { on: true });
    await w.find('[data-testid="today-task"] .pbtn').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenCalledWith('/api/planner/tasks/t1/today', { on: false });
    expect(w.emitted('changed')).toHaveLength(2);
  });

  it('quick add creates the task already pinned', async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-testid="today-add-input"]').setValue('  Call the city about the permit ');
    await w.find('form').trigger('submit');
    await flushPromises();
    expect(post).toHaveBeenCalledWith('/api/planner/tasks', { title: 'Call the city about the permit', today: true });
  });

  it('checking a task marks it done through the existing task PATCH', async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-testid="today-task"] .cb').setValue(true);
    await flushPromises();
    expect(patch).toHaveBeenCalledWith('/api/planner/tasks/t1', { status: 'done' });
  });
});

describe('PlannerTodayPanel — the note', () => {
  it('saves on blur with the day and the version it was loaded at', async () => {
    put.mockResolvedValue({ ...NOTE_V1, body: 'Call the supplier\nand the city', updated_at: '2026-09-28T14:05:00+00:00' });
    const w = mountPanel();
    await flushPromises();
    expect(noteBox(w).element.value).toBe('Call the supplier');
    await noteBox(w).setValue('Call the supplier\nand the city');
    await noteBox(w).trigger('blur');
    await flushPromises();
    expect(put).toHaveBeenCalledWith(
      '/api/planner/today/note',
      { body: 'Call the supplier\nand the city', note_date: '2026-09-28', base_updated_at: NOTE_V1.updated_at },
      { suppressErrorToast: true },
    );
    expect(w.find('[data-testid="today-note-status"]').text()).toBe('Saved');
  });

  it('autosaves after a pause in typing', async () => {
    put.mockResolvedValue({ ...NOTE_V1, body: 'x' });
    const w = mountPanel();
    await flushPromises();
    await noteBox(w).setValue('x');
    expect(put).not.toHaveBeenCalled();
    vi.advanceTimersByTime(2000);
    await flushPromises();
    expect(put).toHaveBeenCalledTimes(1);
  });

  it('a refresh while typing keeps the draft and the base version', async () => {
    const w = mountPanel();
    await flushPromises();
    await noteBox(w).setValue('half a thought');
    get.mockResolvedValue(view({ note: { ...NOTE_V1, body: 'AI wrote this', updated_at: '2026-09-28T14:09:00+00:00', updated_via: 'ai' } }));
    window.dispatchEvent(new CustomEvent('gdx:planner-refresh'));
    await flushPromises();
    expect(noteBox(w).element.value).toBe('half a thought');
    await noteBox(w).trigger('blur');
    await flushPromises();
    expect(put.mock.calls[0][1].base_updated_at).toBe(NOTE_V1.updated_at);
  });

  it('a 409 shows both texts; Keep mine re-saves on the version it was shown', async () => {
    const latest = { ...NOTE_V1, body: 'Call the supplier\n- AI: order 2 openers', updated_at: '2026-09-28T14:09:00+00:00', updated_via: 'ai' };
    put.mockRejectedValueOnce(Object.assign(new Error('conflict'), {
      status: 409, body: { detail: { reason: 'stale', current: latest } },
    }));
    const w = mountPanel();
    await flushPromises();
    await noteBox(w).setValue('Call the supplier today');
    await noteBox(w).trigger('blur');
    await flushPromises();

    const box = w.find('[data-testid="today-note-conflict"]');
    expect(box.exists()).toBe(true);
    expect(box.text()).toContain('- AI: order 2 openers');
    expect(box.text()).toContain('Call the supplier today');
    expect(box.text()).toContain('by AI');
    // no autosave fires while the choice is pending
    vi.advanceTimersByTime(5000);
    await flushPromises();
    expect(put).toHaveBeenCalledTimes(1);

    put.mockResolvedValueOnce({ ...latest, body: 'Call the supplier today', updated_via: 'user' });
    await w.find('[data-testid="today-note-keep-mine"]').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenCalledTimes(2);
    expect(put.mock.calls[1][1]).toEqual({ body: 'Call the supplier today', note_date: '2026-09-28', base_updated_at: latest.updated_at });
    expect(w.find('[data-testid="today-note-conflict"]').exists()).toBe(false);
  });

  it('Use latest takes the other text and saves nothing', async () => {
    const latest = { ...NOTE_V1, body: 'theirs', updated_at: '2026-09-28T14:09:00+00:00' };
    put.mockRejectedValueOnce(Object.assign(new Error('conflict'), {
      status: 409, body: { detail: { reason: 'stale', current: latest } },
    }));
    const w = mountPanel();
    await flushPromises();
    await noteBox(w).setValue('mine');
    await noteBox(w).trigger('blur');
    await flushPromises();
    await w.find('[data-testid="today-note-use-latest"]').trigger('click');
    await flushPromises();
    vi.advanceTimersByTime(5000);
    await flushPromises();
    expect(noteBox(w).element.value).toBe('theirs');
    expect(put).toHaveBeenCalledTimes(1);
  });

  it('a failed save says so and offers a retry', async () => {
    put.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }));
    const w = mountPanel();
    await flushPromises();
    await noteBox(w).setValue('keep me');
    await noteBox(w).trigger('blur');
    await flushPromises();
    expect(w.find('[data-testid="today-note-retry"]').exists()).toBe(true);
  });

  it('says when the AI last edited the note', async () => {
    get.mockResolvedValue(view({ note: { ...NOTE_V1, updated_via: 'ai' } }));
    const w = mountPanel();
    await flushPromises();
    expect(w.find('[data-testid="today-note-status"]').text()).toMatch(/^Edited by AI at /);
  });
});

describe('PlannerTodayPanel — leaving the page', () => {
  it('flushes a pending note when the page is hidden, without waiting for the autosave', async () => {
    put.mockResolvedValue({ ...NOTE_V1, body: 'typed then switched apps' });
    const w = mountPanel();
    await flushPromises();
    await noteBox(w).setValue('typed then switched apps');
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(put).toHaveBeenCalledTimes(1);
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });
});

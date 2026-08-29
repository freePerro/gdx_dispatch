// useFormDraft — the Planner's create dialogs lost typed text on an accidental
// back/X. These tests pin the two rules the adversarial review forced, because
// both failure modes are silent: a resurrected foreign key files work against
// the wrong customer, and a JSON-serialized Date ships an instant where the API
// expects a calendar day (the off-by-one shape fixed twice in this repo).
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useFormDraft } from '../useFormDraft';

const FIELDS = ['title', 'description', 'priority', 'due_date'];
const OPTS = { fields: FIELDS, dates: ['due_date'], defaults: { priority: 'low' } };

function draft(name = 'test_form', extra = {}) {
  return useFormDraft(name, { ...OPTS, ...extra });
}

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.useRealTimers());

describe('useFormDraft — rule 1: text is persisted, references never are', () => {
  it('stores allow-listed text fields', () => {
    const d = draft();
    d.flush({ title: 'Call back Thu', description: 'wants 2 openers' });
    expect(JSON.parse(sessionStorage.getItem(d.storageKey))).toEqual({
      title: 'Call back Thu',
      description: 'wants 2 openers',
    });
  });

  it('NEVER stores a foreign key, even when the form carries one', () => {
    const d = draft();
    d.flush({
      title: 'Call back Thu',
      customer_id: 'cus_11111111',
      job_id: 'job_22222222',
      assigned_to: 'usr_33333333',
    });
    const stored = sessionStorage.getItem(d.storageKey);
    expect(JSON.parse(stored)).toEqual({ title: 'Call back Thu' });
    // Belt and braces: the id must not appear anywhere in the payload.
    expect(stored).not.toContain('cus_11111111');
    expect(stored).not.toContain('job_22222222');
    expect(stored).not.toContain('usr_33333333');
  });

  it('does not restore a reference even if one was hand-planted in storage', () => {
    const d = draft();
    sessionStorage.setItem(
      d.storageKey,
      JSON.stringify({ title: 'x', customer_id: 'cus_11111111' }),
    );
    const form = { title: '', customer_id: null };
    d.applyTo(form);
    expect(form.title).toBe('x');
    expect(form.customer_id).toBeNull();
  });
});

describe('useFormDraft — rule 2: dates round-trip as calendar days', () => {
  it('stores a Date as YYYY-MM-DD, not as an ISO instant', () => {
    const d = draft();
    d.flush({ title: 't', due_date: new Date(2026, 7, 29) });
    const stored = JSON.parse(sessionStorage.getItem(d.storageKey));
    expect(stored.due_date).toBe('2026-08-29');
    // The counterfactual: JSON.stringify(new Date()) would put a 'T' and a 'Z'
    // in here. If this assertion ever passes a value containing them, the
    // instant-vs-calendar-day bug is back.
    expect(stored.due_date).not.toMatch(/[TZ]/);
  });

  it('revives the draft as a real Date on the same calendar day', () => {
    const d = draft();
    const original = new Date(2026, 7, 29);
    d.flush({ title: 't', due_date: original });
    const form = { title: '', due_date: null };
    d.applyTo(form);
    expect(form.due_date).toBeInstanceOf(Date);
    expect(form.due_date.getFullYear()).toBe(2026);
    expect(form.due_date.getMonth()).toBe(7);
    expect(form.due_date.getDate()).toBe(29);
  });

  it('normalizes an ISO instant left by an older build back to a calendar day', () => {
    const d = draft();
    d.flush({ title: 't', due_date: '2026-08-29T05:00:00.000Z' });
    expect(JSON.parse(sessionStorage.getItem(d.storageKey)).due_date).not.toMatch(/[TZ]/);
  });
});

describe('useFormDraft — an untouched form writes nothing', () => {
  it('treats a field equal to its default as empty', () => {
    const d = draft();
    d.flush({ title: '', description: '', priority: 'low', due_date: null });
    expect(sessionStorage.getItem(d.storageKey)).toBeNull();
  });

  it('stores priority once it differs from the default', () => {
    const d = draft();
    d.flush({ priority: 'urgent' });
    expect(JSON.parse(sessionStorage.getItem(d.storageKey))).toEqual({ priority: 'urgent' });
  });

  it('applyTo reports nothing restored when no draft exists', () => {
    const d = draft();
    const form = { title: '' };
    expect(d.applyTo(form)).toBe(false);
    expect(d.restored.value).toBe(false);
  });

  it('clearing an emptied form removes a previously stored draft', () => {
    const d = draft();
    d.flush({ title: 'typed then deleted' });
    expect(sessionStorage.getItem(d.storageKey)).not.toBeNull();
    d.flush({ title: '' });
    expect(sessionStorage.getItem(d.storageKey)).toBeNull();
  });
});

describe('useFormDraft — lifecycle', () => {
  it('save() debounces, flush() writes immediately', () => {
    vi.useFakeTimers();
    const d = draft('debounce_form', { debounceMs: 400 });
    d.save({ title: 'pending' });
    expect(sessionStorage.getItem(d.storageKey)).toBeNull();
    vi.advanceTimersByTime(400);
    expect(JSON.parse(sessionStorage.getItem(d.storageKey)).title).toBe('pending');
  });

  it('flush() cancels a pending debounced write rather than racing it', () => {
    vi.useFakeTimers();
    const d = draft('race_form', { debounceMs: 400 });
    d.save({ title: 'stale' });
    d.flush({ title: 'final' });
    vi.advanceTimersByTime(1000);
    expect(JSON.parse(sessionStorage.getItem(d.storageKey)).title).toBe('final');
  });

  it('clear() drops the draft and resets the restored flag', () => {
    const d = draft();
    d.flush({ title: 'gone soon' });
    const form = { title: '' };
    d.applyTo(form);
    expect(d.restored.value).toBe(true);
    d.clear();
    expect(sessionStorage.getItem(d.storageKey)).toBeNull();
    expect(d.restored.value).toBe(false);
  });

  it('survives storage that throws (private mode / quota)', () => {
    const d = draft();
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('QuotaExceededError');
    });
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('SecurityError');
    });
    expect(() => d.flush({ title: 'x' })).not.toThrow();
    const form = { title: '' };
    expect(() => d.applyTo(form)).not.toThrow();
    expect(form.title).toBe('');
    setItem.mockRestore();
    getItem.mockRestore();
  });

  it('ignores corrupt or non-object stored JSON', () => {
    const d = draft();
    sessionStorage.setItem(d.storageKey, '{not json');
    expect(d.read()).toBeNull();
    sessionStorage.setItem(d.storageKey, '["an","array"]');
    expect(d.read()).toBeNull();
  });
});

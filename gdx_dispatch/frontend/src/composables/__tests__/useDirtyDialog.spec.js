import { describe, it, expect, vi, afterEach } from 'vitest';
import { ref } from 'vue';
import { useDirtyDialog } from '../useDirtyDialog';

afterEach(() => vi.restoreAllMocks());

describe('useDirtyDialog', () => {
  it('is clean right after snapshot and dirty after an edit', () => {
    const form = ref({ name: '', qty: 1 });
    const { snapshot, isDirty } = useDirtyDialog(() => form.value);
    snapshot();
    expect(isDirty.value).toBe(false);
    form.value.name = 'spring';
    expect(isDirty.value).toBe(true);
  });

  it('confirmDiscard passes through without prompting when clean', () => {
    const form = ref({ a: 1 });
    const { snapshot, confirmDiscard } = useDirtyDialog(() => form.value);
    snapshot();
    const spy = vi.spyOn(window, 'confirm');
    expect(confirmDiscard()).toBe(true);
    expect(spy).not.toHaveBeenCalled();
  });

  it('confirmDiscard prompts when dirty and honors the answer', () => {
    const form = ref({ a: 1 });
    const { snapshot, confirmDiscard } = useDirtyDialog(() => form.value);
    snapshot();
    form.value.a = 2;
    const spy = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    expect(confirmDiscard()).toBe(false);
    expect(confirmDiscard()).toBe(true);
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it('re-snapshot after save resets dirtiness', () => {
    const form = ref({ a: 1 });
    const { snapshot, isDirty } = useDirtyDialog(() => form.value);
    snapshot();
    form.value.a = 2;
    expect(isDirty.value).toBe(true);
    snapshot();
    expect(isDirty.value).toBe(false);
  });
});

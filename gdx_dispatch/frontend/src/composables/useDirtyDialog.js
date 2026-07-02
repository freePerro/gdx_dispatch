/**
 * useDirtyDialog — unsaved-changes guard for Dialog-based forms.
 *
 * 2026-07-01 UX audit: only ~2 of the app's form dialogs warned before
 * discarding typed-in work (Esc / the header X silently dropped it).
 * This is the one shared mechanism; views opt in per dialog.
 *
 * Usage:
 *   const { snapshot, isDirty, confirmDiscard } = useDirtyDialog(() => form.value)
 *   function openDialog() { ...populate form...; snapshot(); visible.value = true }
 *   <Dialog :closable="!isDirty" :close-on-escape="!isDirty" ...>
 *     <Button label="Cancel" @click="confirmDiscard() && (visible = false)" />
 *
 * The state getter must return something JSON-serializable (plain form
 * object). Snapshot comparison is by JSON string — cheap and sufficient
 * for flat form models; don't feed it refs holding class instances.
 */
import { ref, computed } from 'vue';

export function useDirtyDialog(getState, { message } = {}) {
  const _snapshot = ref('null');

  /** Capture the pristine form state. Call right after populating the form. */
  function snapshot() {
    _snapshot.value = JSON.stringify(getState() ?? null);
  }

  const isDirty = computed(() => JSON.stringify(getState() ?? null) !== _snapshot.value);

  /**
   * Returns true when it's safe to close: form is clean OR the user
   * explicitly confirmed the discard.
   */
  function confirmDiscard(customMessage) {
    if (!isDirty.value) return true;
    return window.confirm(
      customMessage || message || 'You have unsaved changes. Discard them?'
    );
  }

  return { snapshot, isDirty, confirmDiscard };
}

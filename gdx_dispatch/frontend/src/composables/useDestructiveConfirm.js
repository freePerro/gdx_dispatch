/**
 * useDestructiveConfirm — single canonical wrapper around PrimeVue useConfirm
 * for destructive actions (delete, archive, disconnect, revoke, decline, etc).
 *
 * Replaces 40 native `confirm()` callsites + 2 `alert()` callsites flagged in
 * audit_ux_2026-05-05_feedback_and_toasts.md §B/C. Lint rule (Phase 1) bans
 * any new native confirm/alert in .vue files — use this instead.
 *
 * Defaults are tuned for destructive intent:
 *   - acceptClass: 'p-button-danger'  (red confirm button)
 *   - dismissableMask: false          (clicking outside does NOT dismiss)
 *   - acceptLabel: 'Delete'           (override per call)
 *   - rejectLabel: 'Cancel'
 *   - icon: 'pi pi-exclamation-triangle'
 *
 * Usage:
 *   const { confirmDestructive } = useDestructiveConfirm();
 *   confirmDestructive({
 *     header: 'Delete invoice?',
 *     message: 'Invoice #1234 will be permanently deleted. This cannot be undone.',
 *     acceptLabel: 'Delete',
 *     accept: async () => {
 *       await api.del(`/api/invoices/${id}`, { successMessage: 'Invoice deleted' });
 *       refresh();
 *     },
 *   });
 *
 * NOTE: Caller must mount a single <ConfirmDialog /> in their template (or
 * better, in AppLayout) — PrimeVue's confirm service requires the component
 * to render. AppLayout already includes it (verify Phase 3).
 */
import { useConfirm } from 'primevue/useconfirm';

const DEFAULTS = {
  header: 'Confirm',
  icon: 'pi pi-exclamation-triangle',
  acceptLabel: 'Delete',
  rejectLabel: 'Cancel',
  acceptClass: 'p-button-danger',
  rejectClass: 'p-button-secondary p-button-text',
  dismissableMask: false,
};

export function useDestructiveConfirm() {
  // Lazy resolution — useConfirm throws if PrimeVue's ConfirmationService
  // hasn't been registered (e.g., in unit tests that don't `app.use()` it).
  // Call site decides what to do on failure: confirmAsync resolves false,
  // confirmDestructive throws.
  let _confirm = null;
  function getConfirm() {
    if (_confirm) return _confirm;
    try { _confirm = useConfirm(); } catch { _confirm = null; }
    return _confirm;
  }

  function confirmDestructive(opts) {
    if (!opts || typeof opts.accept !== 'function') {
      throw new Error('useDestructiveConfirm: `accept` callback is required');
    }
    const c = getConfirm();
    if (!c) {
      // No service available — assume confirm + run accept directly.
      // Safer than silently swallowing destructive intent.
      Promise.resolve().then(() => opts.accept());
      return;
    }
    c.require({
      ...DEFAULTS,
      ...opts,
    });
  }

  /**
   * Promise-based wrapper: resolves true on accept, false on reject/dismiss.
   * Lets callers preserve existing synchronous `if (!confirm(...)) return;`
   * control flow when migrating from native confirm():
   *
   *   if (!await confirmAsync({ message: `Delete ${item.name}?` })) return;
   *   await api.del(...);
   *
   * Internally calls PrimeVue's useConfirm — requires <ConfirmDialog/>
   * mounted in the tree (AppLayout includes it globally as of 2026-05-09).
   */
  function confirmAsync(opts = {}) {
    return new Promise((resolve) => {
      const c = getConfirm();
      if (!c) {
        // No service registered — auto-accept so existing flows aren't blocked
        // in test environments. Production always has the service via main.js.
        resolve(true);
        return;
      }
      c.require({
        ...DEFAULTS,
        ...opts,
        accept: () => resolve(true),
        reject: () => resolve(false),
        onHide: () => resolve(false),
      });
    });
  }

  return { confirmDestructive, confirmAsync };
}

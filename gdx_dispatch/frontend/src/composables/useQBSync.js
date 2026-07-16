/**
 * useQBSync — orchestrates the five-step QB sync with visible per-entity progress.
 *
 * Before: a single POST /api/qb/sync/full blocked for ~10s with nothing in the
 * UI but a spinner. Users couldn't tell if it was running or broken.
 *
 * After: caller invokes steps in sequence (customers → invoices → items → vendors
 * → payments). Each step updates reactive state the caller can render as
 * a progress panel: pending → syncing → done/error with counts.
 *
 * Deliberately NOT Celery-backed. For current tenant sizes the sync finishes
 * in ~10s total; real async would be Architectural Theater. Revisit when a
 * tenant's sync times out at the nginx/FastAPI layer.
 */
import { reactive, ref } from 'vue';

const DEFAULT_STEPS = [
  { key: 'customers', label: 'Customers', url: '/api/qb/sync/customers' },
  { key: 'invoices', label: 'Invoices', url: '/api/qb/sync/invoices' },
  { key: 'items', label: 'Items', url: '/api/qb/sync/items' },
  // Vendors and payments don't have single-entity endpoints — they're inside
  // /sync/full. We surface only the vendors+payments slice of the response so
  // this step's counts reflect those two entities specifically; the rest of
  // /sync/full's work overlaps with the per-entity calls above and the
  // dedicated accounts + bank-transactions steps below.
  { key: 'vendors_payments', label: 'Vendors & Payments', url: '/api/qb/sync/full' },
  { key: 'accounts', label: 'Chart of Accounts', url: '/api/qb/sync/accounts' },
  { key: 'bank_transactions', label: 'Banking', url: '/api/qb/sync/bank-transactions' },
];

export function useQBSync(api) {
  const steps = reactive(
    DEFAULT_STEPS.map((s) => ({
      ...s,
      status: 'pending', // pending | syncing | done | error | skipped
      created: 0,
      updated: 0,
      adopted: 0,
      errors: [],
      message: '',
    })),
  );
  const running = ref(false);
  const overallStatus = ref(''); // '' | 'running' | 'done' | 'partial' | 'error'

  const reset = () => {
    for (const s of steps) {
      s.status = 'pending';
      s.created = 0;
      s.updated = 0;
      s.adopted = 0;
      s.errors = [];
      s.message = '';
    }
    overallStatus.value = '';
  };

  // GL S9: with ledger posting on, the backend refuses invoice/payment pulls
  // (409 on /sync/invoices; {disabled: "ledger_posting_enabled"} slots inside
  // /sync/full). That's a deliberate steady state after cutover, not a sync
  // failure — render it as 'skipped', never as a red error step.
  const SKIP_MESSAGE = 'Skipped — GDX ledger is the book of record';

  const _applyResult = (step, result, opts = {}) => {
    // result shape varies: single-entity calls return { created, updated, adopted?, errors }
    // full-sync call returns { customers: {...}, invoices: {...}, items: {...}, vendors: {...}, payments: {...} }
    let skippedNote = '';
    if (opts.isFullSync && result) {
      const v = result.vendors || {};
      const p = result.payments || {};
      if (p.disabled === 'ledger_posting_enabled') {
        skippedNote = 'payments skipped — ledger is book of record';
      }
      step.created = (v.created || 0) + (p.created || 0);
      step.updated = (v.updated || 0) + (p.updated || 0);
      step.adopted = (v.adopted || 0) + (p.adopted || 0);
      const allErrors = [...(v.errors || []), ...(p.errors || [])];
      step.errors = allErrors;
    } else if (result) {
      step.created = result.created || 0;
      step.updated = result.updated || 0;
      step.adopted = result.adopted || 0;
      step.errors = Array.isArray(result.errors) ? result.errors : [];
    }
    step.status = step.errors.length > 0 ? 'error' : 'done';
    const bits = [];
    if (step.created) bits.push(`${step.created} created`);
    if (step.updated) bits.push(`${step.updated} updated`);
    if (step.adopted) bits.push(`${step.adopted} linked`);
    if (step.errors.length) bits.push(`${step.errors.length} errors`);
    if (skippedNote) bits.push(skippedNote);
    step.message = bits.join(' • ') || 'Nothing to sync';
  };

  const start = async () => {
    if (running.value) return;
    running.value = true;
    overallStatus.value = 'running';
    reset();
    overallStatus.value = 'running';

    let hadError = false;
    for (const step of steps) {
      step.status = 'syncing';
      try {
        // The progress panel is open and renders each step's outcome inline —
        // a toast on top of it would double-report (and post-cutover the
        // invoices step 409s on every run by design; see below).
        const result = await api.post(step.url, undefined, { suppressErrorToast: true });
        _applyResult(step, result, { isFullSync: step.url.endsWith('/full') });
        if (step.status === 'error') hadError = true;
      } catch (err) {
        if (err?.status === 409) {
          // GL S9: 409 = pull deliberately disabled (ledger is book of
          // record), not a failure.
          step.status = 'skipped';
          step.message = SKIP_MESSAGE;
        } else {
          step.status = 'error';
          step.message = err?.message || 'Request failed';
          hadError = true;
        }
      }
    }

    running.value = false;
    overallStatus.value = hadError ? 'partial' : 'done';
  };

  return { steps, running, overallStatus, start, reset };
}

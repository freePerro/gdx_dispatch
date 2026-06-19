/**
 * useBudgetAnomalies — pulls the anomaly review panel data and applies
 * recategorizations one at a time.
 *
 * Sprint fix-in-quickbooks (2026-05-25). Yellow-tier:
 *   - load(): GET /api/budgets/anomalies?year= — returns accounts net-negative
 *     with their transactions + a per-txn suggested target account.
 *   - apply(txn): POST /api/budgets/recategorize — writes to QB.
 *
 * After every successful apply, the calling view should call
 * `refreshActuals()` on the budget composable so the cached
 * qb_pnl_monthly numbers reflect the change.
 */
import { ref } from 'vue';
import { useApi } from './useApi';

export function useBudgetAnomalies(injectedApi) {
  const api = injectedApi || useApi();

  const year = ref(new Date().getFullYear());
  const data = ref(null);     // { accounts, qb_accounts, accounting_method }
  const loading = ref(false);
  const error = ref(null);

  const applying = ref(new Set());  // txn_ids currently being applied
  const applied = ref(new Map());   // txn_id → {after_account_name}
  const failed = ref(new Map());    // txn_id → reason string

  async function load(forAccountId) {
    loading.value = true;
    error.value = null;
    try {
      const qs = forAccountId
        ? `year=${year.value}&account_id=${encodeURIComponent(forAccountId)}`
        : `year=${year.value}`;
      data.value = await api.get(`/api/budgets/anomalies?${qs}`);
    } catch (err) {
      error.value = err?.message || 'failed to load anomalies';
      data.value = null;
    } finally {
      loading.value = false;
    }
  }

  async function apply(txn, targetAccountId) {
    const id = txn.txn_id;
    applying.value.add(id);
    failed.value.delete(id);
    try {
      const result = await api.post('/api/budgets/recategorize', {
        txn_id: id,
        txn_type: txn.txn_type,
        new_account_id: targetAccountId,
      });
      applied.value.set(id, result);
      return result;
    } catch (err) {
      const reason = err?.message || err?.detail || 'recategorize failed';
      failed.value.set(id, reason);
      throw err;
    } finally {
      applying.value.delete(id);
      // Force reactivity on Set/Map (Vue 3 doesn't auto-track these)
      applying.value = new Set(applying.value);
      applied.value = new Map(applied.value);
      failed.value = new Map(failed.value);
    }
  }

  function openInQB(txn) {
    // QBO has direct deep-links for transactions:
    //   https://app.qbo.intuit.com/app/<entity>?txnId=<id>
    // Entity for Deposit = "deposit", Purchase = "expense", etc.
    const entitySlug = (txn.txn_type || '').toLowerCase() === 'deposit' ? 'deposit' : 'expense';
    const url = `https://app.qbo.intuit.com/app/${entitySlug}?txnId=${encodeURIComponent(txn.txn_id)}`;
    window.open(url, '_blank', 'noopener');
  }

  return {
    year, data, loading, error,
    applying, applied, failed,
    load, apply, openInQB,
  };
}

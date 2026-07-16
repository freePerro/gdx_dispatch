<template>
  <div class="qb-overview-panel">
    <div v-if="status.money_pulls_disabled" class="ledger-banner" data-testid="qb-overview-ledger-banner">
      <i class="pi pi-lock" />
      <div>
        <p class="ledger-banner-title">Invoice &amp; payment pulls are off — GDX ledger is the book of record</p>
        <p class="ledger-banner-detail">
          Ledger posting is enabled, so invoice and payment changes made in QuickBooks no
          longer flow back into GDX. Make corrections in GDX (credit memo, void, adjustment)
          and they push forward to QuickBooks. Customer, item, account, and banking syncs
          still work.
        </p>
      </div>
    </div>

    <div v-if="status.last_error" class="error-banner" data-testid="qb-overview-error-banner">
      <i class="pi pi-exclamation-triangle" />
      <div>
        <p class="error-banner-title">Last sync error</p>
        <p class="error-banner-detail">{{ status.last_error }}</p>
        <p v-if="status.error_count" class="error-banner-meta">
          {{ status.error_count }} error{{ status.error_count === 1 ? '' : 's' }} recorded
        </p>
      </div>
    </div>

    <div class="overview-cards">
      <div class="overview-card">
        <p class="card-label">Connection</p>
        <Tag
          :value="status.connected ? 'Connected' : 'Disconnected'"
          :severity="status.connected ? 'success' : 'danger'"
        />
        <p v-if="status.realm_id" class="card-sub" data-testid="qb-overview-realm">
          Realm: {{ status.realm_id }}
        </p>
      </div>

      <div class="overview-card">
        <p class="card-label">Last Sync</p>
        <p class="card-value" data-testid="qb-overview-last-sync">
          {{ formatDateTime(status.last_sync_at) }}
        </p>
      </div>

      <div class="overview-card">
        <p class="card-label">Delete Sync</p>
        <Tag
          :value="status.delete_sync_enabled ? 'Enabled' : 'Disabled'"
          :severity="status.delete_sync_enabled ? 'warn' : 'secondary'"
          data-testid="qb-overview-delete-sync"
        />
        <p class="card-sub">Off by default — invoice deletions in GDX do not propagate to QuickBooks.</p>
      </div>
    </div>

    <h3 class="section-heading">Entity counts</h3>
    <div v-if="loading" class="spinner-wrap">
      <ProgressSpinner />
    </div>
    <table v-else class="entity-counts" data-testid="qb-overview-entity-counts">
      <thead>
        <tr>
          <th>Entity</th>
          <th class="count-col">Count</th>
          <th class="action-col">Sync</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in entityRows" :key="row.entity" :data-testid="`qb-overview-row-${row.entity}`">
          <td>{{ row.label }}</td>
          <td class="count-col">{{ row.count }}</td>
          <td class="action-col">
            <Button
              v-tooltip="'Sync'"
              :aria-label="`Sync ${row.label}`"
              icon="pi pi-sync"
              size="small"
              severity="secondary"
              :disabled="!status.connected || (status.money_pulls_disabled && row.entity === 'invoices')"
              :data-testid="`qb-overview-sync-${row.entity}`"
              @click="$emit('sync-entity', row.entity)"
            />
          </td>
        </tr>
        <tr v-if="entityRows.every((r) => r.count === 0)">
          <td colspan="3" class="empty">
            No entities synced yet. Click "Sync Now" to pull from QuickBooks.
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { computed } from 'vue';
import Button from 'primevue/button';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';

const props = defineProps({
  status: {
    type: Object,
    required: true,
  },
  loading: {
    type: Boolean,
    default: false,
  },
});

defineEmits(['sync-entity']);

// Endpoints exposed by gdx/modules/quickbooks/router.py:
//   /sync/customers, /sync/invoices, /sync/items,
//   /sync/accounts, /sync/bank-transactions
// Entity-type keys come from QBEntityMap.entity_type — keep the mapping
// here so the table renders even when a count is missing (zero default).
const ENTITY_DEFS = [
  { entity: 'customers', label: 'Customers', countKey: 'customer' },
  { entity: 'invoices', label: 'Invoices', countKey: 'invoice' },
  { entity: 'items', label: 'Items', countKey: 'item' },
  { entity: 'accounts', label: 'Chart of Accounts', countKey: 'account' },
  { entity: 'bank-transactions', label: 'Banking', countKey: 'bank_transaction' },
];

const entityRows = computed(() => {
  const counts = props.status.entity_counts || {};
  return ENTITY_DEFS.map((d) => ({
    entity: d.entity,
    label: d.label,
    count: Number(counts[d.countKey] || 0),
  }));
});

const formatDateTime = (value) => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
};
</script>

<style scoped>
.qb-overview-panel { padding: 0.5rem 0 1rem; }

.error-banner {
  display: flex;
  gap: 0.75rem;
  padding: 0.85rem 1rem;
  background: var(--p-red-50, #fef2f2);
  border: 1px solid var(--p-red-300, #fca5a5);
  border-radius: 6px;
  color: var(--p-red-900, #450a0a);
  margin-bottom: 1rem;
}

.ledger-banner {
  display: flex;
  gap: 0.75rem;
  padding: 0.85rem 1rem;
  background: var(--p-blue-50, #eff6ff);
  border: 1px solid var(--p-blue-300, #93c5fd);
  border-radius: 6px;
  color: var(--p-blue-900, #1e3a8a);
  margin-bottom: 1rem;
}
.ledger-banner i { font-size: 1.25rem; color: var(--p-blue-600, #2563eb); }
.ledger-banner-title { margin: 0; font-weight: 700; }
.ledger-banner-detail { margin: 0.15rem 0 0; font-weight: 500; }
.error-banner i { font-size: 1.25rem; color: var(--p-red-600, #dc2626); }
.error-banner-title { margin: 0; font-weight: 700; }
.error-banner-detail { margin: 0.15rem 0 0; font-weight: 500; }
.error-banner-meta { margin: 0.25rem 0 0; font-size: 0.85em; color: var(--p-red-700, #b91c1c); }

.overview-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 0.75rem;
  margin-bottom: 1.25rem;
}
.overview-card {
  padding: 0.85rem 1rem;
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e2e8f0);
  border-radius: 6px;
}
.card-label {
  margin: 0 0 0.4rem;
  font-size: 0.85em;
  color: var(--p-text-muted-color, #64748b);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.card-value {
  margin: 0;
  font-weight: 600;
  color: var(--p-text-color, #0f172a);
}
.card-sub {
  margin: 0.4rem 0 0;
  font-size: 0.85em;
  color: var(--p-text-muted-color, #64748b);
}

.section-heading {
  margin: 1rem 0 0.5rem;
  color: var(--p-text-color, #0f172a);
  font-size: 1.05rem;
  font-weight: 700;
}

.entity-counts {
  width: 100%;
  border-collapse: collapse;
}
.entity-counts th, .entity-counts td {
  padding: 0.55rem 0.75rem;
  text-align: left;
  border-bottom: 1px solid var(--p-content-border-color, #e2e8f0);
}
.entity-counts th { font-size: 0.85em; color: var(--p-text-muted-color, #64748b); text-transform: uppercase; }
.count-col { width: 120px; text-align: right; }
.action-col { width: 80px; text-align: right; }
.entity-counts .empty { color: var(--p-text-muted-color, #64748b); text-align: center; padding: 1.25rem; }

.spinner-wrap { display: flex; justify-content: center; padding: 1.5rem; }
</style>

<template>
  <div class="qb-accounts-panel">
    <div class="panel-toolbar">
      <Button
        label="Sync Accounts"
        icon="pi pi-sync"
        size="small"
        :loading="syncing"
        data-testid="qb-accounts-sync-btn"
        @click="syncAccounts"
      />
      <Button
        label="Refresh"
        icon="pi pi-refresh"
        size="small"
        severity="secondary"
        :loading="loading"
        data-testid="qb-accounts-refresh-btn"
        @click="fetch"
      />
    </div>

    <div v-if="loading && !accounts.length" class="spinner-wrap">
      <ProgressSpinner />
    </div>
    <DataTable
      v-else
      :value="accounts"
      striped-rows
      paginator
      :rows="20"
      data-testid="qb-accounts-table"
    >
      <Column field="name" header="Name" sortable :style="{ minWidth: '200px' }" />
      <Column field="account_type" header="Type" sortable :style="{ minWidth: '140px' }" />
      <Column field="account_sub_type" header="Subtype" sortable :style="{ minWidth: '140px' }" />
      <Column field="classification" header="Classification" sortable :style="{ minWidth: '120px' }" />
      <Column header="Balance" :style="{ width: '140px', textAlign: 'right' }">
        <template #body="{ data }">{{ formatCurrency(data.current_balance) }}</template>
      </Column>
      <Column header="Active" :style="{ width: '90px' }">
        <template #body="{ data }">
          <Tag :value="data.active ? 'Yes' : 'No'" :severity="data.active ? 'success' : 'secondary'" />
        </template>
      </Column>
      <template #empty>
        <p class="empty">
          No accounts synced yet. Click "Sync Accounts" to pull the Chart of Accounts from QuickBooks.
        </p>
      </template>
    </DataTable>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';
import { useApiWithToast } from '../../composables/useApiWithToast';

const api = useApiWithToast();
const accounts = ref([]);
const loading = ref(false);
const syncing = ref(false);

const fetch = async () => {
  loading.value = true;
  try {
    const resp = await api.get('/api/qb/accounts');
    accounts.value = Array.isArray(resp?.items) ? resp.items : [];
  } catch (err) {
    api.toast?.add?.({
      severity: 'error',
      summary: 'Could not load accounts',
      detail: err?.message || 'Unknown error',
      life: 4000,
    });
  } finally {
    loading.value = false;
  }
};

const syncAccounts = async () => {
  syncing.value = true;
  try {
    await api.post('/api/qb/sync/accounts', undefined, { successMessage: 'Chart of Accounts synced' });
    await fetch();
  } catch (err) {
    api.toast?.add?.({
      severity: 'error',
      summary: 'Sync failed',
      detail: err?.message || 'Unknown error',
      life: 4000,
    });
  } finally {
    syncing.value = false;
  }
};

const formatCurrency = (value) => {
  const num = Number(value);
  if (Number.isNaN(num)) return '—';
  return num.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
};

onMounted(fetch);
</script>

<style scoped>
.qb-accounts-panel { padding: 0.5rem 0 1rem; }
.panel-toolbar {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}
.spinner-wrap { display: flex; justify-content: center; padding: 1.5rem; }
.empty {
  color: var(--p-text-muted-color, #64748b);
  text-align: center;
  padding: 1.25rem;
  margin: 0;
}
</style>

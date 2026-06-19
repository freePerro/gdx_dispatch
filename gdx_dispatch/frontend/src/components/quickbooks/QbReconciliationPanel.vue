<template>
  <div class="qb-reconciliation-panel">
    <div class="reconciliation-banner" :class="status.delete_sync_enabled ? 'warn' : 'info'" data-testid="qb-reconciliation-banner">
      <i :class="status.delete_sync_enabled ? 'pi pi-exclamation-triangle' : 'pi pi-info-circle'" />
      <div class="banner-body">
        <p class="banner-title">
          Delete sync: {{ status.delete_sync_enabled ? 'ENABLED' : 'DISABLED' }}
        </p>
        <p class="banner-detail">
          When enabled, customers / invoices / payments that disappear from
          QuickBooks are soft-deleted locally. Each soft-delete is recorded
          below.
          <span v-if="status.delete_sync_source === 'env'">
            Currently following the global <code>QB_DELETE_SYNC_ENABLED</code>
            env var. Flip the toggle to override for this tenant.
          </span>
          <span v-else>
            Per-tenant override is active. Clear it to fall back to the
            global default.
          </span>
        </p>
      </div>
      <div class="banner-toggle">
        <ToggleSwitch
          v-model="toggleValue"
          data-testid="qb-reconciliation-toggle"
          :disabled="toggling"
          @update:modelValue="onToggle"
        />
        <Button
          v-if="status.delete_sync_source === 'tenant'"
          label="Clear override"
          severity="secondary"
          size="small"
          text
          :disabled="toggling"
          data-testid="qb-reconciliation-clear-override"
          @click="onClearOverride"
        />
      </div>
    </div>

    <div class="panel-toolbar">
      <Button
        label="Refresh"
        icon="pi pi-refresh"
        size="small"
        severity="secondary"
        :loading="loading"
        data-testid="qb-reconciliation-refresh-btn"
        @click="fetch"
      />
    </div>

    <div v-if="loading && !rows.length" class="spinner-wrap">
      <ProgressSpinner />
    </div>
    <DataTable
      v-else
      :value="rows"
      striped-rows
      paginator
      :rows="20"
      data-testid="qb-reconciliation-table"
    >
      <Column header="When" :style="{ minWidth: '200px' }">
        <template #body="{ data }">{{ formatDateTime(data.timestamp) }}</template>
      </Column>
      <Column header="Entity" :style="{ minWidth: '140px' }">
        <template #body="{ data }">
          <Tag :value="data.details?.entity_type || '—'" severity="secondary" />
        </template>
      </Column>
      <Column field="entity_id" header="QB ID" :style="{ minWidth: '160px' }" />
      <Column header="Local ID" :style="{ minWidth: '260px' }">
        <template #body="{ data }">
          <code class="local-id">{{ data.details?.local_id || '—' }}</code>
        </template>
      </Column>
      <Column header="Reason" :style="{ minWidth: '200px' }">
        <template #body="{ data }">{{ data.details?.reason || '—' }}</template>
      </Column>
      <template #empty>
        <p class="empty">
          {{ status.delete_sync_enabled
              ? 'No reconciliation deletes recorded yet.'
              : 'No reconciliation deletes — flag is currently disabled, so no soft-deletes can occur.' }}
        </p>
      </template>
    </DataTable>
  </div>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';
import ToggleSwitch from 'primevue/toggleswitch';
import { useApiWithToast } from '../../composables/useApiWithToast';

const api = useApiWithToast();
const rows = ref([]);
const loading = ref(false);
const status = ref({ delete_sync_enabled: false, delete_sync_source: 'env' });
const toggleValue = ref(false);
const toggling = ref(false);

watch(
  () => status.value.delete_sync_enabled,
  (v) => { toggleValue.value = !!v; },
  { immediate: true },
);

const fetch = async () => {
  loading.value = true;
  try {
    const [events, dashboard] = await Promise.all([
      api.get('/api/qb/events?action=qb_delete_sync&limit=200'),
      api.get('/api/qb/dashboard').catch(() => ({})),
    ]);
    rows.value = Array.isArray(events?.events) ? events.events : [];
    status.value = {
      delete_sync_enabled: !!dashboard?.delete_sync_enabled,
      delete_sync_source: dashboard?.delete_sync_source || 'env',
    };
  } catch (err) {
    api.toast?.add?.({
      severity: 'error',
      summary: 'Could not load reconciliation log',
      detail: err?.message || 'Unknown error',
      life: 4000,
    });
  } finally {
    loading.value = false;
  }
};

const onToggle = async (next) => {
  // Optimistic flip — POST the new value and refetch on success/failure.
  // ToggleSwitch already updated toggleValue locally; we revert if the POST
  // fails so the UI doesn't claim a state the backend rejected.
  toggling.value = true;
  try {
    await api.post('/api/qb/settings/delete-sync', { enabled: !!next });
    api.toast?.add?.({
      severity: 'success',
      summary: next ? 'Delete sync enabled' : 'Delete sync disabled',
      detail: next
        ? 'Missing QuickBooks rows will now soft-delete locally on next sync.'
        : 'Delete detection paused. Existing local rows are unaffected.',
      life: 4000,
    });
    await fetch();
  } catch (err) {
    api.toast?.add?.({
      severity: 'error',
      summary: 'Could not update flag',
      detail: err?.message || 'Unknown error',
      life: 4000,
    });
    toggleValue.value = !next;
  } finally {
    toggling.value = false;
  }
};

const onClearOverride = async () => {
  // Sends enabled: null — backend clears the column and the effective state
  // falls back to the env var default.
  toggling.value = true;
  try {
    await api.post('/api/qb/settings/delete-sync', { enabled: null });
    api.toast?.add?.({
      severity: 'success',
      summary: 'Override cleared',
      detail: 'Now following the global default.',
      life: 3000,
    });
    await fetch();
  } catch (err) {
    api.toast?.add?.({
      severity: 'error',
      summary: 'Could not clear override',
      detail: err?.message || 'Unknown error',
      life: 4000,
    });
  } finally {
    toggling.value = false;
  }
};

const formatDateTime = (value) => {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
};

onMounted(fetch);
</script>

<style scoped>
.qb-reconciliation-panel { padding: 0.5rem 0 1rem; }

.reconciliation-banner {
  display: flex;
  gap: 0.75rem;
  padding: 0.85rem 1rem;
  border-radius: 6px;
  margin-bottom: 1rem;
  align-items: flex-start;
}
.banner-body { flex: 1; }
.banner-toggle {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 0.4rem;
}
.reconciliation-banner.warn {
  background: var(--p-amber-50, #fffbeb);
  border: 1px solid var(--p-amber-300, #fcd34d);
  color: var(--p-amber-900, #78350f);
}
.reconciliation-banner.info {
  background: var(--p-blue-50, #eff6ff);
  border: 1px solid var(--p-blue-300, #93c5fd);
  color: var(--p-blue-900, #1e3a8a);
}
.reconciliation-banner i { font-size: 1.25rem; }
.banner-title { margin: 0; font-weight: 700; }
.banner-detail { margin: 0.2rem 0 0; font-weight: 500; font-size: 0.92em; }
.banner-detail code {
  background: var(--p-content-background, #fff);
  padding: 0 0.3rem;
  border-radius: 3px;
  font-size: 0.9em;
}

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
.local-id {
  font-size: 0.85em;
  color: var(--p-text-muted-color, #475569);
}
</style>

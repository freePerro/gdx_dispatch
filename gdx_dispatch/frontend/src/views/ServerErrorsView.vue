<template>
  <section class="server-errors-view view-card">
    <Toolbar data-testid="server-errors-toolbar">
      <template #start>
        <h2 class="page-title">Server Logs</h2>
        <Tag v-if="stats" :severity="stats.open_total ? 'danger' : 'success'"
             :value="`${stats.open_total} open`" data-testid="server-errors-open-count" />
      </template>
      <template #end>
        <Button label="Refresh" icon="pi pi-refresh" class="p-button-text"
                :loading="loading" @click="loadAll" data-testid="server-errors-refresh" />
      </template>
    </Toolbar>

    <div class="server-errors-filters">
      <div class="filter-tabs" data-testid="server-errors-status-tabs">
        <button v-for="tab in statusTabs" :key="tab"
                :class="{ active: statusFilter === tab }"
                @click="statusFilter = tab; loadErrors()">
          {{ tab === 'all' ? 'All' : tab === 'open' ? 'Open' : 'Resolved' }}
        </button>
      </div>
      <input v-model="pathFilter" class="p-inputtext" placeholder="filter by path…"
             @keyup.enter="loadErrors()" data-testid="server-errors-path-filter" />
    </div>

    <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>
    <DataTable v-else :value="errors" class="clickable-rows" data-testid="server-errors-table"
               @row-click="openDetail($event.data)" :rows="50" paginator>
      <template #empty>No server errors{{ statusFilter === 'open' ? ' — all clear 🎉' : '' }}.</template>
      <Column field="occurred_at" header="When">
        <template #body="{ data }">{{ formatDateTime(data.occurred_at) }}</template>
      </Column>
      <Column field="exception_class" header="Error" />
      <Column field="path" header="Path" />
      <Column field="status_code" header="Status">
        <template #body="{ data }">
          <Tag :severity="data.status_code >= 500 ? 'danger' : 'warn'" :value="String(data.status_code || '—')" />
        </template>
      </Column>
      <Column field="user_email" header="User" />
      <Column header="State">
        <template #body="{ data }">
          <Tag :severity="data.resolved_at ? 'success' : 'secondary'"
               :value="data.resolved_at ? 'resolved' : 'open'" />
        </template>
      </Column>
    </DataTable>

    <Dialog v-model:visible="showDialog" modal header="Error detail" :style="{ width: '60rem' }"
            data-testid="server-errors-dialog">
      <div v-if="selected" class="detail-grid">
        <div><strong>{{ selected.exception_class }}</strong></div>
        <div class="detail-msg">{{ selected.exception_message }}</div>
        <div class="detail-meta">
          {{ selected.method }} {{ selected.path }} · {{ formatDateTime(selected.occurred_at) }}
          <span v-if="selected.user_email"> · {{ selected.user_email }}</span>
          <span v-if="selected.git_sha"> · {{ selected.git_sha }}</span>
        </div>
        <pre class="detail-pre">{{ selected.traceback || '(no traceback captured)' }}</pre>
      </div>
      <template #footer>
        <span v-if="selected && selected.resolved_at" class="resolved-note">
          Resolved by {{ selected.resolved_by || 'system' }}
        </span>
        <template v-else-if="selected">
          <label class="resolve-group">
            <input type="checkbox" v-model="resolveGroup" /> resolve all like this
          </label>
          <Button label="Mark resolved" icon="pi pi-check" :loading="resolving"
                  @click="resolveSelected" data-testid="server-errors-resolve" />
        </template>
      </template>
    </Dialog>
  </section>
</template>

<script setup>
import { onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Toolbar from 'primevue/toolbar';
import Button from 'primevue/button';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Dialog from 'primevue/dialog';
import Tag from 'primevue/tag';
import ProgressSpinner from 'primevue/progressspinner';

const api = useApiWithToast();

const errors = ref([]);
const stats = ref(null);
const loading = ref(false);
const statusFilter = ref('open');
const pathFilter = ref('');
const showDialog = ref(false);
const selected = ref(null);
const resolving = ref(false);
const resolveGroup = ref(false);

const statusTabs = ['open', 'resolved', 'all'];

function formatDateTime(value) {
  if (!value) return '—';
  try { return new Date(value).toLocaleString(); } catch { return value; }
}

async function loadErrors() {
  loading.value = true;
  try {
    const qs = new URLSearchParams({ status: statusFilter.value, page_size: '200' });
    if (pathFilter.value.trim()) qs.set('path', pathFilter.value.trim());
    const data = await api.get(`/api/admin/errors?${qs.toString()}`);
    errors.value = data?.items || [];
  } finally {
    loading.value = false;
  }
}

async function loadStats() {
  try { stats.value = await api.get('/api/admin/errors/stats'); } catch { /* non-critical */ }
}

async function loadAll() {
  await Promise.all([loadErrors(), loadStats()]);
}

async function openDetail(row) {
  resolveGroup.value = false;
  // List rows omit the traceback; fetch the full record.
  try {
    selected.value = await api.get(`/api/admin/errors/${row.id}`);
  } catch {
    selected.value = row;
  }
  showDialog.value = true;
}

async function resolveSelected() {
  if (!selected.value) return;
  resolving.value = true;
  try {
    await api.patch(`/api/admin/errors/${selected.value.id}/resolve`,
      { resolve_group: resolveGroup.value },
      { successMessage: 'Marked resolved' });
    showDialog.value = false;
    await loadAll();
  } finally {
    resolving.value = false;
  }
}

onMounted(loadAll);
</script>

<style scoped>
.server-errors-filters { display: flex; gap: 1rem; align-items: center; margin: 0.75rem 0; flex-wrap: wrap; }
.filter-tabs { display: flex; gap: 0.25rem; }
.filter-tabs button { padding: 0.35rem 0.9rem; border: 1px solid var(--p-content-border-color, #ccc); background: transparent; border-radius: 6px; cursor: pointer; }
.filter-tabs button.active { background: var(--p-primary-color, #2563eb); color: #fff; border-color: transparent; }
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.detail-grid { display: flex; flex-direction: column; gap: 0.5rem; }
.detail-msg { color: var(--p-text-muted-color, #555); }
.detail-meta { font-size: 0.85rem; color: var(--p-text-muted-color, #777); }
.detail-pre { background: #1e1e1e; color: #e6e6e6; padding: 1rem; border-radius: 8px; overflow: auto; max-height: 28rem; font-size: 0.8rem; white-space: pre-wrap; word-break: break-word; }
.resolve-group { margin-right: 1rem; font-size: 0.85rem; }
.resolved-note { color: var(--p-text-muted-color, #777); }
.page-title { margin: 0; }
</style>

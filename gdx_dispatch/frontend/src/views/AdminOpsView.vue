<template>
    <section class="admin-ops-view view-card">
      <Toolbar data-testid="admin-ops-toolbar">
        <template #start>
          <h2 class="page-title">System Admin</h2>
          <span v-if="updateInfo" class="version-badge" data-testid="app-version">
            v{{ updateInfo.current }}
            <template v-if="updateInfo.update_available">
              <Tag severity="warn" :value="`update available → ${updateInfo.latest}`" data-testid="update-available" />
              <a v-if="updateInfo.notes_url" :href="updateInfo.notes_url" target="_blank" rel="noopener noreferrer">release notes</a>
            </template>
          </span>
        </template>
        <template #end>
          <div class="admin-ops-actions">
            <Button
              :label="button.label"
              :icon="button.icon"
              :loading="runningAction === button.payload"
              :severity="button.severity"
              class="p-button-text"
              :data-testid="button.testId"
              v-for="button in actionButtons"
              :key="button.payload"
              @click="handleAction(button.payload)"
            />
          </div>
        </template>
      </Toolbar>

      <div class="admin-ops-filters">
        <div class="filter-tabs" data-testid="admin-ops-status-tabs">
          <Button
            v-for="tab in statusTabs"
            :key="tab"
            :label="tabToLabel(tab)"
            :severity="statusFilter === tab ? undefined : 'secondary'"
            size="small"
            :class="{ active: statusFilter === tab }"
            @click="statusFilter = tab"
          />
        </div>
        <div class="target-filter" data-testid="admin-ops-target-filter">
          <label>Target</label>
          <Select
            v-model="targetFilter"
            :options="targetOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="All targets"
            class="w-full"
          />
        </div>
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-else
        :value="filteredEntries"
        paginator
        :rows="20"
        striped-rows
        responsive-layout="scroll"
        
        data-testid="admin-ops-table"
        @row-click="openDetails($event.data)"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-history"
            title="No audit entries yet"
            message="Administrative actions are logged here as they happen."
          />
        </template>
        <Column field="action" header="Action" />
        <Column field="target" header="Target" />
        <Column field="performed_by" header="Performed By" />
        <Column field="performed_at" header="Performed At">
          <template #body="{ data }">{{ formatDate(data.performed_at) }}</template>
        </Column>
        <Column field="status" header="Status" style="width:140px">
          <template #body="{ data }">
            <Tag :value="formatStatus(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        modal
        header="Audit entry"
        :style="{ width: '480px' }"
        data-testid="admin-ops-dialog"
      >
        <div v-if="selectedEntry" class="detail-grid">
          <div>
            <label>Action</label>
            <p>{{ selectedEntry.action }}</p>
          </div>
          <div>
            <label>Target</label>
            <p>{{ selectedEntry.target }}</p>
          </div>
          <div>
            <label>Performed By</label>
            <p>{{ selectedEntry.performed_by }}</p>
          </div>
          <div>
            <label>Performed At</label>
            <p>{{ formatDateTime(selectedEntry.performed_at) }}</p>
          </div>
          <div>
            <label>Status</label>
            <Tag :value="formatStatus(selectedEntry.status)" :severity="statusSeverity(selectedEntry.status)" />
          </div>
          <div v-if="selectedEntry.details">
            <label>Details</label>
            <pre class="detail-pre">{{ selectedEntry.details }}</pre>
          </div>
        </div>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDate, formatDateTime } from '../composables/useFormatters';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Toolbar from 'primevue/toolbar';
import Select from 'primevue/select';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Dialog from 'primevue/dialog';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';

const api = useApiWithToast();

const entries = ref([]);
const loading = ref(false);
const statusFilter = ref('all');
const targetFilter = ref(null);
const showDialog = ref(false);
const selectedEntry = ref(null);
const runningAction = ref('');
const updateInfo = ref(null);

const statusTabs = ['all', 'success', 'failed'];

const actionButtons = [
  { label: 'Run maintenance', payload: 'run-maintenance', icon: 'pi pi-cog', severity: 'primary', testId: 'run-maintenance-btn' },
  { label: 'Rebuild indexes', payload: 'rebuild-indexes', icon: 'pi pi-sync', severity: 'info', testId: 'rebuild-indexes-btn' },
  { label: 'Clear cache', payload: 'clear-cache', icon: 'pi pi-trash', severity: 'warning', testId: 'clear-cache-btn' },
  { label: 'Export audit log', payload: 'export-audit-log', icon: 'pi pi-download', severity: 'secondary', testId: 'export-audit-log-btn' },
];

const targetOptions = computed(() => {
  const base = [{ label: 'All targets', value: null }];
  const seen = new Set();
  for (const entry of entries.value) {
    if (!entry.target || seen.has(entry.target)) continue;
    seen.add(entry.target);
    base.push({ label: entry.target, value: entry.target });
  }
  return base;
});

const filteredEntries = computed(() => {
  return entries.value.filter((entry) => {
    const matchesTarget = targetFilter.value ? entry.target === targetFilter.value : true;
    const matchesStatus =
      statusFilter.value === 'all'
        ? true
        : statusFilter.value === 'success'
        ? entry.status === 'success'
        : entry.status === 'failed';
    return matchesTarget && matchesStatus;
  });
});

function statusSeverity(status) {
  return status === 'success' ? 'success' : status === 'failed' ? 'danger' : 'info';
}

function tabToLabel(tab) {
  return tab === 'all' ? 'All' : tab === 'success' ? 'Success' : 'Failed';
}

function formatStatus(status) {
  return status?.replace('_', ' ') || 'Unknown';
}

async function loadEntries() {
  loading.value = true;
  try {
    const data = await api.get('/api/admin-ops');
    entries.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

async function handleAction(action) {
  runningAction.value = action;
  try {
    await api.post('/api/admin-ops/actions', { action });
    await loadEntries();
  } finally {
    runningAction.value = '';
  }
}

function openDetails(entry) {
  selectedEntry.value = entry;
  showDialog.value = true;
}

async function loadUpdateInfo() {
  // Best-effort: the endpoint always returns 200 (errors land in .error), so a
  // GitHub outage shows the running version without a scary toast.
  try {
    updateInfo.value = await api.get('/api/admin/update-check');
  } catch {
    /* ignore — version badge is non-critical */
  }
}

onMounted(() => {
  loadEntries();
  loadUpdateInfo();
});
</script>

<template>
    <section class="quickbooks-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">QuickBooks Sync</h2>
        </template>
        <template #end>
          <Button
            label="Connect"
            icon="pi pi-link"
            :loading="actionLoading === 'connect'"
            class="p-button-outlined"
            data-testid="quickbooks-connect-btn"
            @click="runAction('connect')"
          />
          <Button
            label="Disconnect"
            icon="pi pi-unlink"
            :loading="actionLoading === 'disconnect'"
            class="p-button-outlined"
            data-testid="quickbooks-disconnect-btn"
            @click="runAction('disconnect')"
          />
          <Button
            label="Sync Now"
            icon="pi pi-sync"
            :loading="actionLoading === 'sync'"
            data-testid="quickbooks-sync-btn"
            @click="runAction('sync')"
          />
          <QbSyncScheduleSelect data-testid="qb-schedule-select-wrap" />
        </template>
      </Toolbar>

      <Tabs v-model:value="activeTab" class="quickbooks-tabview">
        <TabList>
          <Tab value="overview" data-testid="qb-tab-overview">Overview</Tab>
          <Tab value="log" data-testid="qb-tab-log">Sync Log</Tab>
          <Tab value="accounts" data-testid="qb-tab-accounts">Chart of Accounts</Tab>
          <Tab value="bank-transactions" data-testid="qb-tab-bank-transactions">Banking</Tab>
          <Tab value="reconciliation" data-testid="qb-tab-reconciliation">Reconciliation</Tab>
        </TabList>
        <TabPanels>
          <TabPanel value="overview">
            <QbOverviewPanel
              :status="quickbooksStatus"
              :loading="loading"
              data-testid="qb-overview-panel"
              @sync-entity="onSyncEntity"
            />
          </TabPanel>

          <TabPanel value="log">
            <div class="filter-tabs" data-testid="quickbooks-tabs">
              <Button
                v-for="tab in logTabs"
                :key="tab"
                :label="tabLabel(tab)"
                :severity="logFilter === tab ? undefined : 'secondary'"
                size="small"
                class="p-button-text"
                :data-testid="`quickbooks-tab-${tab}`"
                @click="logFilter = tab"
              />
            </div>

            <div v-if="loading" class="spinner-wrap">
              <ProgressSpinner />
            </div>
            <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
              v-else
              :value="filteredEvents"
              striped-rows
              paginator
              :rows="10"
              
              data-testid="quickbooks-table"
              @row-click="openEventDialog"
            >
              <template #empty>
                <EmptyState
                  icon="pi pi-sync"
                  title="No sync events yet"
                  message="QuickBooks sync activity will be logged here after your first sync."
                />
              </template>
              <Column header="Timestamp" :style="{ minWidth: '180px' }">
                <template #body="{ data }">{{ formatDateTime(data.timestamp) }}</template>
              </Column>
              <Column field="type" header="Type" :style="{ minWidth: '160px' }" />
              <Column field="count" header="Count" :style="{ width: '120px' }" />
              <Column header="Status" :style="{ width: '140px' }">
                <template #body="{ data }">
                  <Tag :value="normalizeStatus(data.status)" :severity="eventSeverity(data.status)" />
                </template>
              </Column>
            </DataTable>
          </TabPanel>

          <TabPanel value="accounts">
            <QbAccountsPanel data-testid="qb-accounts-panel" />
          </TabPanel>

          <TabPanel value="bank-transactions">
            <QbBankingPanel data-testid="qb-banking-panel" />
          </TabPanel>

          <TabPanel value="reconciliation">
            <QbReconciliationPanel data-testid="qb-reconciliation-panel" />
          </TabPanel>
        </TabPanels>
      </Tabs>

      <Dialog
        v-model:visible="showSyncProgress"
        :header="syncProgressHeader"
        :modal="true"
        :closable="!syncRunning"
        :close-on-escape="!syncRunning"
        :style="{ width: '540px' }"
        data-testid="qb-sync-progress-dialog"
      >
        <div class="sync-progress">
          <p v-if="syncRunning" class="sync-progress-hint">
            Syncing with QuickBooks. This can take up to a minute depending on how much data you have.
          </p>
          <p v-else-if="syncOverall === 'done'" class="sync-progress-hint success">
            All entities synced successfully.
          </p>
          <p v-else-if="syncOverall === 'partial'" class="sync-progress-hint warn">
            Sync finished with some issues. Review the details below.
          </p>

          <ul class="sync-steps">
            <li v-for="step in syncSteps" :key="step.key" :class="['sync-step', 'status-' + step.status]" :data-testid="`qb-sync-step-${step.key}`">
              <span class="sync-step-icon">
                <i v-if="step.status === 'pending'" class="pi pi-clock" />
                <i v-else-if="step.status === 'syncing'" class="pi pi-spin pi-spinner" />
                <i v-else-if="step.status === 'done'" class="pi pi-check-circle" />
                <i v-else-if="step.status === 'skipped'" class="pi pi-lock" />
                <i v-else class="pi pi-exclamation-circle" />
              </span>
              <span class="sync-step-label">{{ step.label }}</span>
              <span class="sync-step-message">{{ step.message || stepDefaultMessage(step.status) }}</span>
            </li>
          </ul>

          <div v-if="hasAnySyncErrors" class="sync-error-list" data-testid="qb-sync-errors">
            <h4>Issues</h4>
            <ul>
              <li v-for="(e, i) in allSyncErrors" :key="i">
                <code>{{ e.qb_id || '?' }}</code> — {{ e.error || 'unknown' }}
              </li>
            </ul>
          </div>
        </div>
        <template #footer>
          <Button
            label="Close"
            severity="secondary"
            :disabled="syncRunning"
            @click="showSyncProgress = false"
            data-testid="qb-sync-progress-close"
          />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="showEventDialog"
        header="Sync Event"
        :modal="true"
        :style="{ width: '480px' }"
        data-testid="quickbooks-event-dialog"
      >
        <div v-if="selectedEvent">
          <div class="form-field full-width">
            <p class="field-label">Timestamp</p>
            <p>{{ formatDateTime(selectedEvent.timestamp) }}</p>
          </div>
          <div class="form-field full-width">
            <p class="field-label">Type</p>
            <p>{{ selectedEvent.type || '—' }}</p>
          </div>
          <div class="form-field full-width">
            <p class="field-label">Count</p>
            <p>{{ selectedEvent.count ?? '—' }}</p>
          </div>
          <div class="form-field full-width">
            <p class="field-label">Status</p>
            <Tag :value="normalizeStatus(selectedEvent.status)" :severity="eventSeverity(selectedEvent.status)" />
          </div>
          <div class="form-field full-width">
            <p class="field-label">Details</p>
            <p>{{ selectedEvent.details || selectedEvent.message || 'No additional details' }}</p>
          </div>
        </div>
        <template #footer>
          <Button label="Close" severity="secondary" @click="showEventDialog = false" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import EmptyState from '../components/EmptyState.vue';
import QbOverviewPanel from '../components/quickbooks/QbOverviewPanel.vue';
import QbAccountsPanel from '../components/quickbooks/QbAccountsPanel.vue';
import QbBankingPanel from '../components/quickbooks/QbBankingPanel.vue';
import QbSyncScheduleSelect from '../components/quickbooks/QbSyncScheduleSelect.vue';
import QbReconciliationPanel from '../components/quickbooks/QbReconciliationPanel.vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { useQBSync } from '../composables/useQBSync';
import { formatDateTime } from '../composables/useFormatters';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import ProgressSpinner from 'primevue/progressspinner';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();
const route = useRoute();
const router = useRouter();

const VALID_TABS = ['overview', 'log', 'accounts', 'bank-transactions', 'reconciliation'];
const initialTab = VALID_TABS.includes(route.query.tab) ? route.query.tab : 'overview';
const activeTab = ref(initialTab);

watch(activeTab, (next) => {
  if (route.query.tab === next) return;
  router.replace({ query: { ...route.query, tab: next } });
});

const quickbooksStatus = ref({
  connected: false,
  last_sync_at: null,
  realm_id: null,
  error_count: 0,
  last_error: null,
  entity_counts: {},
  delete_sync_enabled: false,
});
const syncEvents = ref([]);
const loading = ref(true);
const logFilter = ref('all');
const logTabs = ['all', 'success', 'failed'];
const actionLoading = ref('');
const showEventDialog = ref(false);
const selectedEvent = ref(null);

const normalizeStatus = (status) => {
  const normalized = (status || '').toString().toLowerCase();
  if (normalized === 'success') return 'success';
  if (normalized === 'failed' || normalized === 'error') return 'failed';
  return normalized || 'failed';
};

const eventSeverity = (status) => {
  return normalizeStatus(status) === 'success' ? 'success' : 'danger';
};

const filteredEvents = computed(() => {
  if (logFilter.value === 'all') return syncEvents.value;
  return syncEvents.value.filter((event) => normalizeStatus(event.status) === logFilter.value);
});

const counts = computed(() => {
  const map = { all: syncEvents.value.length, success: 0, failed: 0 };
  syncEvents.value.forEach((event) => {
    const status = normalizeStatus(event.status);
    if (status === 'success') map.success += 1;
    else map.failed += 1;
  });
  return map;
});

const tabLabel = (tab) => {
  const label = tab.charAt(0).toUpperCase() + tab.slice(1);
  const suffix = counts.value[tab] ? ` (${counts.value[tab]})` : '';
  return `${label}${suffix}`;
};

const loadQuickbooks = async () => {
  loading.value = true;
  try {
    const [dashboard, events] = await Promise.all([
      // /dashboard returns connection + entity_counts + delete_sync_enabled.
      // Falls back to /status if /dashboard fails (older builds).
      api.get('/api/qb/dashboard').catch(() => api.get('/api/qb/status')),
      api.get('/api/qb/events?limit=50').catch(() => ({ events: [] })),
    ]);
    quickbooksStatus.value = {
      connected: !!dashboard.connected,
      last_sync_at: dashboard.last_sync_at || null,
      realm_id: dashboard.realm_id || null,
      error_count: dashboard.error_count || 0,
      last_error: dashboard.last_error || null,
      entity_counts: dashboard.entity_counts || {},
      delete_sync_enabled: !!dashboard.delete_sync_enabled,
      money_pulls_disabled: !!dashboard.money_pulls_disabled,
    };
    syncEvents.value = Array.isArray(events?.events) ? events.events : [];
  } catch (err) {
    api.toast?.add?.({
      severity: 'error',
      summary: 'QuickBooks load failed',
      detail: err?.message || 'Unable to fetch QuickBooks status',
      life: 4000,
    });
  } finally {
    loading.value = false;
  }
};

// Sync progress state (per-entity visible feedback)
const { steps: syncSteps, running: syncRunning, overallStatus: syncOverall, start: startSync } = useQBSync(api);
const showSyncProgress = ref(false);

const syncProgressHeader = computed(() => {
  if (syncRunning.value) return 'QuickBooks sync in progress';
  if (syncOverall.value === 'done') return 'QuickBooks sync complete';
  if (syncOverall.value === 'partial') return 'QuickBooks sync finished with issues';
  return 'QuickBooks sync';
});

const stepDefaultMessage = (status) => {
  if (status === 'pending') return 'Waiting';
  if (status === 'syncing') return 'Fetching from QuickBooks...';
  if (status === 'done') return 'Up to date';
  if (status === 'error') return 'Failed';
  return '';
};

const allSyncErrors = computed(() => syncSteps.flatMap((s) => s.errors || []));
const hasAnySyncErrors = computed(() => allSyncErrors.value.length > 0);

const onSyncEntity = async (entity) => {
  // Per-entity sync from Overview tab. Routes to the same endpoints the
  // composable exposes: /sync/customers, /sync/invoices, /sync/items,
  // /sync/accounts, /sync/bank-transactions. After completion, refresh
  // the dashboard so entity_counts reflect the new state.
  if (!entity) return;
  const url = `/api/qb/sync/${entity}`;
  try {
    await api.post(url, undefined, { successMessage: `${entity} synced` });
    await loadQuickbooks();
  } catch (err) {
    api.toast?.add?.({
      severity: 'error',
      summary: `${entity} sync failed`,
      detail: err?.message || 'Unknown error',
      life: 4000,
    });
  }
};

const runAction = async (action) => {
  const actionMap = {
    connect: { url: '/api/qb/connect', message: 'Opening QuickBooks authorization...' },
    disconnect: { url: '/api/qb/disconnect', message: 'Disconnected from QuickBooks' },
    sync: { url: '/api/qb/sync', message: 'QuickBooks sync triggered' },
  };
  if (!actionMap[action]) return;
  actionLoading.value = action;
  try {
    if (action === 'sync') {
      showSyncProgress.value = true;
      await startSync();
      await loadQuickbooks();
      return;
    }
    if (action === 'connect') {
      const response = await api.post(actionMap[action].url);
      if (response?.redirect_url) {
        const popup = window.open(response.redirect_url, '_blank', 'width=600,height=700');
        if (!popup || popup.closed || typeof popup.closed === 'undefined') {
          api.toast?.add?.({
            severity: 'warn',
            summary: 'Popup blocked',
            detail: 'Redirecting in this window instead...',
            life: 3000,
          });
          window.location.href = response.redirect_url;
        }
      }
      return;
    }
    await api.post(actionMap[action].url, undefined, { successMessage: actionMap[action].message });
    await loadQuickbooks();
  } finally {
    actionLoading.value = '';
  }
};

const openEventDialog = ({ data }) => {
  selectedEvent.value = data;
  showEventDialog.value = true;
};

function onOAuthMessage(event) {
  if (event.origin !== window.location.origin) return;
  const data = event.data;
  if (!data || data.type !== 'qb_oauth_result') return;
  actionLoading.value = '';
  if (data.status === 'connected') {
    loadQuickbooks();
  }
}

onMounted(() => {
  window.addEventListener('message', onOAuthMessage);
  loadQuickbooks();
});

onBeforeUnmount(() => {
  window.removeEventListener('message', onOAuthMessage);
});
</script>

<style scoped>
.quickbooks-tabview {
  margin-top: 1rem;
}

.sync-progress-hint {
  margin: 0 0 1rem;
  color: var(--p-text-color, #0f172a);
  font-weight: 500;
}
.sync-progress-hint.success { color: var(--p-green-700, #15803d); font-weight: 600; }
.sync-progress-hint.warn { color: var(--p-amber-700, #b45309); font-weight: 600; }

.sync-steps {
  list-style: none;
  padding: 0;
  margin: 0;
}
.sync-step {
  display: grid;
  grid-template-columns: 32px 180px 1fr;
  gap: 0.75rem;
  align-items: center;
  padding: 0.7rem 0.5rem;
  border-bottom: 1px solid var(--p-content-border-color, #cbd5e1);
}
.sync-step:last-child { border-bottom: none; }
.sync-step-icon { font-size: 1.35rem; text-align: center; }
.sync-step.status-pending .sync-step-icon { color: var(--p-text-muted-color, #64748b); }
.sync-step.status-syncing .sync-step-icon { color: var(--p-blue-600, #2563eb); }
.sync-step.status-done .sync-step-icon { color: var(--p-green-600, #16a34a); }
.sync-step.status-error .sync-step-icon { color: var(--p-red-600, #dc2626); }
.sync-step.status-skipped .sync-step-icon { color: var(--p-text-muted-color, #64748b); }
.sync-step-label {
  font-weight: 600;
  color: var(--p-text-color, #0f172a);
  font-size: 1rem;
}
.sync-step-message {
  color: var(--p-text-color, #1e293b);
  font-size: 0.95em;
  font-weight: 500;
}

.sync-error-list {
  margin-top: 1rem;
  padding: 0.75rem 1rem;
  background: var(--p-red-50, #fef2f2);
  border: 1px solid var(--p-red-300, #fca5a5);
  border-radius: 6px;
}
.sync-error-list h4 {
  margin: 0 0 0.5rem;
  color: var(--p-red-800, #7f1d1d);
  font-weight: 700;
}
.sync-error-list ul {
  margin: 0;
  padding-left: 1.25rem;
  color: var(--p-red-900, #450a0a);
  font-size: 0.92em;
  font-weight: 500;
}
.sync-error-list code {
  background: var(--p-red-100, #fee2e2);
  color: var(--p-red-900, #450a0a);
  padding: 0 0.35rem;
  border-radius: 3px;
  font-size: 0.88em;
  font-weight: 600;
}
</style>

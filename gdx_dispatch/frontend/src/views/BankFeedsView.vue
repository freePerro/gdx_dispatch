<template>
  <section class="bank-feeds-view view-card">
    <Toolbar>
      <template #start>
        <h2 class="page-title">Bank Feeds</h2>
      </template>
      <template #end>
        <Button
          v-if="canManage"
          label="Sync Now"
          icon="pi pi-sync"
          :loading="actionLoading === 'sync'"
          data-testid="bank-feeds-sync-btn"
          @click="syncNow()"
        />
      </template>
    </Toolbar>

    <Tabs v-model:value="activeTab" class="bank-feeds-tabview">
      <TabList>
        <Tab value="banks" data-testid="bank-feeds-tab-banks">Banks</Tab>
        <Tab value="accounts" data-testid="bank-feeds-tab-accounts">Accounts</Tab>
        <Tab value="transactions" data-testid="bank-feeds-tab-transactions">Transactions</Tab>
        <Tab value="statements" data-testid="bank-feeds-tab-statements">Statements</Tab>
        <Tab value="settings" data-testid="bank-feeds-tab-settings">Settings</Tab>
      </TabList>
      <TabPanels>
        <!-- ── Banks ─────────────────────────────────────────────── -->
        <TabPanel value="banks">
          <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>
          <template v-else>
            <DataTable :value="institutions" striped-rows responsiveLayout="scroll" data-testid="bank-feeds-banks-table">
              <template #empty>
                <EmptyState
                  icon="pi pi-building-columns"
                  title="No banks connected"
                  :message="canManage
                    ? 'Add your bank to start syncing accounts, transactions, and statements.'
                    : 'No banks are configured yet. Ask an admin to add one.'"
                />
              </template>
              <Column field="label" header="Bank" :style="{ minWidth: '160px' }" />
              <Column header="Status" :style="{ minWidth: '150px' }">
                <template #body="{ data }">
                  <Tag :value="stateLabel(data)" :severity="stateSeverity(data)" />
                </template>
              </Column>
              <Column header="Accounts" :style="{ width: '110px' }">
                <template #body="{ data }">{{ data.account_count }}</template>
              </Column>
              <Column header="Last Sync" :style="{ minWidth: '170px' }">
                <template #body="{ data }">
                  {{ data.last_synced_at ? formatDateTime(data.last_synced_at) : '—' }}
                </template>
              </Column>
              <Column header="Statements" :style="{ width: '130px' }">
                <template #body="{ data }">
                  <span v-if="data.documents_available === true">Available</span>
                  <span v-else-if="data.documents_available === false" class="muted">Unavailable</span>
                  <span v-else class="muted">—</span>
                </template>
              </Column>
              <Column v-if="canManage" header="" :style="{ minWidth: '280px' }">
                <template #body="{ data }">
                  <div class="row-actions">
                    <Button
                      v-if="!data.connected"
                      label="Connect"
                      size="small"
                      icon="pi pi-link"
                      :disabled="!data.configured"
                      :loading="actionLoading === `connect-${data.id}`"
                      :data-testid="`bank-connect-${data.id}`"
                      @click="connectBank(data)"
                    />
                    <Button
                      v-else-if="data.auth_state !== 'healthy'"
                      label="Reconnect"
                      size="small"
                      icon="pi pi-refresh"
                      severity="warn"
                      :loading="actionLoading === `connect-${data.id}`"
                      @click="connectBank(data)"
                    />
                    <Button
                      v-if="data.connected"
                      label="Disconnect"
                      size="small"
                      icon="pi pi-unlink"
                      class="p-button-outlined"
                      @click="disconnectBank(data)"
                    />
                    <Button
                      label="Edit"
                      size="small"
                      class="p-button-text"
                      icon="pi pi-pencil"
                      @click="openEditDialog(data)"
                    />
                  </div>
                </template>
              </Column>
            </DataTable>
            <div v-if="canManage" class="add-bank-row">
              <Button
                label="Add bank"
                icon="pi pi-plus"
                class="p-button-outlined"
                data-testid="bank-feeds-add-bank-btn"
                @click="openAddDialog"
              />
            </div>
          </template>
        </TabPanel>

        <!-- ── Accounts ──────────────────────────────────────────── -->
        <TabPanel value="accounts">
          <DataTable :value="accounts" striped-rows responsiveLayout="scroll" data-testid="bank-feeds-accounts-table">
            <template #empty>
              <EmptyState
                icon="pi pi-wallet"
                title="No accounts yet"
                message="Accounts appear here after the first sync of a connected bank."
              />
            </template>
            <Column field="institution_label" header="Bank" :style="{ minWidth: '140px' }" />
            <Column header="Account" :style="{ minWidth: '180px' }">
              <template #body="{ data }">
                {{ data.name || 'Account' }}
                <span v-if="data.account_number_masked" class="muted"> {{ data.account_number_masked }}</span>
              </template>
            </Column>
            <Column field="account_type" header="Type" :style="{ width: '130px' }" />
            <Column header="Balance" :style="{ width: '140px' }">
              <template #body="{ data }">
                {{ data.balance != null ? formatMoney(data.balance) : '—' }}
              </template>
            </Column>
            <Column header="Last Sync" :style="{ minWidth: '170px' }">
              <template #body="{ data }">
                {{ data.last_synced_at ? formatDateTime(data.last_synced_at) : '—' }}
              </template>
            </Column>
            <Column header="Sync" :style="{ width: '100px' }">
              <template #body="{ data }">
                <InputSwitch
                  :modelValue="data.sync_enabled"
                  :disabled="!canManage"
                  @update:modelValue="(v) => toggleAccount(data, v)"
                />
              </template>
            </Column>
            <Column header="" :style="{ width: '110px' }">
              <template #body="{ data }">
                <Tag v-if="data.is_inactive" value="inactive" severity="secondary" />
              </template>
            </Column>
          </DataTable>
        </TabPanel>

        <!-- ── Transactions ──────────────────────────────────────── -->
        <TabPanel value="transactions">
          <div class="filters-row">
            <Select
              v-model="txnFilters.institution_id"
              :options="institutionOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All banks"
              showClear
              class="filter-input"
              @change="reloadTransactions"
            />
            <Select
              v-model="txnFilters.account_id"
              :options="accountOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All accounts"
              showClear
              class="filter-input"
              @change="reloadTransactions"
            />
            <DatePicker v-model="txnFilters.date_from" placeholder="From" dateFormat="yy-mm-dd" showIcon class="filter-input" @update:modelValue="reloadTransactions" />
            <DatePicker v-model="txnFilters.date_to" placeholder="To" dateFormat="yy-mm-dd" showIcon class="filter-input" @update:modelValue="reloadTransactions" />
            <InputText v-model="txnFilters.q" placeholder="Search payee / memo" class="filter-input" @keyup.enter="reloadTransactions" />
            <label class="pending-toggle">
              <Checkbox v-model="txnFilters.include_pending" binary @change="reloadTransactions" />
              <span>Include pending</span>
            </label>
          </div>
          <DataTable
            :value="transactions"
            lazy
            paginator
            :rows="txnPaging.limit"
            :totalRecords="txnPaging.total"
            :loading="txnLoading"
            striped-rows
            responsiveLayout="scroll"
            data-testid="bank-feeds-transactions-table"
            @page="onTxnPage"
          >
            <template #empty>
              <EmptyState
                icon="pi pi-list"
                title="No transactions"
                message="Synced bank transactions will appear here."
              />
            </template>
            <Column header="Date" :style="{ width: '120px' }">
              <template #body="{ data }">
                {{ data.posted_date || '—' }}
              </template>
            </Column>
            <Column field="payee" header="Payee" :style="{ minWidth: '180px' }" />
            <Column field="memo" header="Memo" :style="{ minWidth: '220px' }" />
            <Column field="institution_label" header="Bank" :style="{ width: '130px' }" />
            <Column header="Amount" :style="{ width: '130px' }">
              <template #body="{ data }">
                <span :class="amountClass(data.amount_cents)">{{ formatCents(data.amount_cents) }}</span>
              </template>
            </Column>
            <Column header="" :style="{ width: '110px' }">
              <template #body="{ data }">
                <Tag v-if="data.pending" value="pending" severity="warn" />
              </template>
            </Column>
          </DataTable>
        </TabPanel>

        <!-- ── Statements ────────────────────────────────────────── -->
        <TabPanel value="statements">
          <div class="filters-row">
            <Select
              v-model="docFilters.document_type"
              :options="docTypeOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All types"
              showClear
              class="filter-input"
              @change="reloadDocuments"
            />
            <Select
              v-model="docFilters.institution_id"
              :options="institutionOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All banks"
              showClear
              class="filter-input"
              @change="reloadDocuments"
            />
          </div>
          <DataTable
            :value="documents"
            lazy
            paginator
            :rows="docPaging.limit"
            :totalRecords="docPaging.total"
            :loading="docLoading"
            striped-rows
            responsiveLayout="scroll"
            data-testid="bank-feeds-statements-table"
            @page="onDocPage"
          >
            <template #empty>
              <EmptyState
                icon="pi pi-file-pdf"
                :title="statementsUnavailable ? 'Statements unavailable' : 'No statements yet'"
                :message="statementsUnavailable
                  ? 'Your bank has not made documents available for this connection — check eStatement enrollment in your bank\'s own app.'
                  : 'Statement PDFs are archived here after each sync.'"
              />
            </template>
            <Column header="Date" :style="{ width: '120px' }">
              <template #body="{ data }">{{ data.document_date || '—' }}</template>
            </Column>
            <Column field="title" header="Title" :style="{ minWidth: '220px' }" />
            <Column header="Type" :style="{ width: '120px' }">
              <template #body="{ data }">
                <Tag :value="data.document_type" :severity="data.document_type === 'statement' ? 'info' : 'secondary'" />
              </template>
            </Column>
            <Column field="institution_label" header="Bank" :style="{ width: '140px' }" />
            <Column header="" :style="{ width: '130px' }">
              <template #body="{ data }">
                <Button
                  v-if="data.fetched"
                  label="Download"
                  size="small"
                  icon="pi pi-download"
                  class="p-button-text"
                  :data-testid="`bank-doc-download-${data.id}`"
                  @click="downloadDocument(data)"
                />
                <Tag v-else value="queued" severity="secondary" />
              </template>
            </Column>
          </DataTable>
        </TabPanel>

        <!-- ── Settings ──────────────────────────────────────────── -->
        <TabPanel value="settings">
          <div class="settings-panel">
            <h3>Sync schedule</h3>
            <div class="settings-row">
              <label>Frequency</label>
              <Select
                v-model="scheduleForm.frequency"
                :options="frequencyOptions"
                optionLabel="label"
                optionValue="value"
                :disabled="!canManage"
                class="filter-input"
                data-testid="bank-feeds-frequency-select"
              />
            </div>
            <div class="settings-row">
              <label>Backfill history (days)</label>
              <InputNumber
                v-model="scheduleForm.backfill_days"
                :min="1"
                :max="3650"
                :disabled="!canManage"
                data-testid="bank-feeds-backfill-days"
              />
            </div>
            <p class="muted">
              Changing the backfill window only affects future full re-syncs — history already synced is kept.
            </p>
            <div class="settings-row" v-if="schedule.last_run_at">
              <label>Last run</label>
              <span>
                {{ formatDateTime(schedule.last_run_at) }}
                <Tag
                  v-if="schedule.last_run_status"
                  :value="schedule.last_run_status"
                  :severity="schedule.last_run_status === 'ok' ? 'success' : (schedule.last_run_status === 'partial' ? 'warn' : 'danger')"
                />
              </span>
            </div>
            <Button
              v-if="canManage"
              label="Save schedule"
              icon="pi pi-save"
              :loading="actionLoading === 'schedule'"
              data-testid="bank-feeds-save-schedule"
              @click="saveSchedule"
            />
          </div>
        </TabPanel>
      </TabPanels>
    </Tabs>

    <!-- ── Add / Edit bank dialog ─────────────────────────────────── -->
    <Dialog
      v-model:visible="showBankDialog"
      :header="editingInstitutionId ? 'Edit bank' : 'Add bank'"
      modal
      :style="{ width: '460px' }"
    >
      <div class="dialog-form">
        <label>Banno hostname
          <InputText
            v-model="bankForm.fi_host"
            placeholder="digital.yourbank.com"
            data-testid="bank-form-host"
          />
        </label>
        <label>Display name
          <InputText v-model="bankForm.display_label" placeholder="My Bank" data-testid="bank-form-label" />
        </label>
        <label>Client ID
          <InputText v-model="bankForm.client_id" autocomplete="off" data-testid="bank-form-client-id" />
        </label>
        <label>Client secret
          <Password
            v-model="bankForm.client_secret"
            :feedback="false"
            toggleMask
            autocomplete="new-password"
            :placeholder="editingSecretSet ? '•••••• (already set — leave blank to keep)' : ''"
            data-testid="bank-form-client-secret"
          />
        </label>
        <p class="muted">
          Each bank provisions these credentials as an “external application” in its Banno back office.
        </p>
      </div>
      <template #footer>
        <Button label="Cancel" class="p-button-text" @click="showBankDialog = false" />
        <Button
          :label="editingInstitutionId ? 'Save' : 'Add bank'"
          :loading="actionLoading === 'save-bank'"
          data-testid="bank-form-save"
          @click="saveBank"
        />
      </template>
    </Dialog>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { useAuthStore } from '../stores/auth';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDateTime } from '../composables/useFormatters';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Checkbox from 'primevue/checkbox';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputSwitch from 'primevue/inputswitch';
import InputText from 'primevue/inputtext';
import Password from 'primevue/password';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();
const auth = useAuthStore();

const canManage = computed(() => auth.hasPermission('bank_feeds.manage'));

const activeTab = ref('banks');
const loading = ref(true);
const actionLoading = ref('');

const institutions = ref([]);
const schedule = ref({});
const accounts = ref([]);

// ── status + accounts ──────────────────────────────────────────────

const loadStatus = async () => {
  loading.value = true;
  try {
    const [status, accts] = await Promise.all([
      api.get('/api/bank-feeds/status'),
      api.get('/api/bank-feeds/accounts').catch(() => ({ accounts: [] })),
    ]);
    institutions.value = status.institutions || [];
    schedule.value = status.schedule || {};
    scheduleForm.frequency = schedule.value.frequency || 'manual';
    scheduleForm.backfill_days = schedule.value.backfill_days || 365;
    accounts.value = accts.accounts || [];
  } finally {
    loading.value = false;
  }
};

const stateLabel = (inst) => {
  if (!inst.configured) return 'not configured';
  if (!inst.connected) return 'not connected';
  if (inst.auth_state === 'healthy') return 'connected';
  return inst.auth_state.replace('_', ' ');
};

const stateSeverity = (inst) => {
  if (!inst.configured || !inst.connected) return 'secondary';
  if (inst.auth_state === 'healthy') return 'success';
  return 'warn';
};

const statementsUnavailable = computed(
  () => institutions.value.length > 0
    && institutions.value.every((i) => i.documents_available === false)
);

// ── OAuth popup connect flow (QB pattern + close-poll fallback) ────

let popupWatch = null;

const connectBank = async (inst) => {
  actionLoading.value = `connect-${inst.id}`;
  try {
    const response = await api.post('/api/bank-feeds/connect', { institution_id: inst.id });
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
        return;
      }
      // Fallback for postMessage origin mismatches: when the popup closes
      // without a message, re-poll status once so the UI can't strand.
      if (popupWatch) clearInterval(popupWatch);
      popupWatch = setInterval(() => {
        if (popup.closed) {
          clearInterval(popupWatch);
          popupWatch = null;
          loadStatus();
        }
      }, 1000);
    }
  } finally {
    actionLoading.value = '';
  }
};

function onOAuthMessage(event) {
  if (event.origin !== window.location.origin) return;
  const data = event.data;
  if (!data || data.type !== 'bank_feeds_oauth_result') return;
  actionLoading.value = '';
  if (data.status === 'connected') {
    loadStatus();
  }
}

const disconnectBank = async (inst) => {
  await api.post(
    '/api/bank-feeds/disconnect',
    { institution_id: inst.id },
    { successMessage: 'Bank disconnected — synced data is kept.' },
  );
  await loadStatus();
};

const syncNow = async () => {
  actionLoading.value = 'sync';
  try {
    await api.post('/api/bank-feeds/sync', {}, { successMessage: 'Sync queued' });
  } finally {
    actionLoading.value = '';
  }
};

// ── add / edit bank ────────────────────────────────────────────────

const showBankDialog = ref(false);
const editingInstitutionId = ref(null);
const editingSecretSet = ref(false);
const bankForm = reactive({ fi_host: '', display_label: '', client_id: '', client_secret: '' });

const openAddDialog = () => {
  editingInstitutionId.value = null;
  editingSecretSet.value = false;
  Object.assign(bankForm, { fi_host: '', display_label: '', client_id: '', client_secret: '' });
  showBankDialog.value = true;
};

const openEditDialog = async (inst) => {
  editingInstitutionId.value = inst.id;
  const detail = await api.get('/api/bank-feeds/institutions');
  const row = (detail.institutions || []).find((i) => i.id === inst.id) || {};
  editingSecretSet.value = !!row.secret_set;
  Object.assign(bankForm, {
    fi_host: row.fi_host || '',
    display_label: row.display_label || '',
    client_id: row.client_id || '',
    client_secret: '',
  });
  showBankDialog.value = true;
};

const saveBank = async () => {
  actionLoading.value = 'save-bank';
  try {
    const payload = {
      fi_host: bankForm.fi_host,
      display_label: bankForm.display_label,
      client_id: bankForm.client_id,
    };
    if (bankForm.client_secret) payload.client_secret = bankForm.client_secret;
    if (editingInstitutionId.value) {
      await api.patch(`/api/bank-feeds/institutions/${editingInstitutionId.value}`, payload, {
        successMessage: 'Bank updated',
      });
    } else {
      await api.post('/api/bank-feeds/institutions', { ...payload, client_secret: bankForm.client_secret || '' }, {
        successMessage: 'Bank added',
      });
    }
    showBankDialog.value = false;
    await loadStatus();
  } finally {
    actionLoading.value = '';
  }
};

// ── accounts toggle ────────────────────────────────────────────────

const toggleAccount = async (account, value) => {
  await api.patch(`/api/bank-feeds/accounts/${account.id}`, { sync_enabled: value });
  account.sync_enabled = value;
};

// ── transactions ───────────────────────────────────────────────────

const transactions = ref([]);
const txnLoading = ref(false);
const txnFilters = reactive({
  institution_id: null,
  account_id: null,
  date_from: null,
  date_to: null,
  q: '',
  include_pending: true,
});
const txnPaging = reactive({ limit: 50, offset: 0, total: 0 });

const toDateParam = (d) => {
  if (!d) return null;
  if (typeof d === 'string') return d.slice(0, 10);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

const loadTransactions = async () => {
  txnLoading.value = true;
  try {
    const params = new URLSearchParams();
    if (txnFilters.institution_id) params.set('institution_id', txnFilters.institution_id);
    if (txnFilters.account_id) params.set('account_id', txnFilters.account_id);
    const from = toDateParam(txnFilters.date_from);
    const to = toDateParam(txnFilters.date_to);
    if (from) params.set('date_from', from);
    if (to) params.set('date_to', to);
    if (txnFilters.q) params.set('q', txnFilters.q);
    params.set('include_pending', String(txnFilters.include_pending));
    params.set('limit', String(txnPaging.limit));
    params.set('offset', String(txnPaging.offset));
    const data = await api.get(`/api/bank-feeds/transactions?${params.toString()}`);
    transactions.value = data.items || [];
    txnPaging.total = data.total || 0;
  } finally {
    txnLoading.value = false;
  }
};

const reloadTransactions = () => {
  txnPaging.offset = 0;
  loadTransactions();
};

const onTxnPage = (event) => {
  txnPaging.offset = event.first;
  loadTransactions();
};

const institutionOptions = computed(() =>
  institutions.value.map((i) => ({ label: i.label, value: i.id }))
);
const accountOptions = computed(() =>
  accounts.value.map((a) => ({
    label: `${a.name || 'Account'} ${a.account_number_masked || ''}`.trim(),
    value: a.id,
  }))
);

const formatCents = (cents) => {
  if (cents == null) return '—';
  const value = cents / 100;
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
};
const formatMoney = (v) => Number(v).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
const amountClass = (cents) => (cents != null && cents < 0 ? 'amount-out' : 'amount-in');

// ── documents ──────────────────────────────────────────────────────

const documents = ref([]);
const docLoading = ref(false);
const docFilters = reactive({ document_type: 'statement', institution_id: null });
const docPaging = reactive({ limit: 50, offset: 0, total: 0 });
const docTypeOptions = [
  { label: 'Statements', value: 'statement' },
  { label: 'Notices', value: 'notice' },
  { label: 'Tax documents', value: 'tax' },
];

const loadDocuments = async () => {
  docLoading.value = true;
  try {
    const params = new URLSearchParams();
    if (docFilters.document_type) params.set('document_type', docFilters.document_type);
    if (docFilters.institution_id) params.set('institution_id', docFilters.institution_id);
    params.set('limit', String(docPaging.limit));
    params.set('offset', String(docPaging.offset));
    const data = await api.get(`/api/bank-feeds/documents?${params.toString()}`);
    documents.value = data.items || [];
    docPaging.total = data.total || 0;
  } finally {
    docLoading.value = false;
  }
};

const reloadDocuments = () => {
  docPaging.offset = 0;
  loadDocuments();
};

const onDocPage = (event) => {
  docPaging.offset = event.first;
  loadDocuments();
};

function deriveTenantId() {
  if (auth.tenantSlug) return auth.tenantSlug;
  const parts = window.location.hostname.split('.');
  const slug = parts.length >= 3 ? parts[0] : null;
  return slug && slug !== 'gdx' ? slug : '';
}

const downloadDocument = async (doc) => {
  const headers = new Headers();
  const tenantId = deriveTenantId();
  if (tenantId) headers.set('x-tenant-id', tenantId);
  if (auth.accessToken) headers.set('Authorization', `Bearer ${auth.accessToken}`);
  const response = await fetch(`/api/bank-feeds/documents/${doc.id}/download`, {
    headers,
    credentials: 'include',
  });
  if (!response.ok) {
    api.toast?.add?.({ severity: 'error', summary: 'Download failed', life: 4000 });
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = doc.title || `statement-${doc.document_date || 'document'}.pdf`;
  link.click();
  URL.revokeObjectURL(url);
};

// ── schedule ───────────────────────────────────────────────────────

const scheduleForm = reactive({ frequency: 'manual', backfill_days: 365 });
const frequencyOptions = [
  { label: 'Manual only', value: 'manual' },
  { label: 'Hourly', value: 'hourly' },
  { label: 'Every 4 hours', value: 'every_4h' },
  { label: 'Daily', value: 'daily' },
  { label: 'Weekly', value: 'weekly' },
];

const saveSchedule = async () => {
  actionLoading.value = 'schedule';
  try {
    const data = await api.put('/api/bank-feeds/schedule', {
      frequency: scheduleForm.frequency,
      backfill_days: scheduleForm.backfill_days,
    }, { successMessage: 'Schedule saved' });
    schedule.value = data;
  } finally {
    actionLoading.value = '';
  }
};

// ── lifecycle ──────────────────────────────────────────────────────

onMounted(() => {
  window.addEventListener('message', onOAuthMessage);
  loadStatus();
  loadTransactions();
  loadDocuments();
});

onBeforeUnmount(() => {
  window.removeEventListener('message', onOAuthMessage);
  if (popupWatch) clearInterval(popupWatch);
});
</script>

<style scoped>
.bank-feeds-tabview {
  margin-top: 1rem;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem;
}
.row-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.add-bank-row {
  margin-top: 1rem;
}
.filters-row {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 1rem;
}
.filter-input {
  min-width: 170px;
}
.pending-toggle {
  display: inline-flex;
  gap: 0.4rem;
  align-items: center;
}
.amount-out {
  color: var(--p-red-600, #dc2626);
  font-variant-numeric: tabular-nums;
}
.amount-in {
  color: var(--p-green-700, #15803d);
  font-variant-numeric: tabular-nums;
}
.muted {
  color: var(--p-text-muted-color, #64748b);
}
.settings-panel {
  max-width: 480px;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.settings-row {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.dialog-form {
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}
.dialog-form label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-weight: 500;
}
</style>

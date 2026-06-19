<template>
    <section class="inbound-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Inbound Communications</h2>
        </template>
      </Toolbar>

      <div class="split-pane">
        <div class="list-pane">
          <Tabs v-model:value="activeTab" class="list-tabs">
            <TabList>
              <Tab value="sms">SMS</Tab>
              <Tab value="email">Email</Tab>
            </TabList>
            <TabPanels>
            <TabPanel value="sms">
              <div class="list-filter">
                <InputText
                  v-model="smsFilter"
                  placeholder="Filter by phone number"
                  class="w-full"
                  @keyup.enter="loadSms"
                />
                <Button label="Refresh" icon="pi pi-sync" class="p-button-text" @click="loadSms" />
              </div>
              <div v-if="loadingSms" class="spinner-wrap"><ProgressSpinner /></div>
              <DataTable
        class="clickable-rows smsRowClass"
      responsiveLayout="scroll"
                v-else
                :value="smsMessages"
                row-key="id"
                paginator
                :rows="12"
                striped-rows
                
                @row-click="handleRowClick('sms', $event)"
                :row-
              >
                <Column field="from_number" header="From" />
                <Column header="Received" :body="formatDateCell" />
                <Column header="Linked Job" :body="linkLabel('job')" />
                <Column header="Linked Customer" :body="linkLabel('customer')" />
              </DataTable>
              <div v-if="!loadingSms && !smsMessages.length" class="empty-state">
                <i class="pi pi-comments" />
                <p>No SMS so far.</p>
              </div>
            </TabPanel>

            <TabPanel value="email">
              <div class="list-filter">
                <InputText
                  v-model="emailFilter"
                  placeholder="Filter by sender email"
                  class="w-full"
                  @keyup.enter="loadEmail"
                />
                <Button label="Refresh" icon="pi pi-sync" class="p-button-text" @click="loadEmail" />
              </div>
              <div v-if="loadingEmail" class="spinner-wrap"><ProgressSpinner /></div>
              <DataTable
        class="clickable-rows emailRowClass"
      responsiveLayout="scroll"
                v-else
                :value="emailMessages"
                row-key="id"
                paginator
                :rows="12"
                striped-rows
                
                @row-click="handleRowClick('email', $event)"
                :row-
              >
                <Column field="from_email" header="From" />
                <Column field="subject" header="Subject" />
                <Column header="Status">
                  <template #body="{ data }">
                    <Tag
                      :value="data.read_at ? 'Read' : 'Unread'"
                      :severity="data.read_at ? 'success' : 'danger'"
                    />
                  </template>
                </Column>
                <Column header="Received" :body="formatDateCell" />
              </DataTable>
              <div v-if="!loadingEmail && !emailMessages.length" class="empty-state">
                <i class="pi pi-envelope" />
                <p>No emails yet.</p>
              </div>
            </TabPanel>
            </TabPanels>
          </Tabs>
        </div>

        <div class="detail-pane">
          <div v-if="detailLoading" class="spinner-wrap"><ProgressSpinner /></div>
          <div v-else-if="selectedMessage" class="detail-card card">
            <div class="detail-header">
              <div>
                <div class="detail-title">{{ detailTitle }}</div>
                <div class="detail-subtitle">
                  <span v-if="selectedMessage.type === 'sms'">From: {{ selectedMessage.from_number }}</span>
                  <span v-else>From: {{ selectedMessage.from_email }}</span>
                  <span class="detail-date">• {{ formatDateValue(selectedMessage.received_at) }}</span>
                </div>
                <div class="detail-tags">
                  <Tag v-if="selectedMessage.type === 'email'" :value="selectedMessage.read_at ? 'Read' : 'Unread'" :severity="selectedMessage.read_at ? 'success' : 'danger'" />
                  <Tag :value="`Provider: ${selectedMessage.provider || 'n/a'}`" severity="secondary" />
                </div>
                <div class="link-status">
                  <Tag
                    v-if="selectedMessage.customer_id"
                    :value="`Customer ${selectedMessage.customer_id}`"
                    severity="info"
                  />
                  <Tag v-else value="No customer linked" severity="warn" />
                  <Tag
                    v-if="selectedMessage.job_id"
                    :value="`Job ${selectedMessage.job_id}`"
                    severity="info"
                  />
                  <Tag v-else value="No job linked" severity="warn" />
                </div>
              </div>
              <div class="detail-actions">
                <Button label="Link to Customer" icon="pi pi-user" text size="small" @click="openLinkDialog('customer')" />
                <Button label="Link to Job" icon="pi pi-briefcase" text size="small" @click="openLinkDialog('job')" />
              </div>
            </div>
            <pre class="detail-body">{{ detailBody }}</pre>
          </div>
          <div v-else class="empty-detail">
            <i class="pi pi-inbox" />
            <p>Select a message to view the details.</p>
          </div>
        </div>
      </div>

      <Dialog v-model:visible="showLinkCustomer" header="Link to Customer" :style="{ width: '400px' }" modal>
        <form class="link-form" @submit.prevent="submitLink('customer')">
          <div class="form-field">
            <label>Customer ID</label>
            <InputText v-model="linkForm.customer_id" placeholder="Enter customer ID" class="w-full" />
          </div>
          <div class="form-actions">
            <Button label="Cancel" text @click="showLinkCustomer = false" />
            <Button label="Link" severity="primary" type="submit" :loading="linking" />
          </div>
        </form>
      </Dialog>

      <Dialog v-model:visible="showLinkJob" header="Link to Job" :style="{ width: '400px' }" modal>
        <form class="link-form" @submit.prevent="submitLink('job')">
          <div class="form-field">
            <label>Job ID</label>
            <InputText v-model="linkForm.job_id" placeholder="Enter job ID" class="w-full" />
          </div>
          <div class="form-actions">
            <Button label="Cancel" text @click="showLinkJob = false" />
            <Button label="Link" severity="primary" type="submit" :loading="linking" />
          </div>
        </form>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();
const activeTab = ref('sms');
const smsMessages = ref([]);
const emailMessages = ref([]);
const loadingSms = ref(true);
const loadingEmail = ref(true);
const detailLoading = ref(false);
const selectedMessage = ref(null);
const smsFilter = ref('');
const emailFilter = ref('');
const showLinkCustomer = ref(false);
const showLinkJob = ref(false);
const linking = ref(false);
const linkForm = ref({ customer_id: '', job_id: '' });

const SMS_LIMIT = 200;
const EMAIL_LIMIT = 200;

const detailBody = computed(() => {
  if (!selectedMessage.value) return '';
  return selectedMessage.value.body_text || selectedMessage.value.body || selectedMessage.value.body_html || '';
});

const detailTitle = computed(() => {
  if (!selectedMessage.value) return '';
  if (selectedMessage.value.type === 'email') {
    return selectedMessage.value.subject || '(No subject)';
  }
  return 'Inbound SMS';
});

const smsRowClass = (row) => (selectedMessage.value?.type === 'sms' && selectedMessage.value?.id === row.id ? 'selected-row' : '');
const emailRowClass = (row) => (selectedMessage.value?.type === 'email' && selectedMessage.value?.id === row.id ? 'selected-row' : '');

onMounted(() => {
  loadSms();
  loadEmail();
});

watch(activeTab, () => {
  selectedMessage.value = null;
});

async function loadSms() {
  loadingSms.value = true;
  try {
    const params = new URLSearchParams();
    params.set('limit', SMS_LIMIT.toString());
    if (smsFilter.value.trim()) {
      params.set('from_number', smsFilter.value.trim());
    }
    const data = await api.get(`/api/inbound-sms?${params.toString()}`);
    smsMessages.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loadingSms.value = false;
  }
}

async function loadEmail() {
  loadingEmail.value = true;
  try {
    const params = new URLSearchParams();
    params.set('limit', EMAIL_LIMIT.toString());
    if (emailFilter.value.trim()) {
      params.set('from_email', emailFilter.value.trim());
    }
    const data = await api.get(`/api/inbound-email?${params.toString()}`);
    emailMessages.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loadingEmail.value = false;
  }
}

function formatDateCell(row) {
  const value = row?.received_at || row?.created_at;
  return formatDateValue(value);
}

function formatDateValue(value) {
  if (!value) return '—';
  return new Date(value).toLocaleString();
}

function linkLabel(kind) {
  return ({ customer_id, job_id }) => {
    if (kind === 'job') {
      return job_id ? `Job ${job_id}` : '—';
    }
    return customer_id ? `Customer ${customer_id}` : '—';
  };
}

async function handleRowClick(type, event) {
  const row = event?.data;
  if (!row) return;
  detailLoading.value = true;
  try {
    const detail = await api.get(`/api/inbound-${type}/${row.id}`);
    let message = { ...detail, type };
    if (type === 'email' && !message.read_at) {
      await markEmailRead(message);
      message = { ...message, read_at: new Date().toISOString() };
    }
    selectedMessage.value = message;
  } finally {
    detailLoading.value = false;
  }
}

async function markEmailRead(message) {
  await api.patch(`/api/inbound-email/${message.id}/read`);
  emailMessages.value = emailMessages.value.map((item) =>
    item.id === message.id ? { ...item, read_at: new Date().toISOString() } : item
  );
  if (selectedMessage.value?.id === message.id) {
    selectedMessage.value = { ...selectedMessage.value, read_at: new Date().toISOString() };
  }
}

function openLinkDialog(kind) {
  if (!selectedMessage.value) return;
  if (kind === 'customer') {
    linkForm.value.customer_id = selectedMessage.value.customer_id || '';
    showLinkCustomer.value = true;
  } else {
    linkForm.value.job_id = selectedMessage.value.job_id || '';
    showLinkJob.value = true;
  }
}

async function submitLink(kind) {
  if (!selectedMessage.value) return;
  const payload = {};
  if (kind === 'customer') {
    if (!linkForm.value.customer_id.trim()) return;
    payload.customer_id = linkForm.value.customer_id.trim();
  } else {
    if (!linkForm.value.job_id.trim()) return;
    payload.job_id = linkForm.value.job_id.trim();
  }

  linking.value = true;
  try {
    const endpoint = `/api/inbound-${selectedMessage.value.type}/${selectedMessage.value.id}/link`;
    const response = await api.post(endpoint, payload, { successMessage: 'Link saved' });
    const updated = { ...response, type: selectedMessage.value.type };
    selectedMessage.value = updated;
    refreshListEntry(updated);
    if (kind === 'customer') {
      showLinkCustomer.value = false;
      linkForm.value.customer_id = '';
    } else {
      showLinkJob.value = false;
      linkForm.value.job_id = '';
    }
  } finally {
    linking.value = false;
  }
}

function refreshListEntry(updated) {
  if (updated.type === 'sms') {
    smsMessages.value = smsMessages.value.map((item) => (item.id === updated.id ? updated : item));
  } else {
    emailMessages.value = emailMessages.value.map((item) => (item.id === updated.id ? updated : item));
  }
}
</script>

<style scoped>
.inbound-view {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.split-pane {
  display: grid;
  grid-template-columns: minmax(320px, 1fr) minmax(320px, 1fr);
  gap: var(--space-4);
}
.list-pane {
  border: 1px solid var(--border-strong);
  border-radius: 0.75rem;
  background: var(--surface-card);
  padding: var(--space-0);
}
.detail-pane {
  border: 1px solid var(--border-strong);
  border-radius: 0.75rem;
  background: var(--surface-card);
  padding: var(--space-4);
  min-height: 420px;
}
.list-filter {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
}
.list-tabs .p-tabview-panels {
  background: transparent;
  border: none;
  padding: 0;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: var(--space-5);
}
.empty-state,
.empty-detail {
  text-align: center;
  color: var(--text-muted);
  padding: var(--space-5) 0;
}
.empty-state i,
.empty-detail i {
  font-size: 2rem;
  margin-bottom: var(--space-2);
}
.detail-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.detail-header {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
}
.detail-title {
  font-size: 1.25rem;
  font-weight: 600;
}
.detail-subtitle {
  display: flex;
  gap: var(--space-2);
  color: var(--text-muted);
  margin: 0.25rem 0;
}
.detail-date {
  font-weight: 500;
}
.detail-tags,
.link-status {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-2);
}
.link-status :deep(.p-tag) {
  font-size: 0.85rem;
}
.detail-actions {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.detail-body {
  white-space: pre-wrap;
  background: var(--surface-ground);
  padding: var(--space-3);
  border-radius: 0.5rem;
  min-height: 220px;
}
.link-form .form-field {
  margin-bottom: var(--space-3);
}
.link-form label {
  font-weight: 600;
  display: block;
  margin-bottom: var(--space-2);
}
.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
.selected-row {
  border-left: 4px solid var(--interactive-primary);
}
@media (max-width: 1100px) {
  .split-pane {
    grid-template-columns: 1fr;
  }
  .detail-pane {
    min-height: auto;
  }
}
</style>

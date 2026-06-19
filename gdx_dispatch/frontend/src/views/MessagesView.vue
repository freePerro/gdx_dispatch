<template>
    <section class="messages-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">
            Messages
            <Badge v-if="unreadCount > 0" :value="unreadCount" severity="danger" class="unread-badge" />
          </h2>
        </template>
        <template #end>
          <div class="toolbar-actions">
            <Button label="Mark all read" icon="pi pi-check" class="p-button-text" size="small"
              :disabled="unreadCount === 0" :loading="markAllLoading" @click="markAllRead" />
            <Button label="Compose" icon="pi pi-envelope" severity="primary" @click="openCompose" />
          </div>
        </template>
      </Toolbar>

      <Tabs v-model:value="activeTab" class="message-tabs">
        <TabList>
          <Tab value="inbox">Inbox</Tab>
          <Tab value="sent">Sent</Tab>
        </TabList>
        <TabPanels>
        <TabPanel value="inbox">
          <div class="tab-content">
            <div v-if="loadingInbox" class="spinner-wrap"><ProgressSpinner /></div>
            <DataTable
        class="clickable-rows inboxRowClass"
      responsiveLayout="scroll"
              v-else
              :value="inboxMessages"
              row-key="id"
              paginator
              :rows="10"
              striped-rows
              
              @row-click="handleRowClick"
              :row-
            >
              <Column header="Status" style="width:120px">
                <template #body="{ data }">
                  <Tag :value="data.read_at ? 'Read' : 'Unread'" :severity="data.read_at ? 'success' : 'danger'" />
                </template>
              </Column>
              <Column header="From" :body="fromCell" />
              <Column header="Subject" field="subject" />
              <Column header="Preview" field="preview" />
              <Column header="Received" :body="formatDateCell" />
            </DataTable>
            <div v-if="!loadingInbox && !inboxMessages.length" class="empty-state">
              <i class="pi pi-inbox" />
              <p>No new messages.</p>
            </div>
          </div>
        </TabPanel>

        <TabPanel value="sent">
          <div class="tab-content">
            <div v-if="loadingSent" class="spinner-wrap"><ProgressSpinner /></div>
            <DataTable
        class="clickable-rows sentRowClass"
      responsiveLayout="scroll"
              v-else
              :value="sentMessages"
              row-key="id"
              paginator
              :rows="10"
              striped-rows
              
              @row-click="handleRowClick"
              :row-
            >
              <Column header="Recipients" :body="sentRecipients" />
              <Column header="Subject" field="subject" />
              <Column header="Preview" field="preview" />
              <Column header="Sent" :body="formatDateCell" />
              <Column header="Actions" style="width:120px">
                <template #body="{ data }">
                  <Button icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click.stop="deleteMessage(data)" />
                </template>
              </Column>
            </DataTable>
            <div v-if="!loadingSent && !sentMessages.length" class="empty-state">
              <i class="pi pi-paper-plane" />
              <p>You have not sent any messages.</p>
            </div>
          </div>
        </TabPanel>
        </TabPanels>
      </Tabs>

      <div v-if="selectedMessage" class="message-detail card">
        <div class="detail-header">
          <div>
            <div class="detail-title">{{ selectedMessage.subject || '(No subject)' }}</div>
            <div class="detail-subtitle">
              <span>
                <strong>From:</strong>
                {{ selectedMessage.sender_name || selectedMessage.sender_id || 'System' }}
              </span>
              <span class="detail-date">• {{ formatDate(selectedMessage.created_at) }}</span>
            </div>
            <p v-if="activeTab === 'sent'" class="detail-subtitle">
              <strong>To:</strong> {{ formatRecipients(selectedMessage) }}
            </p>
          </div>
          <div class="detail-actions">
            <Button v-if="activeTab === 'inbox' && !selectedMessage.read_at" label="Mark as read" icon="pi pi-check" text size="small"
              @click="markSelectedRead" />
            <Button v-if="activeTab === 'sent'" label="Delete" icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small"
              @click="deleteMessage(selectedMessage)" />
          </div>
        </div>
        <Tag class="detail-tag" :value="activeTab === 'inbox' && !selectedMessage.read_at ? 'Unread' : 'Read'" :severity="activeTab === 'inbox' && !selectedMessage.read_at ? 'danger' : 'success'" />
        <pre class="message-body">{{ selectedMessage.body }}</pre>
      </div>

      <Dialog v-model:visible="showComposeDialog" header="Compose Message" :style="{ width: '600px' }" modal>
        <form class="compose-form" @submit.prevent="sendMessage">
          <div class="form-field">
            <label>Recipients</label>
            <MultiSelect
              v-model="composeForm.recipient_ids"
              :options="recipientOptions"
              option-label="label"
              option-value="value"
              placeholder="Add recipients"
              filter
              :disabled="loadingStaff"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Subject</label>
            <InputText v-model="composeForm.subject" class="w-full" placeholder="Subject" />
          </div>
          <div class="form-field">
            <label>Body</label>
            <Textarea v-model="composeForm.body" rows="6" class="w-full" />
          </div>
          <div class="form-actions">
            <Button label="Cancel" text @click="closeCompose" />
            <Button icon="pi pi-paper-plane" severity="primary" label="Send" type="submit" :loading="sending" :disabled="!canSend" />
          </div>
        </form>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useAuthStore } from '../stores/auth';
import { useApiWithToast } from '../composables/useApiWithToast';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import MultiSelect from 'primevue/multiselect';
import ProgressSpinner from 'primevue/progressspinner';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import Toolbar from 'primevue/toolbar';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();
const auth = useAuthStore();

const activeTab = ref('inbox');
const inboxMessages = ref([]);
const sentMessages = ref([]);
const unreadCount = ref(0);
const loadingInbox = ref(true);
const loadingSent = ref(true);
const loadingStaff = ref(true);
const markAllLoading = ref(false);
const showComposeDialog = ref(false);
const selectedMessage = ref(null);
const sending = ref(false);
const staffOptions = ref([]);

const composeForm = ref({
  recipient_ids: [],
  subject: '',
  body: '',
});

const recipientOptions = computed(() => {
  const list = (staffOptions.value || [])
    .map((item) => ({ value: item.value, label: item.label }))
    .filter(Boolean);
  const currentId = auth.user?.user_id || auth.user?.sub;
  if (currentId && !list.find((opt) => opt.value === currentId)) {
    list.unshift({
      value: currentId,
      label: auth.user?.name || auth.user?.username || 'You',
    });
  }
  return list;
});

const canSend = computed(() => composeForm.value.recipient_ids.length > 0 && composeForm.value.body.trim().length > 0);

const inboxRowClass = (row) => (row.read_at ? '' : 'unread-row');
const sentRowClass = () => '';
const fromCell = (row) => row?.sender_name || row?.sender_id || 'System';
const formatDateCell = (row) => formatDate(row?.created_at);

onMounted(() => {
  loadInbox();
  loadSent();
  loadUnreadCount();
  loadStaff();
});

watch(activeTab, () => {
  selectedMessage.value = null;
});

async function loadInbox() {
  loadingInbox.value = true;
  try {
    const data = await api.get('/api/messages');
    inboxMessages.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loadingInbox.value = false;
  }
}

async function loadSent() {
  loadingSent.value = true;
  try {
    const data = await api.get('/api/messages/sent');
    sentMessages.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loadingSent.value = false;
  }
}

async function loadUnreadCount() {
  try {
    const data = await api.get('/api/messages/unread_count');
    unreadCount.value = data?.count || 0;
  } catch {
    unreadCount.value = 0;
  }
}

async function loadStaff() {
  loadingStaff.value = true;
  try {
    const data = await api.get('/api/users/staff');
    const staff = Array.isArray(data?.data) ? data.data : [];
    staffOptions.value = staff.map((user) => ({
      value: user.id,
      label: `${user.name || user.id}${user.role ? ` · ${user.role}` : ''}`,
    }));
  } finally {
    loadingStaff.value = false;
  }
}

async function markAllRead() {
  if (markAllLoading.value) return;
  markAllLoading.value = true;
  try {
    await api.post('/api/messages/mark-all-read');
    const now = new Date().toISOString();
    inboxMessages.value = inboxMessages.value.map((msg) => ({ ...msg, read_at: msg.read_at || now }));
    if (selectedMessage.value && !selectedMessage.value.read_at) {
      selectedMessage.value = { ...selectedMessage.value, read_at: now };
    }
    await loadUnreadCount();
  } finally {
    markAllLoading.value = false;
  }
}

async function handleRowClick(event) {
  const row = event?.data;
  if (!row) return;
  selectedMessage.value = { ...row };
  if (activeTab.value === 'inbox' && !row.read_at) {
    await markMessageRead(row);
  }
}

async function markMessageRead(message) {
  try {
    await api.patch(`/api/messages/${message.id}/read`);
    const readAt = new Date().toISOString();
    inboxMessages.value = inboxMessages.value.map((item) =>
      item.id === message.id ? { ...item, read_at: readAt } : item
    );
    if (selectedMessage.value?.id === message.id) {
      selectedMessage.value = { ...selectedMessage.value, read_at: readAt };
    }
    await loadUnreadCount();
  } catch (err) {
    // error handled by toast
  }
}

async function markSelectedRead() {
  if (!selectedMessage.value || selectedMessage.value.read_at) return;
  await markMessageRead(selectedMessage.value);
}

async function deleteMessage(message) {
  if (!(await confirmAsync({ header: 'Confirm', message: 'Delete this message?' }))) return;
  try {
    await api.del(`/api/messages/${message.id}`, { successMessage: 'Message deleted' });
    sentMessages.value = sentMessages.value.filter((item) => item.id !== message.id);
    if (selectedMessage.value?.id === message.id) {
      selectedMessage.value = null;
    }
  } catch {
    // handled by toast
  }
}

function sentRecipients(message) {
  return message.recipients?.map((r) => r.recipient_id).join(', ') || '—';
}

function formatRecipients(message) {
  if (!message.recipients?.length) {
    return '—';
  }
  return message.recipients.map((r) => r.recipient_id).join(', ');
}

function formatDate(data) {
  const value = typeof data === 'string' ? data : data?.created_at;
  if (!value) return '—';
  return new Date(value).toLocaleString();
}

function openCompose() {
  showComposeDialog.value = true;
}

function closeCompose() {
  showComposeDialog.value = false;
}

async function sendMessage() {
  if (!canSend.value) return;
  sending.value = true;
  try {
    const payload = {
      subject: composeForm.value.subject || null,
      body: composeForm.value.body,
      recipient_ids: composeForm.value.recipient_ids,
    };
    const sent = await api.post('/api/messages', payload, { successMessage: 'Message sent' });
    sentMessages.value = [sent, ...sentMessages.value];
    composeForm.value = { recipient_ids: [], subject: '', body: '' };
    showComposeDialog.value = false;
  } finally {
    sending.value = false;
  }
}
</script>

<style scoped>
.messages-view {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.page-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
}
.unread-badge {
  font-size: 0.75rem;
}
.toolbar-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.message-tabs .p-tabview-panels {
  border: 1px solid var(--border-strong);
  border-top: none;
  border-radius: 0 0 0.75rem 0.75rem;
  padding: var(--space-4);
  background: var(--surface-card);
}
.tab-content {
  min-height: 220px;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: var(--space-6) 0;
}
.empty-state {
  text-align: center;
  padding: var(--space-6) 0;
  color: var(--text-muted);
}
.empty-state i {
  font-size: 2rem;
}
.message-detail {
  padding: var(--space-4);
  border: 1px solid var(--border-strong);
  border-radius: 0.75rem;
  background: var(--surface-card);
}
.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-3);
}
.detail-title {
  font-size: 1.25rem;
  font-weight: 600;
}
.detail-subtitle {
  margin: 0.25rem 0;
  font-size: 0.9rem;
  color: var(--text-muted);
  display: flex;
  gap: var(--space-2);
}
.detail-date {
  font-weight: 500;
}
.detail-actions {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.detail-tag {
  margin-top: var(--space-2);
}
.message-body {
  margin-top: var(--space-3);
  white-space: pre-wrap;
  font-family: var(--font-family);
}
.compose-form .form-field {
  margin-bottom: var(--space-3);
}
.compose-form label {
  font-weight: 500;
  display: block;
  margin-bottom: var(--space-2);
}
.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
.unread-row {
  background: var(--surface-highlight);
}
</style>

<template>
    <section class="comms-view view-card">
      <header class="page-header">
        <div class="title-wrap">
          <h2 class="page-title">Communications</h2>
          <Badge v-if="totalUnreadCount > 0" :value="totalUnreadCount" severity="danger" data-testid="total-unread" />
        </div>
        <Button
          label="Compose"
          icon="pi pi-pencil" aria-label="Edit"
          data-testid="compose-btn"
          @click="openCompose"
        />
      </header>

      <div class="toolbar-row">
        <InputText
          id="comms-search"
          name="comms-search"
          v-model="searchQuery"
          placeholder="Search across all messages..."
          class="search-input"
          data-testid="comms-search"
        />
      </div>

      <Tabs v-model:value="activeChannelTabValue" class="channel-tabs" data-testid="channel-tabs">
        <TabList>
          <Tab value="all">
            <div class="tab-header">
              <span>All</span>
              <Badge v-if="totalUnreadCount > 0" :value="totalUnreadCount" severity="danger" />
            </div>
          </Tab>
          <Tab value="sms">
            <div class="tab-header">
              <span>SMS</span>
              <Badge v-if="smsUnreadCount > 0" :value="smsUnreadCount" severity="danger" />
            </div>
          </Tab>
          <Tab value="email">
            <div class="tab-header">
              <span>Email</span>
              <Badge v-if="emailUnreadCount > 0" :value="emailUnreadCount" severity="danger" />
            </div>
          </Tab>
        </TabList>
      </Tabs>

      <div class="comms-layout">
        <Card class="thread-list-panel" data-testid="thread-list">
          <template #title>
            <div class="panel-title-row">
              <span>Threads</span>
              <small v-if="searchLoading" class="muted">Searching messages...</small>
            </div>
          </template>
          <template #content>
            <div v-if="threadsLoading" class="loading-area">
              <i class="pi pi-spin pi-spinner"></i>
              <span>Loading threads...</span>
            </div>
            <div v-else-if="threadsError" class="empty-area">
              <p>{{ threadsError }}</p>
              <Button label="Retry" size="small" @click="fetchThreads()" />
            </div>
            <div v-else-if="filteredThreads.length === 0" class="empty-area">
              <p v-if="searchQuery.trim()">No conversations match your search.</p>
              <p v-else>No conversations found.</p>
            </div>
            <DataTable
        class="clickable-rows thread-table"
      responsiveLayout="scroll"
              v-else
              :value="filteredThreads"
              dataKey="id"
              rowGroupMode="subheader"
              groupRowsBy="customerName"
              sortField="customerName"
              :sortOrder="1"
              scrollable
              scrollHeight="520px"
              
              @row-click="onThreadRowClick"
            >
              <template #groupheader="slotProps">
                <div class="group-header">
                  <span class="group-name">{{ slotProps.data.customerName }}</span>
                  <Badge
                    v-if="customerUnreadCount(slotProps.data.customerName) > 0"
                    :value="customerUnreadCount(slotProps.data.customerName)"
                    severity="danger"
                  />
                </div>
              </template>
              <Column field="channel" header="Channel" class="channel-col">
                <template #body="slotProps">
                  <span :class="['channel-pill', slotProps.data.channel]">{{ channelLabel(slotProps.data.channel) }}</span>
                </template>
              </Column>
              <Column field="preview" header="Conversation">
                <template #body="slotProps">
                  <div class="thread-cell" :class="{ active: selectedThreadId === slotProps.data.id }">
                    <div class="thread-top">
                      <span class="thread-subject">{{ slotProps.data.subject || '(No subject)' }}</span>
                      <Badge v-if="slotProps.data.unreadCount > 0" :value="slotProps.data.unreadCount" severity="danger" />
                    </div>
                    <p class="thread-preview">{{ slotProps.data.preview || 'No preview available' }}</p>
                  </div>
                </template>
              </Column>
              <Column field="lastMessageAt" header="Last" class="time-col">
                <template #body="slotProps">
                  <span class="thread-time">{{ formatRelativeTime(slotProps.data.lastMessageAt) }}</span>
                </template>
              </Column>
            </DataTable>
          </template>
        </Card>

        <Card class="thread-detail-panel" data-testid="thread-detail">
          <template #title>
            <div v-if="selectedThread" class="detail-title">
              <div class="detail-title-main">
                <span class="detail-customer">{{ selectedThread.customerName }}</span>
                <span :class="['channel-pill', selectedThread.channel]">{{ channelLabel(selectedThread.channel) }}</span>
              </div>
              <small class="detail-contact">{{ selectedThread.to || selectedThread.from || 'No address/phone provided' }}</small>
            </div>
            <div v-else>Select a conversation</div>
          </template>
          <template #content>
            <div v-if="!selectedThread" class="empty-area detail-empty">
              <p>Choose a thread on the left to view messages.</p>
            </div>

            <div v-else class="detail-content">
              <div v-if="messagesLoading" class="loading-area">
                <i class="pi pi-spin pi-spinner"></i>
                <span>Loading messages...</span>
              </div>
              <div v-else-if="messagesError" class="empty-area">
                <p>{{ messagesError }}</p>
                <Button label="Retry" size="small" @click="loadSelectedThreadMessages(true)" />
              </div>
              <div v-else-if="selectedThreadMessages.length === 0" class="empty-area">
                <p>No messages in this thread yet.</p>
              </div>

              <div v-else class="messages-panel" data-testid="message-list">
                <article
                  v-for="message in selectedThreadMessages"
                  :key="message.id"
                  :class="['message-item', message.direction === 'out' ? 'outbound' : 'inbound']"
                >
                  <div class="message-head">
                    <strong>{{ message.direction === 'out' ? 'You' : selectedThread.customerName }}</strong>
                    <span>{{ formatDateTime(message.sentAt || message.createdAt) }}</span>
                  </div>
                  <p v-if="message.subject" class="message-subject">{{ message.subject }}</p>
                  <p class="message-body">{{ message.body || message.message || '' }}</p>
                </article>
              </div>

              <div class="reply-box" data-testid="reply-area">
                <Textarea
                  v-model="replyBody"
                  rows="3"
                  :autoResize="true"
                  class="reply-textarea"
                  placeholder="Type a reply..."
                  data-testid="reply-textarea"
                />
                <div class="reply-actions">
                  <Button
                    label="Send Reply"
                    icon="pi pi-send"
                    :loading="replySending"
                    :disabled="!selectedThread || !replyBody.trim()"
                    data-testid="send-reply-btn"
                    @click="sendReply"
                  />
                </div>
              </div>
            </div>
          </template>
        </Card>
      </div>

      <Dialog
        v-model:visible="composeDialogVisible"
        header="Compose Message"
        modal
        class="compose-dialog"
        data-testid="compose-dialog"
      >
        <div class="compose-form">
          <div class="form-field">
            <label for="compose-customer">Customer (auto-fills contact)</label>
            <Select id="compose-customer" v-model="composeCustomerId" :options="customerOptions"
              optionLabel="label" optionValue="value" filter showClear placeholder="Select customer..."
              data-testid="compose-customer" @change="onCustomerSelect" />
          </div>

          <Tabs v-model:value="composeChannelValue">
            <TabList>
              <Tab value="sms">SMS</Tab>
              <Tab value="email">Email</Tab>
            </TabList>
          </Tabs>

          <div v-if="composeChannel === 'sms'" class="form-field">
            <label for="compose-phone">Phone</label>
            <InputText id="compose-phone" v-model="composePhone" placeholder="+1 555 555 1234" data-testid="compose-phone" />
          </div>
          <div v-else>
            <div class="form-field">
              <label for="compose-to">To</label>
              <InputText id="compose-to" v-model="composeTo" placeholder="customer@example.com" data-testid="compose-to" />
            </div>
            <div class="form-field">
              <label for="compose-subject">Subject</label>
              <InputText id="compose-subject" v-model="composeSubject" placeholder="Message subject" data-testid="compose-subject" />
            </div>
          </div>

          <div class="form-field">
            <label for="compose-template">Quick Template</label>
            <Select id="compose-template" :options="messageTemplates" optionLabel="label" optionValue="value"
              placeholder="Select template..." data-testid="compose-template" @change="applyTemplate" />
          </div>

          <div class="form-field">
            <label for="compose-body">Body</label>
            <Textarea
              id="compose-body"
              v-model="composeBody"
              rows="5"
              :autoResize="true"
              placeholder="Write your message..."
              data-testid="compose-body"
            />
          </div>

          <small v-if="composeError" class="inline-error">{{ composeError }}</small>
        </div>

        <template #footer>
          <Button label="Cancel" severity="secondary" @click="composeDialogVisible = false" />
          <Button label="Send" icon="pi pi-send" :loading="composeSending" @click="sendCompose" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import Select from 'primevue/select';
import InputText from 'primevue/inputtext';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Textarea from 'primevue/textarea';

const api = useApiWithToast();

const CHANNELS = ['all', 'sms', 'email'];

const threads = ref([]);
const threadsLoading = ref(false);
const threadsError = ref('');

const selectedThreadId = ref(null);
const messageCache = ref({});
const messagesLoading = ref(false);
const messagesError = ref('');

const searchQuery = ref('');
const searchLoading = ref(false);
const searchMatches = ref({});
let searchToken = 0;
let searchDebounceHandle = null;

// v4 Tabs use string values instead of numeric index
const activeChannelTabValue = ref('all');
const activeChannelTab = computed(() => ['all', 'sms', 'email'].indexOf(activeChannelTabValue.value));

const replyBody = ref('');
const replySending = ref(false);

const composeDialogVisible = ref(false);
const composeChannelValue = ref('sms');
const composeChannelTab = computed(() => composeChannelValue.value === 'sms' ? 0 : 1);
const composePhone = ref('');
const composeTo = ref('');
const composeSubject = ref('');
const composeBody = ref('');
const composeSending = ref(false);
const composeError = ref('');
const composeCustomerId = ref(null);
const customers = ref([]);

const customerOptions = computed(() =>
  customers.value.map((c) => ({ label: c.name || c.full_name || c.email, value: c.id, data: c }))
);

function onCustomerSelect(event) {
  const cust = customers.value.find((c) => c.id === event.value);
  if (cust) {
    composePhone.value = cust.phone || cust.mobile_phone || '';
    composeTo.value = cust.email || '';
  }
}

const messageTemplates = [
  { label: 'Job Completed', value: 'job_complete' },
  { label: 'On My Way', value: 'on_way' },
  { label: 'Appointment Reminder', value: 'reminder' },
  { label: 'Invoice Sent', value: 'invoice_sent' },
  { label: 'Payment Thank You', value: 'payment_thanks' },
  { label: 'Review Request', value: 'review_request' },
];

const TEMPLATE_BODIES = {
  job_complete: 'Hi {name}, your service has been completed. Thank you for choosing us! If you have any questions, just reply to this message.',
  on_way: 'Hi {name}, your technician is on the way and should arrive in about 20 minutes. Please have the area accessible. Thanks!',
  reminder: "Hi {name}, this is a reminder about your upcoming service appointment. We'll see you soon!",
  invoice_sent: 'Hi {name}, your invoice is ready. You can view and pay it online. Thank you for your business!',
  payment_thanks: 'Hi {name}, thanks for your payment! We appreciate your business.',
  review_request: 'Hi {name}, we hope you were happy with our service. Would you mind leaving us a quick review? Thank you!',
};

function applyTemplate(event) {
  const tpl = TEMPLATE_BODIES[event.value];
  if (!tpl) return;
  const customerName = customers.value.find((c) => c.id === composeCustomerId.value)?.name?.split(' ')[0] || 'there';
  composeBody.value = tpl.replace('{name}', customerName);
  if (event.value === 'job_complete') composeSubject.value = 'Your service is complete';
  if (event.value === 'reminder') composeSubject.value = 'Appointment reminder';
  if (event.value === 'invoice_sent') composeSubject.value = 'Your invoice is ready';
}

async function loadCustomers() {
  try {
    const result = await api.get('/api/customers?per_page=500');
    const payload = result?.data || result;
    customers.value = Array.isArray(payload) ? payload : payload?.items || [];
  } catch {
    customers.value = [];
  }
}

let pollHandle = null;

const selectedChannel = computed(() => CHANNELS[activeChannelTab.value] || 'all');
const composeChannel = computed(() => (composeChannelTab.value === 0 ? 'sms' : 'email'));

const normalizedThreads = computed(() =>
  threads.value
    .map((thread) => normalizeThread(thread))
    .sort((a, b) => toTime(b.lastMessageAt) - toTime(a.lastMessageAt)),
);

const selectedThread = computed(() => normalizedThreads.value.find((thread) => thread.id === selectedThreadId.value) || null);

const selectedThreadMessages = computed(() => {
  const threadId = selectedThreadId.value;
  if (!threadId) return [];
  const messages = messageCache.value[threadId] || [];
  return [...messages].sort((a, b) => toTime(a.sentAt || a.createdAt) - toTime(b.sentAt || b.createdAt));
});

const filteredThreads = computed(() => {
  let rows = normalizedThreads.value;

  if (selectedChannel.value !== 'all') {
    rows = rows.filter((thread) => thread.channel === selectedChannel.value);
  }

  const query = searchQuery.value.trim().toLowerCase();
  if (!query) return rows;

  return rows.filter((thread) => {
    const inMeta = [
      thread.customerName,
      thread.subject,
      thread.preview,
      thread.to,
      thread.from,
    ].join(' ').toLowerCase().includes(query);

    return inMeta || !!searchMatches.value[thread.id];
  });
});

const totalUnreadCount = computed(() => normalizedThreads.value.reduce((sum, thread) => sum + thread.unreadCount, 0));
const smsUnreadCount = computed(() =>
  normalizedThreads.value.filter((thread) => thread.channel === 'sms').reduce((sum, thread) => sum + thread.unreadCount, 0),
);
const emailUnreadCount = computed(() =>
  normalizedThreads.value.filter((thread) => thread.channel === 'email').reduce((sum, thread) => sum + thread.unreadCount, 0),
);

watch(searchQuery, () => {
  if (searchDebounceHandle) clearTimeout(searchDebounceHandle);
  searchDebounceHandle = setTimeout(() => {
    runSearch();
  }, 350);
});

watch(activeChannelTab, () => {
  if (selectedThread.value && selectedChannel.value !== 'all' && selectedThread.value.channel !== selectedChannel.value) {
    selectedThreadId.value = null;
  }

  if (!selectedThreadId.value && filteredThreads.value.length > 0) {
    selectedThreadId.value = filteredThreads.value[0].id;
    loadSelectedThreadMessages(false);
  }
});

watch(composeChannelTab, () => {
  composeError.value = '';
});

function normalizeThread(thread) {
  return {
    ...thread,
    id: thread.id,
    customerName: thread.customer_name || thread.customerName || thread.customer?.name || 'Unknown Customer',
    unreadCount: Number(thread.unread_count || thread.unreadCount || 0),
    channel: (thread.channel || 'email').toLowerCase(),
    subject: thread.subject || thread.last_subject || '',
    preview: thread.last_message || thread.preview || thread.snippet || '',
    lastMessageAt: thread.last_message_at || thread.updated_at || thread.created_at || null,
    to: thread.to || thread.recipient || '',
    from: thread.from || thread.sender || '',
  };
}

function normalizeMessages(payload) {
  const rows = extractList(payload);
  return rows.map((msg) => ({
    ...msg,
    id: msg.id || `${msg.created_at || msg.sent_at}-${msg.direction || 'in'}`,
    body: msg.body || msg.message || msg.text || '',
    subject: msg.subject || '',
    direction: (msg.direction || 'in').toLowerCase(),
    createdAt: msg.created_at || msg.createdAt || null,
    sentAt: msg.sent_at || msg.sentAt || null,
  }));
}

function extractList(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.data)) return payload.data;
  if (Array.isArray(payload?.items)) return payload.items;
  if (Array.isArray(payload?.data?.items)) return payload.data.items;
  return [];
}

function toTime(value) {
  if (!value) return 0;
  const ts = new Date(value).getTime();
  return Number.isNaN(ts) ? 0 : ts;
}

function formatDateTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString();
}

function formatRelativeTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';

  const diffMs = Date.now() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);

  if (diffMin < 1) return 'now';
  if (diffMin < 60) return `${diffMin}m`;
  const diffHours = Math.floor(diffMin / 60);
  if (diffHours < 24) return `${diffHours}h`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}d`;
  return date.toLocaleDateString();
}

function channelLabel(channel) {
  return channel === 'sms' ? 'SMS' : 'Email';
}

function customerUnreadCount(customerName) {
  return filteredThreads.value
    .filter((thread) => thread.customerName === customerName)
    .reduce((sum, thread) => sum + thread.unreadCount, 0);
}

function onThreadRowClick(event) {
  const thread = event.data;
  if (!thread?.id) return;
  selectedThreadId.value = thread.id;
  loadSelectedThreadMessages(false);
}

async function fetchThreads(options = {}) {
  const { silent = false } = options;

  if (!silent) {
    threadsLoading.value = true;
    threadsError.value = '';
  }

  try {
    const response = await api.get('/api/communications/threads');
    threads.value = extractList(response);

    if (selectedThreadId.value && !threads.value.some((thread) => thread.id === selectedThreadId.value)) {
      selectedThreadId.value = null;
    }

    if (!selectedThreadId.value && filteredThreads.value.length > 0) {
      selectedThreadId.value = filteredThreads.value[0].id;
      await loadSelectedThreadMessages(false);
    }
  } catch (error) {
    if (!silent) {
      threads.value = [];
      threadsError.value = 'Unable to load communications threads.';
    }
  } finally {
    if (!silent) threadsLoading.value = false;
  }
}

async function fetchThreadMessages(threadId, options = {}) {
  const { force = false, silent = false } = options;

  if (!threadId) return [];
  if (!force && messageCache.value[threadId]) return messageCache.value[threadId];

  if (!silent) {
    messagesLoading.value = true;
    messagesError.value = '';
  }

  try {
    const response = await api.get(`/api/communications/threads/${threadId}/messages`);
    const messages = normalizeMessages(response);
    messageCache.value = {
      ...messageCache.value,
      [threadId]: messages,
    };
    return messages;
  } catch (error) {
    if (!silent && selectedThreadId.value === threadId) {
      messagesError.value = 'Unable to load messages for this thread.';
    }
    return [];
  } finally {
    if (!silent) messagesLoading.value = false;
  }
}

async function loadSelectedThreadMessages(force = false) {
  if (!selectedThreadId.value) return;
  await fetchThreadMessages(selectedThreadId.value, { force, silent: false });
}

async function runSearch() {
  const query = searchQuery.value.trim().toLowerCase();
  const currentToken = ++searchToken;

  if (!query) {
    searchMatches.value = {};
    searchLoading.value = false;
    return;
  }

  const rows = normalizedThreads.value.filter((thread) => selectedChannel.value === 'all' || thread.channel === selectedChannel.value);

  searchLoading.value = true;
  const matches = {};

  await Promise.all(
    rows.map(async (thread) => {
      const inMeta = [thread.customerName, thread.subject, thread.preview, thread.to, thread.from]
        .join(' ')
        .toLowerCase()
        .includes(query);

      if (inMeta) {
        matches[thread.id] = true;
        return;
      }

      const messages = await fetchThreadMessages(thread.id, { force: false, silent: true });
      const inMessages = messages.some((message) =>
        [message.subject, message.body, message.message, message.from, message.to]
          .join(' ')
          .toLowerCase()
          .includes(query),
      );

      if (inMessages) {
        matches[thread.id] = true;
      }
    }),
  );

  if (currentToken === searchToken) {
    searchMatches.value = matches;
    searchLoading.value = false;
  }
}

async function sendReply() {
  if (!selectedThread.value || !replyBody.value.trim()) return;

  replySending.value = true;
  try {
    await api.post('/api/communications/send', {
      thread_id: selectedThread.value.id,
      channel: selectedThread.value.channel,
      body: replyBody.value.trim(),
      message: replyBody.value.trim(),
      to: selectedThread.value.to || undefined,
    }, { successMessage: 'Reply sent' });

    replyBody.value = '';
    await fetchThreads({ silent: true });
    await loadSelectedThreadMessages(true);
  } finally {
    replySending.value = false;
  }
}

function resetComposeForm() {
  composePhone.value = '';
  composeTo.value = '';
  composeSubject.value = '';
  composeBody.value = '';
  composeError.value = '';
}

function openCompose() {
  resetComposeForm();
  composeChannelValue.value = 'sms';
  composeDialogVisible.value = true;
}

async function sendCompose() {
  composeError.value = '';

  if (!composeBody.value.trim()) {
    composeError.value = 'Message body is required.';
    return;
  }

  if (composeChannel.value === 'sms' && !composePhone.value.trim()) {
    composeError.value = 'Phone is required for SMS.';
    return;
  }

  if (composeChannel.value === 'email') {
    if (!composeTo.value.trim()) {
      composeError.value = 'Recipient email is required.';
      return;
    }
    if (!composeSubject.value.trim()) {
      composeError.value = 'Subject is required for email.';
      return;
    }
  }

  composeSending.value = true;
  try {
    const payload = {
      channel: composeChannel.value,
      body: composeBody.value.trim(),
      message: composeBody.value.trim(),
      to: composeChannel.value === 'email' ? composeTo.value.trim() : undefined,
      phone: composeChannel.value === 'sms' ? composePhone.value.trim() : undefined,
      subject: composeChannel.value === 'email' ? composeSubject.value.trim() : undefined,
    };

    await api.post('/api/communications/send', payload, { successMessage: 'Message sent' });

    composeDialogVisible.value = false;
    resetComposeForm();
    await fetchThreads({ silent: true });
    await runSearch();
  } finally {
    composeSending.value = false;
  }
}

async function pollForUpdates() {
  await fetchThreads({ silent: true });
  if (selectedThreadId.value) {
    await fetchThreadMessages(selectedThreadId.value, { force: true, silent: true });
  }
  if (searchQuery.value.trim()) {
    await runSearch();
  }
}

onMounted(async () => {
  await Promise.all([fetchThreads(), loadCustomers()]);
  pollHandle = setInterval(() => {
    pollForUpdates();
  }, 30000);
});

onBeforeUnmount(() => {
  if (pollHandle) clearInterval(pollHandle);
  if (searchDebounceHandle) clearTimeout(searchDebounceHandle);
});
</script>

<style scoped>
.comms-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.title-wrap {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.page-title {
  margin: 0;
  font-size: 1.4rem;
}

.toolbar-row {
  display: flex;
  gap: 0.75rem;
}

.search-input {
  width: min(480px, 100%);
}

.channel-tabs :deep(.p-tabview-panels) {
  display: none;
}

.tab-header {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.comms-layout {
  display: grid;
  grid-template-columns: minmax(300px, 36%) minmax(0, 1fr);
  gap: 1rem;
  min-height: 560px;
}

.thread-list-panel,
.thread-detail-panel {
  height: 100%;
}

.panel-title-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.thread-table :deep(.p-datatable-tbody > tr > td) {
  cursor: pointer;
  vertical-align: top;
}

.group-header {
  width: 100%;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.group-name {
  font-weight: 600;
}

.thread-cell {
  padding: 0.1rem 0;
}

.thread-cell.active {
  border-left: 3px solid var(--p-primary-color);
  padding-left: 0.5rem;
}

.thread-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.thread-subject {
  font-weight: 600;
  font-size: 0.9rem;
}

.thread-preview {
  margin: 0.3rem 0 0;
  font-size: 0.82rem;
  color: var(--p-text-muted-color);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.channel-col,
.time-col {
  width: 110px;
}

.channel-pill {
  display: inline-block;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 600;
}

.channel-pill.sms {
  background: color-mix(in srgb, var(--p-green-500) 18%, transparent);
  color: var(--p-green-800);
}

.channel-pill.email {
  background: color-mix(in srgb, var(--p-blue-500) 18%, transparent);
  color: var(--p-blue-800);
}

.thread-time {
  color: var(--p-text-muted-color);
  font-size: 0.78rem;
}

.detail-title {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.detail-title-main {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}

.detail-customer {
  font-size: 1rem;
  font-weight: 700;
}

.detail-contact {
  color: var(--p-text-muted-color);
}

.detail-content {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.messages-panel {
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
  max-height: 420px;
  overflow-y: auto;
  padding-right: 0.25rem;
}

.message-item {
  border-radius: 10px;
  padding: 0.65rem 0.8rem;
  max-width: 85%;
}

.message-item.inbound {
  background: var(--p-content-hover-background);
  align-self: flex-start;
}

.message-item.outbound {
  background: var(--p-primary-color);
  color: var(--p-primary-contrast-color);
  align-self: flex-end;
}

.message-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.78rem;
}

.message-subject {
  margin: 0.45rem 0 0.2rem;
  font-weight: 700;
}

.message-body {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}

.reply-box {
  border-top: 1px solid var(--p-content-border-color);
  padding-top: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.reply-textarea {
  width: 100%;
}

.reply-actions {
  display: flex;
  justify-content: flex-end;
}

.compose-dialog {
  width: min(38rem, calc(100vw - 2rem));
}

.compose-form {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.form-field label {
  font-size: 0.85rem;
  font-weight: 600;
}

.loading-area,
.empty-area {
  padding: 1.5rem;
  text-align: center;
  color: var(--p-text-muted-color);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.6rem;
}

.detail-empty {
  min-height: 240px;
  justify-content: center;
}

.inline-error {
  color: var(--p-red-500);
}

.muted {
  color: var(--p-text-muted-color);
}

@media (max-width: 900px) {
  .comms-layout {
    grid-template-columns: 1fr;
    min-height: auto;
  }

  .thread-table {
    max-height: 360px;
    overflow: auto;
  }

  .messages-panel {
    max-height: 50vh;
  }

  .page-header {
    flex-wrap: wrap;
  }
}
</style>

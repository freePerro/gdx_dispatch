<template>
    <section class="winback-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Winback & Follow-ups</h2>
        </template>
        <template #end>
          <Button
            :label="activeTab === 'campaigns' ? '+ New Campaign' : '+ New Follow-up'"
            icon="pi pi-plus"
            @click="activeTab === 'campaigns' ? openCampaignDialog() : openFollowupDialog()"
          />
        </template>
      </Toolbar>

      <div v-if="statsLoading" class="spinner-wrap"><ProgressSpinner /></div>
      <div v-else class="stats-row">
        <Card class="stat-card">
          <div class="stat-label">Total Campaigns</div>
          <div class="stat-value">{{ stats.total_campaigns ?? 0 }}</div>
        </Card>
        <Card class="stat-card">
          <div class="stat-label">Active</div>
          <div class="stat-value">{{ stats.active ?? 0 }}</div>
        </Card>
        <Card class="stat-card">
          <div class="stat-label">Sent Last 30d</div>
          <div class="stat-value">{{ stats.sent_last_30d ?? 0 }}</div>
        </Card>
        <Card class="stat-card">
          <div class="stat-label">Candidates</div>
          <div class="stat-value">{{ stats.candidates_count ?? 0 }}</div>
        </Card>
      </div>

      <Tabs v-model:value="activeTab" class="top-tabs">
        <TabList>
          <Tab value="campaigns">Winback Campaigns</Tab>
          <Tab value="followups">Follow-ups</Tab>
        </TabList>
      </Tabs>

      <div v-if="activeTab === 'campaigns'" class="tab-content">
        <div v-if="campaignsLoading" class="spinner-wrap small"><ProgressSpinner /></div>
        <DataTable
      responsiveLayout="scroll"
          v-else
          :value="campaigns"
          dataKey="id"
          paginator
          :rows="10"
          striped-rows
          responsive-layout="scroll"
          class="clickable-row"
        >
          <template #empty>
            <div class="empty-state">
              <i class="pi pi-filter-slash" style="font-size:3rem; color:#64748b;"></i>
              <h3>No Winback campaigns</h3>
              <p>Use the button above to start a new re-engagement campaign.</p>
            </div>
          </template>
          <Column field="name" header="Campaign" />
          <Column field="channel" header="Channel" />
          <Column field="inactivity_months" header="Inactivity (months)" />
          <Column field="status" header="Status" style="width:160px">
            <template #body="{ data }">
              <Badge :value="data.status?.replace('_', ' ')" :severity="campaignStatusSeverity(data.status)" />
            </template>
          </Column>
          <Column field="sent_at" header="Last Sent" style="width:150px">
            <template #body="{ data }">{{ formatDate(data.sent_at) }}</template>
          </Column>
          <Column header="Actions" style="width:180px">
            <template #body="{ data }">
              <Button
                label="Send"
                icon="pi pi-paper-plane"
                severity="success"
                :disabled="data.status !== 'draft'"
                :loading="sendingCampaignId === data.id"
                @click.stop="sendCampaign(data)"
              />
            </template>
          </Column>
        </DataTable>
      </div>

      <div v-else class="tab-content">
        <div class="filter-tabs">
          <Button
            v-for="status in followupStatuses"
            :key="status"
            :label="status === 'all' ? 'All' : status.charAt(0).toUpperCase() + status.slice(1)"
            :severity="followupStatusFilter === status ? undefined : 'secondary'"
            size="small"
            @click="applyFollowupFilter(status)"
          />
        </div>

        <div class="followup-actions">
          <Button
            label="Complete Selected"
            icon="pi pi-check"
            severity="success"
            :disabled="!selectedFollowups.length"
            :loading="followupsCompleting"
            @click="bulkCompleteFollowups"
          />
        </div>

        <div v-if="followupsLoading" class="spinner-wrap small"><ProgressSpinner /></div>
        <DataTable
      responsiveLayout="scroll"
          v-else
          :value="filteredFollowups"
          dataKey="id"
          paginator
          :rows="10"
          striped-rows
          selectionMode="multiple"
          :selection="selectedFollowups"
          @selection-change="onFollowupSelectionChange"
        >
          <template #empty>
            <EmptyState
              icon="pi pi-check-square"
              title="No follow-ups"
              message="Follow-up tasks from winback campaigns will queue up here."
            />
          </template>
          <Column selectionMode="multiple" style="width:3rem" />
          <Column field="entity_type" header="Type" />
          <Column field="entity_id" header="Entity" />
          <Column field="due_date" header="Due" style="width:130px">
            <template #body="{ data }">{{ formatDate(data.due_date) }}</template>
          </Column>
          <Column field="status" header="Status" style="width:140px">
            <template #body="{ data }">
              <Badge :value="data.status" :severity="followupStatusSeverity(data.status)" />
            </template>
          </Column>
          <Column field="assigned_to" header="Assigned" />
          <Column field="note" header="Note">
            <template #body="{ data }">
              <p class="table-note">{{ data.note || '—' }}</p>
            </template>
          </Column>
        </DataTable>
      </div>

      <Dialog
        v-model:visible="showCampaignDialog"
        header="New Winback Campaign"
        modal
        :style="{ width: '520px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Campaign Name *</label>
            <input v-model="campaignForm.name" type="text" class="p-inputtext w-full" />
          </div>
          <div class="form-field">
            <label>Channel</label>
            <Select v-model="campaignForm.channel" :options="channelOptions" class="w-full" />
          </div>
          <div class="form-field">
            <label>Inactivity (months)</label>
            <input v-model.number="campaignForm.inactivity_months" type="number" min="1" class="p-inputtext w-full" />
          </div>
          <div class="form-field full-width">
            <label>Message Body *</label>
            <textarea v-model="campaignForm.body_template" rows="4" class="p-inputtextarea w-full"></textarea>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showCampaignDialog = false" />
          <Button
            label="Create Campaign"
            icon="pi pi-check"
            :loading="campaignSaving"
            :disabled="!campaignForm.name.trim() || !campaignForm.body_template.trim()"
            @click="saveCampaign"
          />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="showFollowupDialog"
        header="New Follow-up"
        modal
        :style="{ width: '520px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Entity Type</label>
            <Select v-model="followupForm.entity_type" :options="entityTypeOptions" class="w-full" />
          </div>
          <div class="form-field">
            <label>Entity ID *</label>
            <input v-model="followupForm.entity_id" type="text" class="p-inputtext w-full" />
          </div>
          <div class="form-field">
            <label>Due Date *</label>
            <DatePicker v-model="followupForm.due_date" dateFormat="yy-mm-dd" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Note</label>
            <textarea v-model="followupForm.note" rows="3" class="p-inputtextarea w-full"></textarea>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showFollowupDialog = false" />
          <Button
            label="Create Follow-up"
            icon="pi pi-check"
            severity="success"
            :loading="creatingFollowup"
            :disabled="!followupForm.entity_id.trim() || !followupForm.due_date"
            @click="saveFollowup"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import Select from 'primevue/select';
import DatePicker from 'primevue/datepicker';
import Badge from 'primevue/badge';
import ProgressSpinner from 'primevue/progressspinner';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();

const stats = ref({
  total_campaigns: 0,
  active: 0,
  sent_last_30d: 0,
  candidates_count: 0,
});
const statsLoading = ref(true);

const campaigns = ref([]);
const campaignsLoading = ref(true);
const sendingCampaignId = ref(null);
const showCampaignDialog = ref(false);
const campaignSaving = ref(false);

const followups = ref([]);
const followupsLoading = ref(true);
const followupStatusFilter = ref('all');
const selectedFollowups = ref([]);
const followupsCompleting = ref(false);
const showFollowupDialog = ref(false);
const creatingFollowup = ref(false);

const activeTab = ref('campaigns');

const followupStatuses = ['all', 'open', 'completed', 'cancelled'];
const channelOptions = [
  { label: 'Email', value: 'email' },
  { label: 'SMS', value: 'sms' },
];
const entityTypeOptions = [
  { label: 'Customer', value: 'customer' },
  { label: 'Estimate', value: 'estimate' },
  { label: 'Invoice', value: 'invoice' },
];

const campaignForm = ref({
  name: '',
  channel: 'email',
  body_template: '',
  inactivity_months: 6,
});

const followupForm = ref({
  entity_type: 'customer',
  entity_id: '',
  due_date: null,
  note: '',
});

const filteredFollowups = computed(() => {
  if (followupStatusFilter.value === 'all') {
    return followups.value;
  }
  return followups.value.filter((item) => item.status === followupStatusFilter.value);
});

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toISOString().split('T')[0];
}

function campaignStatusSeverity(status) {
  return {
    draft: 'warning',
    sent: 'info',
    active: 'success',
    cancelled: 'danger',
  }[status] || 'secondary';
}

function followupStatusSeverity(status) {
  return {
    open: 'info',
    completed: 'success',
    cancelled: 'danger',
  }[status] || 'secondary';
}

async function loadStats() {
  statsLoading.value = true;
  try {
    const data = await api.get('/api/winback/stats');
    stats.value = {
      total_campaigns: data?.total_campaigns ?? 0,
      active: data?.active ?? 0,
      sent_last_30d: data?.sent_last_30d ?? 0,
      candidates_count: data?.candidates_count ?? 0,
    };
  } finally {
    statsLoading.value = false;
  }
}

async function loadCampaigns() {
  campaignsLoading.value = true;
  try {
    const data = await api.get('/api/winback/campaigns');
    campaigns.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    campaignsLoading.value = false;
  }
}

async function loadFollowups() {
  followupsLoading.value = true;
  try {
    const query = followupStatusFilter.value === 'all' ? '' : `?status=${followupStatusFilter.value}`;
    const data = await api.get(`/api/follow-ups${query}`);
    followups.value = Array.isArray(data) ? data : data?.items || [];
    selectedFollowups.value = [];
  } finally {
    followupsLoading.value = false;
  }
}

function applyFollowupFilter(status) {
  if (followupStatusFilter.value === status) return;
  followupStatusFilter.value = status;
  loadFollowups();
}

function onFollowupSelectionChange(event) {
  selectedFollowups.value = event.value;
}

async function sendCampaign(record) {
  if (sendingCampaignId.value) return;
  sendingCampaignId.value = record.id;
  try {
    await api.post(`/api/winback/campaigns/${record.id}/send`, null, {
      successMessage: 'Campaign send queued',
    });
    await loadCampaigns();
    await loadStats();
  } finally {
    sendingCampaignId.value = null;
  }
}

function openCampaignDialog() {
  showCampaignDialog.value = true;
}

async function saveCampaign() {
  if (!campaignForm.value.name.trim() || !campaignForm.value.body_template.trim()) return;
  campaignSaving.value = true;
  try {
    await api.post(
      '/api/winback/campaigns',
      {
        name: campaignForm.value.name.trim(),
        channel: campaignForm.value.channel,
        inactivity_months: Number(campaignForm.value.inactivity_months) || 1,
        body_template: campaignForm.value.body_template.trim(),
      },
      { successMessage: 'Winback campaign created' }
    );
    showCampaignDialog.value = false;
    campaignForm.value = {
      name: '',
      channel: 'email',
      body_template: '',
      inactivity_months: 6,
    };
    await loadCampaigns();
    await loadStats();
  } finally {
    campaignSaving.value = false;
  }
}

function openFollowupDialog() {
  showFollowupDialog.value = true;
}

async function saveFollowup() {
  if (!followupForm.value.entity_id.trim() || !followupForm.value.due_date) return;
  creatingFollowup.value = true;
  try {
    await api.post('/api/follow-ups', {
      entity_type: followupForm.value.entity_type,
      entity_id: followupForm.value.entity_id.trim(),
      due_date: formatDate(followupForm.value.due_date),
      note: followupForm.value.note.trim() || null,
    }, { successMessage: 'Follow-up created' });
    showFollowupDialog.value = false;
    followupForm.value = {
      entity_type: 'customer',
      entity_id: '',
      due_date: null,
      note: '',
    };
    await loadFollowups();
  } finally {
    creatingFollowup.value = false;
  }
}

async function bulkCompleteFollowups() {
  if (!selectedFollowups.value.length) return;
  followupsCompleting.value = true;
  try {
    await Promise.all(
      selectedFollowups.value.map((item) => api.post(`/api/follow-ups/${item.id}/complete`))
    );
    selectedFollowups.value = [];
    await loadFollowups();
  } finally {
    followupsCompleting.value = false;
  }
}

onMounted(async () => {
  await Promise.all([loadStats(), loadCampaigns(), loadFollowups()]);
});
</script>

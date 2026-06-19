<template>
    <section class="marketing-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Marketing Campaigns</h2>
        </template>
        <template #end>
          <div class="toolbar-actions">
            <label class="toggle-label">
              <span class="toggle-copy">Show engaged only</span>
              <ToggleSwitch v-model="engagedOnly" data-testid="marketing-toggle-engaged" />
            </label>
            <Button
              label="+ New Campaign"
              icon="pi pi-plus"
              class="primary-action"
              data-testid="marketing-new-btn"
              @click="openDialog"
            />
          </div>
        </template>
      </Toolbar>

      <div class="filter-tabs">
        <Button
          v-for="status in statusTabs"
          :key="status"
          :label="tabLabelWithCount(status)"
          :severity="statusFilter === status ? undefined : 'secondary'"
          size="small"
          :data-testid="`marketing-tab-${status}`"
          @click="statusFilter = status"
        />
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
      responsiveLayout="scroll"
        v-else
        :value="filteredCampaigns"
        striped-rows
        :paginator="filteredCampaigns.length > 10"
        :rows="15"
        data-testid="marketing-table"
      >
        <template #empty>
          <div class="empty-state">
            <h3>No campaigns</h3>
            <p>Plan a campaign to keep customers informed.</p>
          </div>
        </template>
        <Column field="name" header="Name" />
        <Column header="Channel">
          <template #body="{ data }">{{ channelLabel(data.channel) }}</template>
        </Column>
        <Column field="status" header="Status" style="width:160px">
          <template #body="{ data }">
            <Tag :value="statusDisplay(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="sent" header="Sent" style="width:90px" />
        <Column field="opened" header="Opened" style="width:90px" />
        <Column field="clicked" header="Clicked" style="width:90px" />
        <Column header="Created" style="width:140px">
          <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showDialog" :header="dialogTitle" modal :style="{ width: '600px' }">
        <div class="form-grid">
          <div class="form-field">
            <label>Campaign Name</label>
            <InputText v-model="form.name" class="w-full" data-testid="marketing-name" />
          </div>
          <div class="form-field">
            <label>Channel</label>
            <Select
              v-model="form.channel"
              :options="channelOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="marketing-channel"
            />
          </div>
          <div class="form-field">
            <label>Subject</label>
            <InputText v-model="form.subject" class="w-full" data-testid="marketing-subject" />
          </div>
          <div class="form-field full-width">
            <label>Body</label>
            <Textarea v-model="form.body" rows="4" class="w-full" data-testid="marketing-body" />
          </div>
          <div class="form-field">
            <label>Audience Segment</label>
            <Select
              v-model="form.audience_segment"
              :options="segmentOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="marketing-segment"
            />
          </div>
          <div class="form-field">
            <label>Scheduled At</label>
            <DatePicker
              v-model="form.scheduled_at"
              class="w-full"
              showIcon
              dateFormat="yy-mm-dd"
              data-testid="marketing-scheduled"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" data-testid="marketing-cancel-btn" @click="showDialog = false" />
          <Button
            label="Save Campaign"
            icon="pi pi-check"
            :loading="saving"
            data-testid="marketing-save-btn"
            @click="saveCampaign"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import Toolbar from 'primevue/toolbar';
import ToggleSwitch from 'primevue/toggleswitch';

const api = useApiWithToast();
const campaigns = ref([]);
const loading = ref(true);
const showDialog = ref(false);
const saving = ref(false);
const statusTabs = ['draft', 'scheduled', 'sent', 'archived'];
const statusFilter = ref(statusTabs[0]);
const engagedOnly = ref(false);

const channelOptions = [
  { label: 'Email', value: 'email' },
  { label: 'SMS', value: 'sms' },
  { label: 'Mail', value: 'mail' },
];

const segmentOptions = [
  { label: 'All customers', value: 'all' },
  { label: 'High value', value: 'high_value' },
  { label: 'Recent activity', value: 'recent' },
];

function emptyForm() {
  return {
    name: '',
    channel: channelOptions[0].value,
    subject: '',
    body: '',
    audience_segment: segmentOptions[0].value,
    scheduled_at: null,
  };
}

const form = ref(emptyForm());

const dialogTitle = computed(() => 'New campaign');

const counts = computed(() => {
  const map = {};
  campaigns.value.forEach((campaign) => {
    const key = campaign.status || statusTabs[0];
    map[key] = (map[key] || 0) + 1;
  });
  return map;
});

const filteredCampaigns = computed(() => {
  return campaigns.value
    .filter((campaign) => campaign.status === statusFilter.value)
    .filter((campaign) => {
      if (!engagedOnly.value) return true;
      const opened = Number(campaign.opened) || 0;
      const clicked = Number(campaign.clicked) || 0;
      return opened > 0 || clicked > 0;
    });
});

function tabLabel(status) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function tabLabelWithCount(status) {
  const count = counts.value[status] || 0;
  return `${tabLabel(status)}${count ? ` (${count})` : ''}`;
}

function statusSeverity(value) {
  return {
    draft: 'info',
    scheduled: 'warning',
    sent: 'success',
    archived: 'secondary',
  }[value] || 'info';
}

function statusDisplay(value) {
  if (!value) return 'Draft';
  return value.replace('_', ' ');
}

function channelLabel(value) {
  if (!value) return 'Email';
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function formatDate(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '—';
  return parsed.toLocaleDateString();
}

async function loadCampaigns() {
  loading.value = true;
  try {
    const data = await api.get('/api/marketing');
    const list = Array.isArray(data) ? data : Array.isArray(data?.items) ? data.items : [];
    campaigns.value = list;
  } finally {
    loading.value = false;
  }
}

function openDialog() {
  form.value = emptyForm();
  showDialog.value = true;
}

async function saveCampaign() {
  if (!form.value.name.trim()) return;
  saving.value = true;
  const payload = {
    name: form.value.name,
    channel: form.value.channel,
    subject: form.value.subject,
    body: form.value.body,
    audience_segment: form.value.audience_segment,
    scheduled_at: form.value.scheduled_at ? form.value.scheduled_at.toISOString() : null,
  };
  try {
    await api.post('/api/marketing', payload, { successMessage: 'Campaign saved' });
    showDialog.value = false;
    await loadCampaigns();
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  loadCampaigns();
});
</script>

<style scoped>
.page-title {
  margin: 0;
}
.filter-tabs {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin: 1rem 0;
}
.toolbar-actions {
  display: flex;
  gap: 0.75rem;
  align-items: center;
}
.toggle-label {
  display: flex;
  gap: 0.4rem;
  align-items: center;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
.toggle-copy {
  font-size: 0.85rem;
}
.form-grid {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.form-field label {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--p-text-muted-color);
}
.w-full {
  width: 100%;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem 0;
}
.empty-state {
  text-align: center;
  padding: 3rem;
  color: var(--p-text-muted-color);
}
.empty-state h3 {
  margin: 1rem 0 0.5rem;
  color: var(--text-color);
}
</style>

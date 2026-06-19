<template>
    <section class="loyalty-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Loyalty</h2>
        </template>
        <template #end>
          <div class="toolbar-actions">
            <label class="toggle-label">
              <span class="toggle-copy">Elite tiers only</span>
              <ToggleSwitch v-model="eliteOnly" data-testid="loyalty-toggle-elite" />
            </label>
            <Button
              label="+ Adjust Points"
              icon="pi pi-star"
              class="primary-action"
              data-testid="loyalty-dialog-btn"
              @click="openDialog"
            />
          </div>
        </template>
      </Toolbar>

      <div class="filter-tabs">
        <Button
          v-for="tab in tabs"
          :key="tab"
          :label="tabLabelWithCount(tab)"
          :severity="activeTab === tab ? undefined : 'secondary'"
          size="small"
          :data-testid="`loyalty-tab-${tab}`"
          @click="activeTab = tab"
        />
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <div v-else>
        <DataTable
      responsiveLayout="scroll"
          v-if="activeTab === 'members'"
          :value="filteredMembers"
          striped-rows
          :paginator="filteredMembers.length > 10"
          :rows="15"
          data-testid="loyalty-members-table"
        >
          <template #empty>
            <div class="empty-state">
              <h3>No loyalty members</h3>
              <p>Members who qualify for rewards will appear here.</p>
            </div>
          </template>
          <Column header="Customer">
            <template #body="{ data }">{{ customerLabel(data) }}</template>
          </Column>
          <Column field="points" header="Points" />
          <Column header="Tier">
            <template #body="{ data }">
              <Tag :value="tierLabel(data.tier)" :severity="tierSeverity(data.tier)" />
            </template>
          </Column>
          <Column header="Joined" style="width:140px">
            <template #body="{ data }">{{ formatDate(data.joined_at) }}</template>
          </Column>
        </DataTable>

        <DataTable
      responsiveLayout="scroll"
          v-else
          :value="redemptions"
          striped-rows
          :paginator="redemptions.length > 10"
          :rows="15"
          data-testid="loyalty-redemptions-table"
        >
          <template #empty>
            <div class="empty-state">
              <h3>No redemptions yet</h3>
              <p>Reward redemption activity will show up once customers claim rewards.</p>
            </div>
          </template>
          <Column header="Customer">
            <template #body="{ data }">{{ data.customer || data.customer_name }}</template>
          </Column>
          <Column field="reward" header="Reward" />
          <Column field="points" header="Points" />
          <Column header="Redeemed" style="width:140px">
            <template #body="{ data }">{{ formatDate(data.redeemed_at) }}</template>
          </Column>
        </DataTable>
      </div>

      <Dialog v-model:visible="showDialog" :header="dialogTitle" modal :style="{ width: '560px' }">
        <div class="form-grid">
          <div class="form-field">
            <label>Customer</label>
            <Select
              v-model="form.customer_id"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="loyalty-customer"
            />
          </div>
          <div class="form-field">
            <label>Action</label>
            <Select
              v-model="form.action"
              :options="actionOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="loyalty-action"
            />
          </div>
          <div class="form-field">
            <label>Points</label>
            <InputNumber
              v-model="form.points"
              mode="decimal"
              min="0"
              class="w-full"
              data-testid="loyalty-points"
            />
          </div>
          <div v-if="form.action === 'redeem'" class="form-field full-width">
            <label>Reward</label>
            <InputText v-model="form.reward" class="w-full" data-testid="loyalty-reward" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" data-testid="loyalty-cancel-btn" @click="showDialog = false" />
          <Button
            :label="form.action === 'redeem' ? 'Redeem reward' : 'Adjust points'"
            icon="pi pi-check"
            :loading="saving"
            data-testid="loyalty-save-btn"
            @click="saveEntry"
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
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';
import ToggleSwitch from 'primevue/toggleswitch';

const api = useApiWithToast();
const loading = ref(true);
const members = ref([]);
const redemptions = ref([]);
const showDialog = ref(false);
const saving = ref(false);
const activeTab = ref('members');
const eliteOnly = ref(false);
const tabs = ['members', 'redemptions'];

const actionOptions = [
  { label: 'Adjust points', value: 'adjust' },
  { label: 'Redeem reward', value: 'redeem' },
];

const form = ref(emptyForm());

function emptyForm() {
  return {
    customer_id: null,
    action: 'adjust',
    points: null,
    reward: '',
  };
}

const dialogTitle = computed(() => (form.value.action === 'redeem' ? 'Redeem reward' : 'Adjust points'));

const tabCounts = computed(() => ({
  members: members.value.length,
  redemptions: redemptions.value.length,
}));

const eliteTiers = new Set(['gold', 'platinum', 'diamond']);

const filteredMembers = computed(() => {
  return members.value.filter((member) => {
    if (!eliteOnly.value) return true;
    const tier = (member.tier || '').toLowerCase();
    return eliteTiers.has(tier);
  });
});

const customerOptions = computed(() =>
  members.value.map((member) => ({
    label: member.customer || member.customer_name || `#${member.id ?? member.customer_id}`,
    value: member.id ?? member.customer_id,
  }))
);

function tabLabel(tab) {
  return tab.charAt(0).toUpperCase() + tab.slice(1);
}

function tabLabelWithCount(tab) {
  const count = tabCounts.value[tab] || 0;
  return `${tabLabel(tab)}${count ? ` (${count})` : ''}`;
}

function formatDate(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '—';
  return parsed.toLocaleDateString();
}

function customerLabel(member) {
  return member.customer || member.customer_name || '—';
}

function tierLabel(value) {
  return value ? value.replace('_', ' ').toUpperCase() : 'Member';
}

function tierSeverity(value) {
  const tier = (value || '').toLowerCase();
  if (tier === 'platinum' || tier === 'diamond') return 'success';
  if (tier === 'gold') return 'warning';
  return 'info';
}

async function loadLoyalty() {
  loading.value = true;
  try {
    const data = await api.get('/api/loyalty');
    const memberList = Array.isArray(data?.members)
      ? data.members
      : Array.isArray(data)
        ? data
        : Array.isArray(data?.items)
          ? data.items
          : [];
    const redemptionList = Array.isArray(data?.redemptions) ? data.redemptions : [];
    members.value = memberList;
    redemptions.value = redemptionList;
  } finally {
    loading.value = false;
  }
}

function openDialog() {
  form.value = emptyForm();
  showDialog.value = true;
}

async function saveEntry() {
  if (!form.value.customer_id || form.value.points == null) return;
  saving.value = true;
  const endpoint = form.value.action === 'redeem' ? '/api/loyalty/redeem' : '/api/loyalty/adjust';
  const payload = {
    customer_id: form.value.customer_id,
    points: Number(form.value.points) || 0,
    reward: form.value.action === 'redeem' ? form.value.reward : undefined,
  };
  try {
    await api.post(endpoint, payload, {
      successMessage: form.value.action === 'redeem' ? 'Reward redeemed' : 'Points updated',
    });
    showDialog.value = false;
    await loadLoyalty();
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  loadLoyalty();
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
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem 0;
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

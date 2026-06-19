<template>
    <section class="portal-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Customer Portal</h2>
        </template>
        <template #end>
          <Button
            label="+ Send Portal Invite"
            icon="pi pi-envelope"
            class="p-button-outlined"
            data-testid="portal-send-invite-btn"
            @click="openInviteDialog()"
          />
        </template>
      </Toolbar>

      <div class="filter-tabs" data-testid="portal-tabs">
        <Button
          v-for="tab in portalTabs"
          :key="tab"
          :label="tabLabel(tab)"
          :severity="statusFilter === tab ? undefined : 'secondary'"
          size="small"
          class="p-button-text"
          :data-testid="`portal-tab-${tab}`"
          @click="statusFilter = tab"
        />
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
      responsiveLayout="scroll"
        v-else
        :value="filteredEntries"
        paginator
        :rows="10"
        striped-rows
        class="clickable-row"
        data-testid="portal-table"
      >
        <Column header="Customer" :style="{ minWidth: '200px' }">
          <template #body="{ data }">{{ data.customer_name || data.customer }}</template>
        </Column>
        <Column header="Portal Enabled" :style="{ width: '160px' }">
          <template #body="{ data }">
            <ToggleSwitch
              :model-value="data.portal_enabled"
              :disabled="toggleLoading[data.id]"
              @change="(val) => togglePortal(data, val)"
              :data-testid="`portal-toggle-${data.id}`"
            />
          </template>
        </Column>
        <Column header="Last Login" :style="{ width: '160px' }">
          <template #body="{ data }">{{ formatDate(data.last_login) }}</template>
        </Column>
        <Column field="invoices_viewed" header="Invoices Viewed" :style="{ width: '170px' }" />
        <Column field="payments_made" header="Payments Made" :style="{ width: '150px' }" />
        <Column header="Actions" :style="{ width: '160px' }">
          <template #body="{ data }">
            <Button
              icon="pi pi-envelope"
              label="Send Invite"
              size="small"
              text
              @click.stop="openInviteDialog(data)"
              data-testid="portal-row-invite-btn"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="inviteDialogVisible"
        header="Send Portal Invite"
        :modal="true"
        :style="{ width: '420px' }"
        data-testid="portal-invite-dialog"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label for="portal-invite-customer">Customer</label>
            <Select
              id="portal-invite-customer"
              v-model="inviteForm.customer_id"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              filter
              showClear
              class="w-full"
              data-testid="portal-invite-customer"
            />
          </div>
          <div class="form-field full-width">
            <label for="portal-invite-email">Email (optional)</label>
            <InputText
              id="portal-invite-email"
              v-model="inviteForm.email"
              class="w-full"
              data-testid="portal-invite-email"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="inviteDialogVisible = false" />
          <Button
            label="Send"
            icon="pi pi-check"
            :loading="inviteSending"
            @click="sendInvite"
            data-testid="portal-invite-send"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import ToggleSwitch from 'primevue/toggleswitch';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();

const portalEntries = ref([]);
const loading = ref(true);
const statusFilter = ref('enabled');
const inviteDialogVisible = ref(false);
const inviteSending = ref(false);
const toggleLoading = reactive({});

const portalTabs = ['enabled', 'disabled'];
const inviteForm = ref({ customer_id: null, email: '' });

const filteredEntries = computed(() => {
  if (statusFilter.value === 'enabled') {
    return portalEntries.value.filter((entry) => entry.portal_enabled);
  }
  if (statusFilter.value === 'disabled') {
    return portalEntries.value.filter((entry) => !entry.portal_enabled);
  }
  return portalEntries.value;
});

const customerOptions = computed(() =>
  portalEntries.value.map((entry) => ({
    label: entry.customer_name || entry.customer || `Customer #${entry.id}`,
    value: entry.id,
  }))
);

const counts = computed(() => {
  const map = { enabled: 0, disabled: 0 };
  portalEntries.value.forEach((entry) => {
    if (entry.portal_enabled) map.enabled += 1;
    else map.disabled += 1;
  });
  return map;
});

const tabLabel = (tab) => {
  const label = tab.charAt(0).toUpperCase() + tab.slice(1);
  const suffix = counts.value[tab] ? ` (${counts.value[tab]})` : '';
  return `${label}${suffix}`;
};

const formatDate = (value) => {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleDateString();
};

const loadPortal = async () => {
  loading.value = true;
  try {
    const data = await api.get('/api/portal');
    const list = Array.isArray(data) ? data : data?.items || data?.entries || [];
    portalEntries.value = list;
  } finally {
    loading.value = false;
  }
};

const togglePortal = async (entry, value) => {
  toggleLoading[entry.id] = true;
  try {
    await api.patch(`/api/portal/${entry.id}`, { portal_enabled: value });
    entry.portal_enabled = value;
  } finally {
    toggleLoading[entry.id] = false;
  }
};

const openInviteDialog = (entry = null) => {
  inviteForm.value = {
    customer_id: entry ? entry.id : null,
    email: entry?.email || '',
  };
  inviteDialogVisible.value = true;
};

const sendInvite = async () => {
  if (!inviteForm.value.customer_id) return;
  inviteSending.value = true;
  try {
    await api.post('/api/portal/invite', {
      customer_id: inviteForm.value.customer_id,
      email: inviteForm.value.email || undefined,
    });
    inviteDialogVisible.value = false;
    inviteForm.value = { customer_id: null, email: '' };
    await loadPortal();
  } finally {
    inviteSending.value = false;
  }
};

onMounted(loadPortal);
</script>

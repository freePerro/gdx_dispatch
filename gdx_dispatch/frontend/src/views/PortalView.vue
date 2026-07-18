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
        <template #empty>
          <EmptyState
            icon="pi pi-globe"
            title="No portal customers"
            message="Invite customers to the portal so they can view invoices and pay online."
            action-label="Send Portal Invite"
            @action="openInviteDialog()"
          />
        </template>
        <Column header="Customer" :style="{ minWidth: '200px' }">
          <template #body="{ data }">{{ data.customer_name || data.customer }}</template>
        </Column>
        <Column header="Email" :style="{ minWidth: '200px' }">
          <template #body="{ data }">{{ data.email || '—' }}</template>
        </Column>
        <Column header="Portal Enabled" :style="{ width: '160px' }">
          <template #body="{ data }">
            <ToggleSwitch
              :model-value="data.portal_enabled"
              :disabled="toggleLoading[data.id]"
              @update:model-value="(val) => togglePortal(data, val)"
              :data-testid="`portal-toggle-${data.id}`"
            />
          </template>
        </Column>
        <Column header="Last Login" :style="{ width: '160px' }">
          <template #body="{ data }">{{ formatDate(data.last_login) }}</template>
        </Column>
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

      <Dialog
        v-model:visible="inviteResultVisible"
        header="Portal Invite"
        :modal="true"
        :style="{ width: '480px' }"
        data-testid="portal-invite-result-dialog"
      >
        <div v-if="inviteResult" class="invite-result">
          <p v-if="inviteResult.invite_sent" data-testid="invite-result-sent">
            <i class="pi pi-check-circle" style="color: var(--p-green-500)" />
            Invite emailed to <strong>{{ inviteResult.email }}</strong>.
          </p>
          <p v-else data-testid="invite-result-not-sent">
            <i class="pi pi-exclamation-triangle" style="color: var(--p-amber-500)" />
            Email delivery isn't configured, so nothing was sent automatically.
            Share this sign-in link with the customer directly (valid 7 days):
          </p>
          <div class="invite-link-row">
            <InputText :model-value="inviteResult.magic_link" readonly class="w-full" data-testid="invite-result-link" @focus="$event.target.select()" />
            <Button icon="pi pi-copy" severity="secondary" aria-label="Copy link" @click="copyInviteLink" data-testid="invite-result-copy" />
          </div>
        </div>
        <template #footer>
          <Button label="Done" @click="inviteResultVisible = false" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDate } from '../composables/useFormatters';
import EmptyState from '../components/EmptyState.vue';
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
const inviteResultVisible = ref(false);
const inviteResult = ref(null);
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
    const result = await api.post('/api/portal/invite', {
      customer_id: inviteForm.value.customer_id,
      email: inviteForm.value.email || undefined,
    });
    inviteDialogVisible.value = false;
    inviteForm.value = { customer_id: null, email: '' };
    inviteResult.value = result;
    inviteResultVisible.value = true;
    await loadPortal();
  } finally {
    inviteSending.value = false;
  }
};

const copyInviteLink = async () => {
  if (!inviteResult.value?.magic_link) return;
  try {
    await navigator.clipboard.writeText(inviteResult.value.magic_link);
  } catch {
    // Clipboard API unavailable (http origin) — the readonly input
    // self-selects on focus so manual copy still works.
  }
};

onMounted(loadPortal);
</script>

<style scoped>
.invite-result p { display: flex; align-items: center; gap: 0.5rem; margin: 0 0 0.75rem; }
.invite-link-row { display: flex; gap: 0.5rem; align-items: center; }
</style>

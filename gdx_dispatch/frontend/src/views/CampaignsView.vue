<template>
    <section class="campaigns-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Campaigns</h2>
        </template>
        <template #end>
          <Button label="New Campaign" icon="pi pi-plus" data-testid="new-campaign-btn" @click="showCreate = true" />
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error">{{ loadError }}</div>
      <div v-if="isLoading" class="spinner-wrap"><ProgressSpinner /></div>

      <template v-if="!isLoading && !loadError">
        <!-- Stats -->
        <div class="stats-row">
          <div class="stat-card">
            <div class="stat-val">{{ campaigns.length }}</div>
            <div class="stat-label">Total</div>
          </div>
          <div class="stat-card">
            <div class="stat-val">{{ campaigns.filter(c => c.status === 'active').length }}</div>
            <div class="stat-label">Active</div>
          </div>
          <div class="stat-card">
            <div class="stat-val">{{ totalSent }}</div>
            <div class="stat-label">Sent</div>
          </div>
          <div class="stat-card">
            <div class="stat-val">{{ totalOpens }}</div>
            <div class="stat-label">Opens</div>
          </div>
        </div>

        <!-- Filter tabs -->
        <div class="filter-tabs">
          <Button v-for="s in ['all', 'draft', 'active', 'paused', 'completed']" :key="s"
            :label="s" :severity="statusFilter === s ? undefined : 'secondary'" size="small"
            @click="statusFilter = s" :data-testid="'filter-' + s" />
        </div>

        <DataTable
      responsiveLayout="scroll" :value="filteredCampaigns" paginator :rows="10" data-testid="campaigns-table"
          striped-rows responsive-layout="scroll" :global-filter-fields="['name', 'type']"
          sort-field="created_at" :sort-order="-1">
          <Column field="name" header="Campaign" sortable>
            <template #body="{ data }">
              <span class="campaign-name" @click="editCampaign(data)">{{ data.name || data.title }}</span>
            </template>
          </Column>
          <Column field="type" header="Type" sortable>
            <template #body="{ data }">
              <Tag :value="data.type || data.campaign_type || 'email'" :severity="typeSeverity(data.type)" />
            </template>
          </Column>
          <Column field="status" header="Status" sortable>
            <template #body="{ data }">
              <Tag :value="data.status || 'draft'" :severity="statusSeverity(data.status)" />
            </template>
          </Column>
          <Column field="sent_count" header="Sent" sortable />
          <Column field="open_count" header="Opens" sortable />
          <Column header="Open Rate">
            <template #body="{ data }">
              {{ data.sent_count > 0 ? Math.round((data.open_count / data.sent_count) * 100) + '%' : '—' }}
            </template>
          </Column>
          <Column field="created_at" header="Created" sortable>
            <template #body="{ data }">{{ data.created_at?.split('T')[0] || '—' }}</template>
          </Column>
          <Column header="Actions" style="width: 8rem">
            <template #body="{ data }">
              <div class="action-btns">
                <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" severity="secondary" size="small" text @click="editCampaign(data)" />
                <Button v-tooltip="'Delete'" icon="pi pi-trash" aria-label="Delete" severity="danger" size="small" text
                  @click="confirmDelete(data)" data-testid="delete-campaign" />
              </div>
            </template>
          </Column>
        </DataTable>
      </template>

      <!-- Create / Edit Dialog -->
      <Dialog v-model:visible="showCreate" :header="editingCampaign ? 'Edit Campaign' : 'New Campaign'"
        modal :style="{ width: '500px' }" data-testid="campaign-dialog">
        <div class="form-grid">
          <div class="field">
            <label>Campaign Name</label>
            <InputText v-model="form.name" placeholder="Summer promotion" data-testid="campaign-name" class="w-full" />
          </div>
          <div class="field">
            <label>Type</label>
            <Select v-model="form.type" :options="typeOptions" placeholder="Select type" data-testid="campaign-type" class="w-full" />
          </div>
          <div class="field">
            <label>Status</label>
            <Select v-model="form.status" :options="['draft', 'active', 'paused', 'completed']" placeholder="Select status" data-testid="campaign-status" class="w-full" />
          </div>
          <div class="field">
            <label>Subject Line</label>
            <InputText v-model="form.subject" placeholder="Don't miss our summer deals!" data-testid="campaign-subject" class="w-full" />
          </div>
          <div class="field full-width">
            <label>Message Body</label>
            <Textarea v-model="form.body" rows="5" placeholder="Campaign content..." data-testid="campaign-body" class="w-full" />
          </div>
          <div class="field">
            <label>Target Audience</label>
            <Select v-model="form.audience" :options="['all_customers', 'active_customers', 'inactive_30d', 'inactive_90d', 'new_leads']"
              placeholder="Select audience" data-testid="campaign-audience" class="w-full" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showCreate = false" />
          <Button :label="editingCampaign ? 'Save Changes' : 'Create Campaign'" icon="pi pi-check"
            @click="saveCampaign" :loading="saving" data-testid="save-campaign" />
        </template>
      </Dialog>

      <!-- Delete Confirmation -->
      <Dialog v-model:visible="showDeleteConfirm" header="Delete Campaign?" modal :style="{ width: '400px' }">
        <p>Are you sure you want to delete "{{ deletingCampaign?.name }}"?</p>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDeleteConfirm = false" />
          <Button label="Delete" severity="danger" icon="pi pi-trash" aria-label="Delete" @click="deleteCampaign" :loading="deleting" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const campaigns = ref([]);
const isLoading = ref(true);
const loadError = ref("");
const statusFilter = ref("all");
const showCreate = ref(false);
const showDeleteConfirm = ref(false);
const editingCampaign = ref(null);
const deletingCampaign = ref(null);
const saving = ref(false);
const deleting = ref(false);

const typeOptions = ["email", "sms", "email_drip", "review_request", "winback"];

const emptyForm = () => ({
  name: "", type: "email", status: "draft", subject: "", body: "", audience: "all_customers",
});
const form = ref(emptyForm());

const filteredCampaigns = computed(() => {
  if (statusFilter.value === "all") return campaigns.value;
  return campaigns.value.filter((c) => c.status === statusFilter.value);
});

const totalSent = computed(() => campaigns.value.reduce((s, c) => s + (c.sent_count || 0), 0));
const totalOpens = computed(() => campaigns.value.reduce((s, c) => s + (c.open_count || 0), 0));

function typeSeverity(type) {
  return { email: "info", sms: "success", email_drip: "warn", review_request: "secondary", winback: "danger" }[type] || "secondary";
}

function statusSeverity(status) {
  return { draft: "secondary", active: "success", paused: "warn", completed: "info" }[status] || "secondary";
}

async function fetchCampaigns() {
  isLoading.value = true;
  loadError.value = "";
  try {
    const result = await api.get("/api/campaigns");
    const payload = result?.data || result;
    campaigns.value = Array.isArray(payload) ? payload : payload?.items || [];
  } catch (e) {
    loadError.value = e.message || "Failed to load campaigns";
  } finally {
    isLoading.value = false;
  }
}

function editCampaign(campaign) {
  editingCampaign.value = campaign;
  form.value = { ...campaign };
  showCreate.value = true;
}

async function saveCampaign() {
  saving.value = true;
  try {
    if (editingCampaign.value) {
      await api.patch(`/api/campaigns/${editingCampaign.value.id}`, form.value);
    } else {
      await api.post("/api/campaigns", form.value);
    }
    showCreate.value = false;
    editingCampaign.value = null;
    form.value = emptyForm();
    await fetchCampaigns();
  } finally {
    saving.value = false;
  }
}

function confirmDelete(campaign) {
  deletingCampaign.value = campaign;
  showDeleteConfirm.value = true;
}

async function deleteCampaign() {
  deleting.value = true;
  try {
    await api.delete(`/api/campaigns/${deletingCampaign.value.id}`);
    showDeleteConfirm.value = false;
    deletingCampaign.value = null;
    await fetchCampaigns();
  } finally {
    deleting.value = false;
  }
}

onMounted(fetchCampaigns);
</script>

<style scoped>
.page-title { margin: 0; }
.stats-row { display: flex; gap: 1rem; margin-bottom: 1rem; flex-wrap: wrap; }
.stat-card { background: var(--surface-card); border-radius: 8px; padding: 1rem 1.5rem; text-align: center; min-width: 100px; }
.stat-val { font-size: 1.5rem; font-weight: 700; color: var(--p-primary-color); }
.stat-label { font-size: 0.75rem; color: var(--p-text-muted-color); text-transform: uppercase; }
.filter-tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; flex-wrap: wrap; }
.campaign-name { cursor: pointer; color: var(--p-primary-color); }
.campaign-name:hover { text-decoration: underline; }
.action-btns { display: flex; gap: 0.25rem; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.field { display: flex; flex-direction: column; gap: 0.3rem; }
.field label { font-size: 0.82rem; font-weight: 600; color: var(--p-text-muted-color); }
.full-width { grid-column: 1 / -1; }
.w-full { width: 100%; }
.inline-error { color: #ef4444; padding: 0.5rem; margin-bottom: 1rem; }
.spinner-wrap { display: flex; justify-content: center; padding: 3rem; }
.muted { color: var(--p-text-muted-color); }
</style>

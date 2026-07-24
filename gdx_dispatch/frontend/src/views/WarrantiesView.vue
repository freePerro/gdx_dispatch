<template>
    <section class="warranties-view view-card" data-testid="warranties-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title" data-testid="warranties-title">Warranties</h2>
        </template>
        <template #end>
          <Button
            v-if="activeTab === 'warranties'"
            label="New Warranty"
            icon="pi pi-plus"
            @click="openCreate"
            data-testid="warranties-new"
          />
        </template>
      </Toolbar>

      <!-- Tabs: the claims half of this capability was backend-only until
           2026-07 (contract-gap Tier 2.2) — a warranty could be recorded but
           a claim against it could never be filed or tracked in the app. -->
      <div class="status-tabs">
        <Button
          label="Warranties"
          :severity="activeTab === 'warranties' ? undefined : 'secondary'"
          size="small"
          data-testid="tab-warranties"
          @click="activeTab = 'warranties'"
        />
        <Button
          :label="`Claims${claims.length ? ` (${claims.length})` : ''}`"
          :severity="activeTab === 'claims' ? undefined : 'secondary'"
          size="small"
          data-testid="tab-claims"
          @click="activeTab = 'claims'"
        />
      </div>

      <div v-if="loading" class="spinner-wrap" data-testid="warranties-loading">
        <ProgressSpinner />
      </div>

      <DataTable
        v-else-if="activeTab === 'warranties'"
        :value="warranties"
        striped-rows
        responsiveLayout="scroll"
        class="clickable-row"
        data-testid="warranties-table"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-shield"
            title="No warranties yet"
            message="Track product warranties by customer and job so expirations never sneak up."
            action-label="New Warranty"
            @action="openCreate"
          />
        </template>
        <Column field="description" header="Coverage" />
        <Column field="customer_id" header="Customer">
          <template #body="{ data }">{{ customerName(data.customer_id) }}</template>
        </Column>
        <Column field="start_date" header="Start">
          <template #body="{ data }">{{ formatDate(data.start_date) }}</template>
        </Column>
        <Column field="end_date" header="Expires">
          <template #body="{ data }">{{ formatDate(data.end_date) }}</template>
        </Column>
        <Column field="status" header="Status" />
        <Column field="claim_count" header="Claims" />
        <Column header="Actions" style="width: 9rem">
          <template #body="{ data }">
            <Button
              v-tooltip="'File claim'"
              icon="pi pi-flag" aria-label="File claim"
              text
              size="small"
              class="mr-2"
              @click.stop="openClaimDialog(data)"
              data-testid="warranties-file-claim"
            />
            <Button
              v-tooltip="'Edit'"
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              class="mr-2"
              @click.stop="openEdit(data)"
              data-testid="warranties-edit"
            />
            <Button
              v-tooltip="'Delete'"
              icon="pi pi-trash" aria-label="Delete"
              severity="danger"
              text
              size="small"
              @click.stop="deleteWarranty(data)"
              data-testid="warranties-delete"
            />
          </template>
        </Column>
      </DataTable>

      <DataTable
        v-else
        :value="claims"
        striped-rows
        responsiveLayout="scroll"
        data-testid="claims-table"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-flag"
            title="No claims filed"
            message="File a claim from the Warranties tab — it's tracked here from filed through resolution."
          />
        </template>
        <Column field="filed_at" header="Filed">
          <template #body="{ data }">{{ formatDate(data.filed_at) }}</template>
        </Column>
        <Column field="customer_id" header="Customer">
          <template #body="{ data }">{{ customerName(data.customer_id) }}</template>
        </Column>
        <Column field="claim_notes" header="Notes">
          <template #body="{ data }">{{ truncate(data.claim_notes) }}</template>
        </Column>
        <Column field="status" header="Status" />
        <Column field="resolution" header="Resolution">
          <template #body="{ data }">{{ truncate(data.resolution) }}</template>
        </Column>
        <Column header="Actions" style="width: 6rem">
          <template #body="{ data }">
            <Button
              v-tooltip="'Update claim'"
              icon="pi pi-pencil" aria-label="Update claim"
              text
              size="small"
              @click.stop="openClaimEdit(data)"
              data-testid="claims-edit"
            />
          </template>
        </Column>
      </DataTable>

      <!-- Warranty create/edit — fields match the backend contract
           (job_id/customer_id/description/start_date/end_date): the previous
           form sent product/customer/expiry_date, which the API rejects, so
           warranty creation from this page had never once succeeded. -->
      <Dialog
        v-model:visible="showDialog"
        :header="editingWarranty ? 'Edit Warranty' : 'Create Warranty'"
        modal
        :style="{ width: '520px' }"
        data-testid="warranties-dialog"
      >
        <div class="form-grid" data-testid="warranties-form">
          <div class="form-field">
            <label>Coverage / Description *</label>
            <InputText v-model="form.description" class="w-full" placeholder="e.g. LiftMaster 8500W opener — 5yr parts" data-testid="warranties-product" />
          </div>
          <!-- Customer/job are the warranty's identity — the PATCH endpoint
               deliberately ignores them, so lock them in edit mode instead of
               pretending they're editable (silent-drop is the bug class this
               whole rewrite exists to kill). -->
          <div class="form-field">
            <label>Customer *</label>
            <Select
              v-model="form.customer_id"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              filter
              class="w-full"
              placeholder="Select customer"
              :disabled="!!editingWarranty"
              data-testid="warranties-customer"
            />
          </div>
          <div class="form-field">
            <label>Job ID *</label>
            <InputText v-model="form.job_id" class="w-full" :disabled="!!editingWarranty" data-testid="warranties-job" />
          </div>
          <div class="form-field">
            <label>Start Date *</label>
            <DatePicker v-model="form.start_date" dateFormat="yy-mm-dd" showIcon class="w-full" data-testid="warranties-start" />
          </div>
          <div class="form-field">
            <label>End Date *</label>
            <DatePicker v-model="form.end_date" dateFormat="yy-mm-dd" showIcon class="w-full" data-testid="warranties-expiry" />
          </div>
          <div v-if="editingWarranty" class="form-field">
            <label>Status</label>
            <Select v-model="form.status" :options="statusOptions" optionLabel="label" optionValue="value" class="w-full" data-testid="warranties-status" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="closeDialog" data-testid="warranties-cancel" />
          <Button
            :label="editingWarranty ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="saving"
            :disabled="!formValid"
            @click="saveWarranty"
            data-testid="warranties-save"
          />
        </template>
      </Dialog>

      <!-- File Claim -->
      <Dialog
        v-model:visible="showClaimDialog"
        header="File Warranty Claim"
        modal
        :style="{ width: '480px' }"
        data-testid="claim-dialog"
      >
        <div class="form-grid-single">
          <p v-if="claimTarget" class="claim-context">
            {{ claimTarget.description }} — {{ customerName(claimTarget.customer_id) }}
          </p>
          <div class="form-field">
            <label>What happened? *</label>
            <Textarea v-model="claimForm.notes" rows="4" class="w-full" placeholder="Failure description, part affected..." data-testid="claim-notes" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showClaimDialog = false" />
          <Button
            label="File Claim"
            icon="pi pi-flag"
            :loading="filingClaim"
            :disabled="!claimForm.notes.trim()"
            @click="fileClaim"
            data-testid="claim-save"
          />
        </template>
      </Dialog>

      <!-- Update Claim -->
      <Dialog
        v-model:visible="showClaimEditDialog"
        header="Update Claim"
        modal
        :style="{ width: '480px' }"
        data-testid="claim-edit-dialog"
      >
        <div class="form-grid-single">
          <div class="form-field">
            <label>Status</label>
            <Select v-model="claimEditForm.status" :options="claimStatusOptions" class="w-full" data-testid="claim-status" />
          </div>
          <div class="form-field">
            <label>Resolution notes</label>
            <Textarea v-model="claimEditForm.resolution" rows="3" class="w-full" data-testid="claim-resolution" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showClaimEditDialog = false" />
          <Button label="Save" icon="pi pi-check" :loading="savingClaim" @click="saveClaimEdit" data-testid="claim-edit-save" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { useDestructiveConfirm } from "../composables/useDestructiveConfirm";
import EmptyState from "../components/EmptyState.vue";
import Button from "primevue/button";
import DatePicker from "primevue/datepicker";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Select from "primevue/select";
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();
const { confirmAsync } = useDestructiveConfirm();

const activeTab = ref("warranties");
const warranties = ref([]);
const claims = ref([]);
const customers = ref([]);
const loading = ref(true);
const showDialog = ref(false);
const editingWarranty = ref(null);
const saving = ref(false);
const form = ref(emptyForm());

const showClaimDialog = ref(false);
const claimTarget = ref(null);
const claimForm = ref({ notes: "" });
const filingClaim = ref(false);

const showClaimEditDialog = ref(false);
const claimEditTarget = ref(null);
const claimEditForm = ref({ status: "filed", resolution: "" });
const savingClaim = ref(false);

const statusOptions = [
  { label: "Active", value: "active" },
  { label: "Expired", value: "expired" },
  { label: "Claimed", value: "claimed" },
  { label: "Voided", value: "voided" },
];
const claimStatusOptions = ["filed", "pending", "approved", "denied", "replaced"];

const customerOptions = computed(() =>
  customers.value.map((c) => ({ label: c.name || c.id, value: String(c.id) })),
);
const customerNames = computed(() => {
  const map = {};
  for (const c of customers.value) map[String(c.id)] = c.name || String(c.id);
  return map;
});
const formValid = computed(() =>
  form.value.description.trim() && form.value.customer_id && form.value.job_id.trim()
  && form.value.start_date && form.value.end_date,
);

function emptyForm() {
  return {
    description: "",
    customer_id: null,
    job_id: "",
    start_date: null,
    end_date: null,
    status: "active",
  };
}

function customerName(id) {
  return customerNames.value[String(id)] || String(id || "—");
}

function truncate(text, len = 60) {
  if (!text) return "—";
  return text.length > len ? `${text.slice(0, len)}…` : text;
}

function formatDate(value) {
  if (!value) return "—";
  const s = String(value);
  return s.includes("T") ? s.split("T")[0] : s.split(" ")[0];
}

function toDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function toIso(value) {
  if (!value) return null;
  return value.toISOString().slice(0, 10);
}

async function loadWarranties() {
  const data = await api.get("/api/warranties");
  warranties.value = Array.isArray(data) ? data : data?.items ?? [];
}

async function loadClaims() {
  try {
    const data = await api.get("/api/warranty-claims", { suppressErrorToast: true });
    claims.value = Array.isArray(data) ? data : data?.items ?? [];
  } catch {
    claims.value = [];
  }
}

async function loadCustomers() {
  try {
    const data = await api.get("/api/customers?per_page=1000", { suppressErrorToast: true });
    customers.value = Array.isArray(data) ? data : data?.items ?? [];
  } catch {
    customers.value = [];
  }
}

async function loadAll() {
  loading.value = true;
  try {
    await Promise.all([loadWarranties(), loadClaims(), loadCustomers()]);
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingWarranty.value = null;
  form.value = emptyForm();
  showDialog.value = true;
}

function openEdit(item) {
  editingWarranty.value = item;
  form.value = {
    description: item.description ?? "",
    customer_id: item.customer_id ? String(item.customer_id) : null,
    job_id: item.job_id ?? "",
    start_date: toDate(item.start_date),
    end_date: toDate(item.end_date),
    status: item.status ?? "active",
  };
  showDialog.value = true;
}

function closeDialog() {
  showDialog.value = false;
  saving.value = false;
  editingWarranty.value = null;
  form.value = emptyForm();
}

async function saveWarranty() {
  if (!formValid.value) return;
  saving.value = true;
  // Edit sends only the fields PATCH accepts (description/dates/status);
  // customer/job are create-time identity.
  const payload = editingWarranty.value
    ? {
        description: form.value.description.trim(),
        start_date: toIso(form.value.start_date),
        end_date: toIso(form.value.end_date),
        status: form.value.status,
      }
    : {
        description: form.value.description.trim(),
        customer_id: form.value.customer_id,
        job_id: form.value.job_id.trim(),
        start_date: toIso(form.value.start_date),
        end_date: toIso(form.value.end_date),
      };
  try {
    if (editingWarranty.value) {
      const id = editingWarranty.value.id;
      await api.patch(`/api/warranties/${encodeURIComponent(id)}`, payload, { successMessage: "Warranty updated" });
    } else {
      await api.post("/api/warranties", payload, { successMessage: "Warranty created" });
    }
    await loadWarranties();
    closeDialog();
  } finally {
    saving.value = false;
  }
}

async function deleteWarranty(item) {
  const id = item.id;
  if (!id) return;
  if (!(await confirmAsync({ header: 'Confirm', message: "Remove this warranty?" }))) return;
  saving.value = true;
  try {
    await api.del(`/api/warranties/${encodeURIComponent(id)}`, { successMessage: "Warranty deleted" });
    await loadWarranties();
  } finally {
    saving.value = false;
  }
}

function openClaimDialog(warranty) {
  claimTarget.value = warranty;
  claimForm.value = { notes: "" };
  showClaimDialog.value = true;
}

async function fileClaim() {
  if (!claimTarget.value) return;
  filingClaim.value = true;
  try {
    // POST /api/warranty-claims also bumps the warranty (status→claimed,
    // claim_count) server-side, so one call keeps both records honest.
    await api.post(
      "/api/warranty-claims",
      {
        warranty_id: String(claimTarget.value.id),
        job_id: claimTarget.value.job_id ? String(claimTarget.value.job_id) : null,
        customer_id: String(claimTarget.value.customer_id),
        claim_notes: claimForm.value.notes.trim(),
      },
      { successMessage: "Claim filed" },
    );
    showClaimDialog.value = false;
    await Promise.all([loadWarranties(), loadClaims()]);
    activeTab.value = "claims";
  } catch {
    // fireError already toasted
  } finally {
    filingClaim.value = false;
  }
}

function openClaimEdit(claim) {
  claimEditTarget.value = claim;
  claimEditForm.value = { status: claim.status || "filed", resolution: claim.resolution || "" };
  showClaimEditDialog.value = true;
}

async function saveClaimEdit() {
  if (!claimEditTarget.value) return;
  savingClaim.value = true;
  try {
    await api.patch(
      `/api/warranty-claims/${claimEditTarget.value.id}`,
      { status: claimEditForm.value.status, resolution: claimEditForm.value.resolution || null },
      { successMessage: "Claim updated" },
    );
    showClaimEditDialog.value = false;
    await loadClaims();
  } catch {
    // fireError already toasted
  } finally {
    savingClaim.value = false;
  }
}

onMounted(loadAll);
</script>

<style scoped>
.status-tabs { display: flex; gap: 0.5rem; margin: 0.75rem 0 1rem; }
.claim-context { margin: 0 0 0.75rem; color: var(--p-text-muted-color, #64748b); }
</style>

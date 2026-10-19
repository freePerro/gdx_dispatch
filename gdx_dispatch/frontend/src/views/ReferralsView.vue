<template>
    <section class="referrals-view view-card" data-testid="referrals-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Referrals</h2>
        </template>
        <template #end>
          <Button
            label="+ Add Referral"
            icon="pi pi-plus"
            class="ml-2"
            data-testid="referral-add-btn"
            @click="openCreate()"
          />
        </template>
      </Toolbar>

      <div class="filter-tabs" data-testid="referrals-tabs">
        <Button
          v-for="status in ['all', ...STATUSES]"
          :key="status"
          :label="statusLabel(status) + (counts[status] ? ` (${counts[status]})` : '')"
          :severity="statusFilter === status ? undefined : 'secondary'"
          size="small"
          @click="statusFilter = status"
        />
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
      responsiveLayout="scroll"
        v-else
        :value="filteredReferrals"
        paginator
        :rows="15"
        striped-rows
        class="clickable-row"
        data-testid="referrals-table"
      >
        <template #empty>
          <div class="empty-state">
            <i class="pi pi-users" style="font-size:3rem; color:#64748b;"></i>
            <h3>No referrals yet</h3>
            <p>Capture new referrals to reward advocates and grow your business.</p>
            <Button label="+ Add Referral" @click="openCreate()" />
          </div>
        </template>

        <Column header="Referrer">
          <template #body="{ data }">
            <router-link
              v-if="data.referrer_name"
              :to="`/customers/${data.referrer_id}`"
            >{{ data.referrer_name }}</router-link>
            <span v-else class="muted">Unknown customer</span>
          </template>
        </Column>
        <Column field="referee_name" header="Referred" />
        <Column field="referee_phone" header="Phone" style="width:150px" />
        <Column field="status" header="Status" style="width:140px">
          <template #body="{ data }">
            <Tag :value="statusLabel(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="reward_given" header="Rewarded" style="width:110px">
          <template #body="{ data }">{{ data.reward_given ? 'Yes' : 'No' }}</template>
        </Column>
        <Column field="created_at" header="Created" style="width:120px">
          <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
        </Column>
        <Column header="Actions" style="width:120px">
          <template #body="{ data }">
            <Button
              v-if="data.status !== 'rewarded'"
              v-tooltip="'Update status'"
              icon="pi pi-pencil" aria-label="Update status"
              text
              size="small"
              data-testid="referral-edit-btn"
              @click.stop="openEdit(data)"
            />
          </template>
        </Column>
      </DataTable>

      <!-- Create: the four fields ReferralCreateIn accepts. The server sets
           status to pending; there is no notes or reward-amount column. -->
      <Dialog
        v-model:visible="showCreate"
        header="New Referral"
        modal
        :style="{ width: '520px', maxWidth: '95vw' }"
        data-testid="referral-dialog"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label for="referral-referrer">Referrer (customer)</label>
            <AutoComplete
              id="referral-referrer"
              v-model="referrerPick"
              :suggestions="customerSuggestions"
              option-label="label"
              forceSelection
              placeholder="Search name, email or phone…"
              class="w-full"
              data-testid="referrer-input"
              @complete="onCustomerComplete"
            />
          </div>
          <div class="form-field full-width">
            <label for="referral-referee-name">Referred name</label>
            <InputText id="referral-referee-name" v-model="form.referee_name" maxlength="120" data-testid="referred-input" class="w-full" />
          </div>
          <div class="form-field">
            <label for="referral-referee-phone">Referred phone</label>
            <InputText id="referral-referee-phone" v-model="form.referee_phone" maxlength="30" type="tel" data-testid="referee-phone-input" class="w-full" />
          </div>
          <div class="form-field">
            <label for="referral-referee-email">Referred email (optional)</label>
            <InputText id="referral-referee-email" v-model="form.referee_email" maxlength="255" type="email" data-testid="referee-email-input" class="w-full" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showCreate = false" />
          <Button label="Save" icon="pi pi-check" :disabled="!canCreate" :loading="saving" data-testid="referral-save-btn" @click="createReferral" />
        </template>
      </Dialog>

      <!-- Edit: ReferralPatchIn takes status only, and the server refuses a
           move backwards (409), so only the current and later statuses show. -->
      <Dialog
        v-model:visible="showEdit"
        :header="editingReferral ? `Update ${editingReferral.referee_name}` : 'Update referral'"
        modal
        :style="{ width: '420px', maxWidth: '95vw' }"
        data-testid="referral-edit-dialog"
      >
        <div class="form-field">
          <label for="referral-status">Status</label>
          <Select
            id="referral-status"
            v-model="editStatus"
            :options="editStatusOptions"
            optionLabel="label"
            optionValue="value"
            class="w-full"
            data-testid="status-select"
          />
          <small v-if="editStatus === 'rewarded'" class="muted">Marking rewarded records that the reward was given. It cannot be undone.</small>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showEdit = false" />
          <Button label="Save" icon="pi pi-check" :loading="saving" data-testid="referral-status-save-btn" @click="saveStatus" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatTimestamp } from "../utils/formatTimestamp";
import AutoComplete from "primevue/autocomplete";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Select from "primevue/select";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";

// Speaks routers/referrals.py as it is. Until GDXA-1693 this page posted
// {referrer_customer, referred_name, reward, notes} (always 422), offered
// statuses the router refuses, and read columns the API never returns.
// STATUSES is the router's ALLOWED_STATUSES in its forward-only order.
const STATUSES = ["pending", "converted", "rewarded"];

const api = useApiWithToast();

const referrals = ref([]);
const loading = ref(true);
const statusFilter = ref("all");
const showCreate = ref(false);
const showEdit = ref(false);
const editingReferral = ref(null);
const editStatus = ref("pending");
const saving = ref(false);
const referrerPick = ref(null);
const customerSuggestions = ref([]);

const emptyForm = () => ({ referee_name: "", referee_phone: "", referee_email: "" });

const form = ref(emptyForm());

const canCreate = computed(() =>
  Boolean(referrerPick.value?.value)
  && form.value.referee_name.trim().length > 0
  && form.value.referee_phone.trim().length > 0
);

const editStatusOptions = computed(() => {
  const from = Math.max(0, STATUSES.indexOf(editingReferral.value?.status));
  return STATUSES.slice(from).map((value) => ({ label: statusLabel(value), value }));
});

const counts = computed(() => {
  const tally = { all: referrals.value.length };
  referrals.value.forEach((item) => {
    const key = item.status || "pending";
    tally[key] = (tally[key] || 0) + 1;
  });
  return tally;
});

const filteredReferrals = computed(() => {
  if (statusFilter.value === "all") return referrals.value;
  return referrals.value.filter((item) => item.status === statusFilter.value);
});

function statusLabel(status) {
  if (!status) return "Pending";
  return status.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function statusSeverity(status) {
  return {
    pending: "warn",
    converted: "info",
    rewarded: "success",
  }[status] || "secondary";
}

// formatDate now delegates to the shared util so PG timestamptz strings
// (space-separated, microseconds) render as a real date instead of leaking
// raw "2026-04-08 16:12:20.838053+00" into the cell. Sprint P4.2.
function formatDate(value) {
  return formatTimestamp(value, 'date');
}

// The list resolves referrer_name server-side in one query; a referrer that
// is not a live customer comes back null and shows as unknown.
async function loadReferrals() {
  loading.value = true;
  try {
    const data = await api.get("/api/referrals");
    referrals.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

async function onCustomerComplete(event) {
  const q = (event?.query || "").trim();
  if (!q) {
    customerSuggestions.value = [];
    return;
  }
  try {
    const rows = await api.get(`/api/customers/search?q=${encodeURIComponent(q)}`);
    customerSuggestions.value = (Array.isArray(rows) ? rows : []).map((c) => ({
      label: c.name || c.email || c.phone || c.id,
      value: c.id,
    }));
  } catch {
    customerSuggestions.value = [];
  }
}

function openCreate() {
  form.value = emptyForm();
  referrerPick.value = null;
  customerSuggestions.value = [];
  showCreate.value = true;
}

function openEdit(referral) {
  editingReferral.value = referral;
  editStatus.value = referral.status || "pending";
  showEdit.value = true;
}

async function createReferral() {
  if (!canCreate.value) return;
  const pick = referrerPick.value;
  const body = {
    referrer_id: pick.value,
    referee_name: form.value.referee_name.trim(),
    referee_phone: form.value.referee_phone.trim(),
  };
  const email = form.value.referee_email.trim();
  if (email) body.referee_email = email;
  saving.value = true;
  try {
    await api.post("/api/referrals", body, { successMessage: "Referral added" });
    showCreate.value = false;
    await loadReferrals();
  } catch {
    // useApiWithToast has already shown the error; keep the dialog open.
  } finally {
    saving.value = false;
  }
}

async function saveStatus() {
  const referral = editingReferral.value;
  if (!referral) return;
  if (editStatus.value === referral.status) {
    showEdit.value = false;
    return;
  }
  saving.value = true;
  try {
    await api.patch(`/api/referrals/${referral.id}`, { status: editStatus.value }, {
      successMessage: "Referral updated",
    });
    showEdit.value = false;
    await loadReferrals();
  } catch {
    // useApiWithToast has already shown the error; keep the dialog open.
  } finally {
    saving.value = false;
  }
}

onMounted(loadReferrals);
</script>

<style scoped>
.muted {
  color: var(--p-text-muted-color);
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
.form-field.full-width {
  grid-column: 1 / -1;
}
.form-field label {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--p-text-muted-color);
}
.w-full {
  width: 100%;
}
</style>

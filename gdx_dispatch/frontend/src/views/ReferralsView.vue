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
            @click="openDialog()"
          />
        </template>
      </Toolbar>

      <div class="filter-tabs" data-testid="referrals-tabs">
        <Button
          v-for="status in ['all', 'pending', 'contacted', 'converted', 'declined']"
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
            <Button label="+ Add Referral" @click="openDialog()" />
          </div>
        </template>

        <Column field="referrer_customer" header="Referrer" />
        <Column field="referred_name" header="Referred" />
        <Column field="status" header="Status" style="width:170px">
          <template #body="{ data }">
            <Tag :value="statusLabel(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="reward" header="Reward" style="width:130px" sortable>
          <template #body="{ data }">
            {{ formatCurrency(data.reward) }}
          </template>
        </Column>
        <Column field="created_at" header="Created" style="width:120px">
          <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
        </Column>
        <Column header="Actions" style="width:120px">
          <template #body="{ data }">
            <Button
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              data-testid="referral-edit-btn"
              @click.stop="openDialog(data)"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="editingReferral ? `Update ${editingReferral.referred_name}` : 'New Referral'"
        modal
        :style="{ width: '520px' }"
        data-testid="referral-dialog"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label>Referrer</label>
            <InputText v-model="form.referrer_customer" data-testid="referrer-input" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Referred</label>
            <InputText v-model="form.referred_name" data-testid="referred-input" class="w-full" />
          </div>
          <div class="form-field">
            <label>Status</label>
            <Select
              v-model="form.status"
              :options="statusOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="status-select"
            />
          </div>
          <div class="form-field">
            <label>Reward</label>
            <InputNumber
              v-model="form.reward"
              mode="currency"
              currency="USD"
              :min="0"
              data-testid="reward-input"
              class="w-full"
            />
          </div>
          <div class="form-field full-width">
            <label>Notes</label>
            <Textarea
              v-model="form.notes"
              rows="3"
              class="w-full"
              placeholder="Capture any context or follow-up notes."
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button label="Save" icon="pi pi-check" @click="saveReferral" :loading="saving" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatTimestamp } from "../utils/formatTimestamp";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Select from "primevue/select";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const referrals = ref([]);
const loading = ref(true);
const statusFilter = ref("all");
const showDialog = ref(false);
const editingReferral = ref(null);
const saving = ref(false);

const statusOptions = [
  { label: "Pending", value: "pending" },
  { label: "Contacted", value: "contacted" },
  { label: "Converted", value: "converted" },
  { label: "Declined", value: "declined" },
];

const emptyForm = () => ({
  referrer_customer: "",
  referred_name: "",
  status: "pending",
  reward: null,
  notes: "",
});

const form = ref(emptyForm());

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
    pending: "warning",
    contacted: "info",
    converted: "success",
    declined: "danger",
  }[status] || "secondary";
}

function formatCurrency(value) {
  if (value == null || value === "") return "—";
  return `$${Number(value).toFixed(2)}`;
}

// formatDate now delegates to the shared util so PG timestamptz strings
// (space-separated, microseconds) render as a real date instead of leaking
// raw "2026-04-08 16:12:20.838053+00" into the cell. Sprint P4.2.
function formatDate(value) {
  return formatTimestamp(value, 'date');
}

async function loadReferrals() {
  loading.value = true;
  try {
    const data = await api.get("/api/referrals");
    referrals.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

function openDialog(referral = null) {
  editingReferral.value = referral;
  form.value = referral ? { ...referral } : emptyForm();
  showDialog.value = true;
}

async function saveReferral() {
  if (!form.value.referrer_customer.trim() || !form.value.referred_name.trim()) {
    return;
  }
  saving.value = true;
  try {
    if (editingReferral.value) {
      await api.patch(`/api/referrals/${editingReferral.value.id}`, form.value, {
        successMessage: "Referral updated",
      });
    } else {
      await api.post("/api/referrals", form.value, { successMessage: "Referral added" });
    }
    await loadReferrals();
    showDialog.value = false;
  } finally {
    saving.value = false;
  }
}

onMounted(loadReferrals);
</script>

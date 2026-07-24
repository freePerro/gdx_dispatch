<template>
    <section class="commissions-view view-card">
      <div class="page-header">
        <h2>Commission Tracking</h2>
      </div>

      <!-- Tabs -->
      <div class="status-tabs">
        <Button
          :label="`Rates`"
          :severity="activeTab === 'rates' ? undefined : 'secondary'"
          size="small"
          data-testid="tab-rates"
          @click="activeTab = 'rates'"
        />
        <Button
          :label="`Summary`"
          :severity="activeTab === 'summary' ? undefined : 'secondary'"
          size="small"
          data-testid="tab-summary"
          @click="activeTab = 'summary'; fetchSummary()"
        />
      </div>

      <!-- RATES TAB — commission rules are per ROLE, not per technician:
           the backend upserts by role and the calculate flow looks rules up
           by role (routers/commission.py). This view used to render a
           per-user contract (user_id/parts_rate/…) that no endpoint ever
           served, so every column was blank and every save was dropped. -->
      <div v-if="activeTab === 'rates'">
        <div class="toolbar">
          <span></span>
          <Button label="Add Rule" icon="pi pi-plus" data-testid="add-rate-btn" @click="openCreateDialog" />
        </div>

        <EmptyState
          v-if="!loading && !rates.length"
          icon="pi pi-percentage"
          title="No commission rules yet"
          message="Set up commission rules per role to start tracking earnings. Click Add Rule above to get started."
        />

        <DataTable
      responsiveLayout="scroll" v-else :value="rates" :loading="loading" stripedRows data-testid="rates-table">
          <template #empty>
            <EmptyState icon="pi pi-percentage" title="No commission rules" message="Click Add Rule above to create one." />
          </template>
          <Column field="role" header="Role" sortable />
          <Column field="parts_pct" header="Parts %" sortable>
            <template #body="{ data }">{{ pct(data.parts_pct) }}</template>
          </Column>
          <Column field="labor_pct" header="Labor %" sortable>
            <template #body="{ data }">{{ pct(data.labor_pct) }}</template>
          </Column>
          <Column field="bonus_per_review" header="Bonus / Review" sortable>
            <template #body="{ data }">{{ currency(data.bonus_per_review) }}</template>
          </Column>
          <Column field="updated_at" header="Updated" sortable>
            <template #body="{ data }">{{ shortDate(data.updated_at) }}</template>
          </Column>
          <Column header="Actions" style="width: 6rem">
            <template #body="{ data }">
              <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" class="p-button-rounded p-button-text" data-testid="edit-rate-btn" @click="openEditDialog(data)" />
            </template>
          </Column>
        </DataTable>
      </div>

      <!-- SUMMARY TAB — the endpoint aggregates by period (YYYY-MM), not by
           arbitrary date range, and returns total_parts/total_labor/
           total_bonus/grand_total/entry_count keyed by user_id. -->
      <div v-if="activeTab === 'summary'">
        <div class="toolbar">
          <div class="flex align-items-center gap-2">
            <DatePicker
              v-model="summaryMonth"
              view="month"
              dateFormat="yy-mm"
              showIcon
              placeholder="Month"
              data-testid="summary-month"
            />
            <Button label="Load" icon="pi pi-search" data-testid="filter-summary-btn" @click="fetchSummary" />
          </div>
        </div>

        <EmptyState
          v-if="!loading && !summaryData.length"
          icon="pi pi-dollar"
          title="No commission data yet"
          message="Commission totals appear once entries are calculated for the selected month. Try another month above."
        />

        <DataTable
      responsiveLayout="scroll" v-else :value="summaryData" :loading="loading" stripedRows data-testid="summary-table">
          <template #empty>
            <EmptyState icon="pi pi-dollar" title="No commission data" message="Try another month above." />
          </template>
          <Column field="user_name" header="Technician" sortable />
          <Column field="total_parts" header="Parts Total" sortable>
            <template #body="{ data }">{{ currency(data.total_parts) }}</template>
          </Column>
          <Column field="total_labor" header="Labor Total" sortable>
            <template #body="{ data }">{{ currency(data.total_labor) }}</template>
          </Column>
          <Column field="total_bonus" header="Bonus" sortable>
            <template #body="{ data }">{{ currency(data.total_bonus) }}</template>
          </Column>
          <Column field="entry_count" header="Entries" sortable />
          <Column field="grand_total" header="Total" sortable>
            <template #body="{ data }">
              <strong>{{ currency(data.grand_total) }}</strong>
            </template>
          </Column>
        </DataTable>
      </div>

      <!-- Rule Dialog -->
      <Dialog v-model:visible="dialogVisible" :header="isEditing ? 'Edit Rule' : 'New Rule'" :style="{ width: '420px' }" modal>
        <div class="flex flex-column gap-3 mt-2">
          <div class="flex flex-column gap-1">
            <label>Role</label>
            <InputText v-model="form.role" placeholder="e.g. technician, installer" data-testid="input-role" />
          </div>
          <div class="flex flex-column gap-1">
            <label>Parts Commission (%)</label>
            <InputNumber v-model="form.parts_pct" :minFractionDigits="1" :maxFractionDigits="2" :min="0" :max="100" data-testid="input-parts-rate" />
          </div>
          <div class="flex flex-column gap-1">
            <label>Labor Commission (%)</label>
            <InputNumber v-model="form.labor_pct" :minFractionDigits="1" :maxFractionDigits="2" :min="0" :max="100" data-testid="input-labor-rate" />
          </div>
          <div class="flex flex-column gap-1">
            <label>Bonus per Review ($)</label>
            <InputNumber v-model="form.bonus_per_review" mode="currency" currency="USD" :min="0" data-testid="input-bonus-per-review" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="dialogVisible = false" />
          <Button label="Save" icon="pi pi-check" data-testid="save-rate-btn" @click="saveRate" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import DatePicker from "primevue/datepicker";
import Dialog from "primevue/dialog";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import EmptyState from "../components/EmptyState.vue";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import { formatMoney, formatPercent } from "../composables/useFormatters";

const toast = useToast();
const api = useApi();
const loading = ref(false);
const activeTab = ref("rates");
const rates = ref([]);
const summaryData = ref([]);
const dialogVisible = ref(false);
const isEditing = ref(false);
const editingId = ref(null);

const summaryMonth = ref(new Date());

const emptyForm = () => ({ role: "", parts_pct: 0, labor_pct: 0, bonus_per_review: 0 });
const form = ref(emptyForm());

// user_id → display name, resolved from the technicians list when the
// caller's role can read it; falls back to the raw id.
const techNames = ref({});

function currency(v) {
  return formatMoney(Number(v) || 0);
}
function pct(v) {
  return formatPercent(Number(v) || 0, { whole: true });
}
function shortDate(d) {
  if (!d) return "";
  const parsed = new Date(d);
  return Number.isNaN(parsed.getTime()) ? String(d).slice(0, 10) : parsed.toLocaleDateString();
}
function fmtPeriod(d) {
  const date = d instanceof Date ? d : new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

async function fetchRates() {
  loading.value = true;
  try {
    const r = await api.get("/api/commissions/rules");
    rates.value = Array.isArray(r) ? r : r?.items || [];
  } catch (e) {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to load commission rules", life: 4000 });
  } finally {
    loading.value = false;
  }
}

async function resolveTechNames() {
  try {
    const r = await api.get("/api/technicians", { suppressErrorToast: true });
    const list = Array.isArray(r) ? r : r?.items || [];
    const map = {};
    for (const t of list) {
      const key = t.user_id || t.id;
      if (key) map[key] = t.name || t.user_name || "";
    }
    techNames.value = map;
  } catch {
    // Not every role can read technicians — ids render as-is.
  }
}

async function fetchSummary() {
  loading.value = true;
  try {
    const r = await api.get(`/api/commissions/summary?period=${fmtPeriod(summaryMonth.value)}`);
    const list = Array.isArray(r) ? r : r?.items || [];
    summaryData.value = list.map((row) => ({
      ...row,
      user_name: techNames.value[row.user_id] || row.user_id,
    }));
  } catch (e) {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to load commission summary", life: 4000 });
  } finally {
    loading.value = false;
  }
}

function openCreateDialog() {
  isEditing.value = false;
  editingId.value = null;
  form.value = emptyForm();
  dialogVisible.value = true;
}

function openEditDialog(rule) {
  isEditing.value = true;
  editingId.value = rule.id;
  form.value = {
    role: rule.role || "",
    parts_pct: Number(rule.parts_pct) || 0,
    labor_pct: Number(rule.labor_pct) || 0,
    bonus_per_review: Number(rule.bonus_per_review) || 0,
  };
  dialogVisible.value = true;
}

async function saveRate() {
  if (!form.value.role?.trim()) {
    toast.add({ severity: "warn", summary: "Role required", detail: "Enter the role this rule applies to", life: 3000 });
    return;
  }
  const payload = {
    role: form.value.role.trim(),
    parts_pct: Number(form.value.parts_pct) || 0,
    labor_pct: Number(form.value.labor_pct) || 0,
    bonus_per_review: Number(form.value.bonus_per_review) || 0,
  };
  try {
    if (isEditing.value) {
      await api.put(`/api/commissions/rules/${editingId.value}`, payload, {
        successMessage: "Rule updated",
        suppressErrorToast: true,
      });
    } else {
      await api.post("/api/commissions/rules", payload, {
        successMessage: "Rule created",
        suppressErrorToast: true,
      });
    }
    dialogVisible.value = false;
    await fetchRates();
  } catch (e) {
    // Surface the server's detail (e.g. 409 "A rule for role … already exists")
    toast.add({ severity: "error", summary: "Save failed", detail: e?.message || "Failed to save rule", life: 4000 });
  }
}

onMounted(() => {
  fetchRates();
  resolveTechNames();
});
</script>

<style scoped>
.commissions-view { padding: 1.5rem; }
.page-header { margin-bottom: 1rem; }
.page-header h2 { margin: 0; }
.status-tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
</style>

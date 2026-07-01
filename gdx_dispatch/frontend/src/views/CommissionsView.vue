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

      <!-- RATES TAB -->
      <div v-if="activeTab === 'rates'">
        <div class="toolbar">
          <span></span>
          <Button label="Add Rate" icon="pi pi-plus" data-testid="add-rate-btn" @click="openCreateDialog" />
        </div>

        <EmptyState
          v-if="!loading && !rates.length"
          icon="pi pi-percentage"
          title="No commission rates yet"
          message="Set up commission rates for your team to start tracking earnings. Click Add Rate above to get started."
        />

        <DataTable
      responsiveLayout="scroll" v-else :value="rates" :loading="loading" stripedRows data-testid="rates-table">
          <template #empty>
            <EmptyState icon="pi pi-percentage" title="No commission rates" message="Click Add Rate above to create one." />
          </template>
          <Column field="user_name" header="Technician" sortable />
          <Column field="parts_rate" header="Parts %" sortable>
            <template #body="{ data }">{{ pct(data.parts_rate) }}</template>
          </Column>
          <Column field="labor_rate" header="Labor %" sortable>
            <template #body="{ data }">{{ pct(data.labor_rate) }}</template>
          </Column>
          <Column field="bonus_threshold" header="Bonus Threshold" sortable>
            <template #body="{ data }">{{ currency(data.bonus_threshold) }}</template>
          </Column>
          <Column field="bonus_rate" header="Bonus %" sortable>
            <template #body="{ data }">{{ pct(data.bonus_rate) }}</template>
          </Column>
          <Column field="effective_date" header="Effective Date" sortable />
          <Column header="Actions" style="width: 6rem">
            <template #body="{ data }">
              <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" class="p-button-rounded p-button-text" data-testid="edit-rate-btn" @click="openEditDialog(data)" />
            </template>
          </Column>
        </DataTable>
      </div>

      <!-- SUMMARY TAB -->
      <div v-if="activeTab === 'summary'">
        <div class="toolbar">
          <div class="flex align-items-center gap-2">
            <DatePicker v-model="startDate" dateFormat="yy-mm-dd" showIcon placeholder="Start" data-testid="summary-start" />
            <DatePicker v-model="endDate" dateFormat="yy-mm-dd" showIcon placeholder="End" data-testid="summary-end" />
            <Button label="Filter" icon="pi pi-search" data-testid="filter-summary-btn" @click="fetchSummary" />
          </div>
        </div>

        <EmptyState
          v-if="!loading && !summaryData.length"
          icon="pi pi-dollar"
          title="No commission data yet"
          message="Commission totals appear once technicians complete jobs in the selected date range. Try widening the dates above."
        />

        <DataTable
      responsiveLayout="scroll" v-else :value="summaryData" :loading="loading" stripedRows data-testid="summary-table">
          <template #empty>
            <EmptyState icon="pi pi-dollar" title="No commission data" message="Try widening the date range above." />
          </template>
          <Column field="user_name" header="Technician" sortable />
          <Column field="parts_total" header="Parts Total" sortable>
            <template #body="{ data }">{{ currency(data.parts_total) }}</template>
          </Column>
          <Column field="labor_total" header="Labor Total" sortable>
            <template #body="{ data }">{{ currency(data.labor_total) }}</template>
          </Column>
          <Column field="parts_commission" header="Parts Comm." sortable>
            <template #body="{ data }">{{ currency(data.parts_commission) }}</template>
          </Column>
          <Column field="labor_commission" header="Labor Comm." sortable>
            <template #body="{ data }">{{ currency(data.labor_commission) }}</template>
          </Column>
          <Column field="bonus_earned" header="Bonus" sortable>
            <template #body="{ data }">{{ currency(data.bonus_earned) }}</template>
          </Column>
          <Column field="total_commission" header="Total" sortable>
            <template #body="{ data }">
              <strong>{{ currency(data.total_commission) }}</strong>
            </template>
          </Column>
        </DataTable>
      </div>

      <!-- Rate Dialog -->
      <Dialog v-model:visible="dialogVisible" :header="isEditing ? 'Edit Rate' : 'New Rate'" :style="{ width: '420px' }" modal>
        <div class="flex flex-column gap-3 mt-2">
          <div class="flex flex-column gap-1">
            <label>User ID</label>
            <InputText v-model="form.user_id" data-testid="input-user-id" />
          </div>
          <div class="flex flex-column gap-1">
            <label>Parts Rate (%)</label>
            <InputNumber v-model="form.parts_rate" :minFractionDigits="1" :maxFractionDigits="2" data-testid="input-parts-rate" />
          </div>
          <div class="flex flex-column gap-1">
            <label>Labor Rate (%)</label>
            <InputNumber v-model="form.labor_rate" :minFractionDigits="1" :maxFractionDigits="2" data-testid="input-labor-rate" />
          </div>
          <div class="flex flex-column gap-1">
            <label>Bonus Threshold ($)</label>
            <InputNumber v-model="form.bonus_threshold" mode="currency" currency="USD" data-testid="input-bonus-threshold" />
          </div>
          <div class="flex flex-column gap-1">
            <label>Bonus Rate (%)</label>
            <InputNumber v-model="form.bonus_rate" :minFractionDigits="1" :maxFractionDigits="2" data-testid="input-bonus-rate" />
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

const toast = useToast();
const api = useApi();
const loading = ref(false);
const activeTab = ref("rates");
const rates = ref([]);
const summaryData = ref([]);
const dialogVisible = ref(false);
const isEditing = ref(false);
const editingId = ref(null);

const now = new Date();
const startDate = ref(new Date(now.getFullYear(), now.getMonth(), 1));
const endDate = ref(new Date());

const form = ref({ user_id: "", parts_rate: 0, labor_rate: 0, bonus_threshold: 0, bonus_rate: 0 });

function currency(v) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(v) || 0);
}
function pct(v) {
  return `${Number(v || 0).toFixed(1)}%`;
}
function fmtDate(d) {
  if (!d) return "";
  return d instanceof Date ? d.toISOString().slice(0, 10) : d;
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

async function fetchSummary() {
  loading.value = true;
  try {
    const s = fmtDate(startDate.value);
    const e = fmtDate(endDate.value);
    const r = await api.get(`/api/commissions/summary?start=${s}&end=${e}`);
    summaryData.value = Array.isArray(r) ? r : r?.items || [];
  } catch (e) {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to load commission summary", life: 4000 });
  } finally {
    loading.value = false;
  }
}

function openCreateDialog() {
  isEditing.value = false;
  editingId.value = null;
  form.value = { user_id: "", parts_rate: 0, labor_rate: 0, bonus_threshold: 0, bonus_rate: 0 };
  dialogVisible.value = true;
}

function openEditDialog(rate) {
  isEditing.value = true;
  editingId.value = rate.id;
  form.value = { ...rate };
  dialogVisible.value = true;
}

async function saveRate() {
  try {
    if (isEditing.value) {
      await api.put(`/api/commissions/rules/${editingId.value}`, { ...form.value });
      toast.add({ severity: "success", summary: "Updated", detail: "Rate updated", life: 3000 });
    } else {
      await api.post("/api/commissions/rules", { ...form.value });
      toast.add({ severity: "success", summary: "Created", detail: "Rate created", life: 3000 });
    }
    dialogVisible.value = false;
    await fetchRates();
  } catch (e) {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to save rate", life: 4000 });
  }
}

onMounted(() => {
  fetchRates();
});
</script>

<style scoped>
.commissions-view { padding: 1.5rem; }
.page-header { margin-bottom: 1rem; }
.page-header h2 { margin: 0; }
.status-tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
</style>

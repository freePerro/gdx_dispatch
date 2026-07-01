<!--
  Payroll — UX audit F-82 / 2026-04-29.
  External-first: tenants type entries here (or import via CSV/integration
  later). Job costing reads these to compute *true* labor cost vs the
  *estimated* rate on Technician.hourly_rate.
-->
<template>
    <section class="payroll-view view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Payroll Entries</h1>
          <Tag :value="config.payroll_source || 'manual'" severity="info" style="margin-left:0.75rem" />
        </template>
        <template #end>
          <Select
            v-model="config.payroll_source"
            :options="(config.candidates || ['manual'])"
            class="filter-select"
            @change="saveSource"
          />
          <Button label="Refresh" icon="pi pi-refresh" severity="secondary" @click="fetchAll" />
          <Button label="+ New Entry" icon="pi pi-plus" @click="showForm = true" />
        </template>
      </Toolbar>

      <p class="muted">
        Job costing prefers the rate computed from these entries (gross pay / hours paid)
        over the per-tech estimated rate. Integration adapters (Gusto, QBO Payroll) are planned.
      </p>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <DataTable :value="items" stripedRows responsiveLayout="scroll" :paginator="true" :rows="50">
        <template #empty><div class="empty-message">No payroll entries yet.</div></template>
        <Column header="Period">
          <template #body="{ data }">
            {{ formatDate(data.period_start) }} → {{ formatDate(data.period_end) }}
          </template>
        </Column>
        <Column field="tech_user_id" header="Tech (user_id)" />
        <Column field="hours_paid" header="Hours" />
        <Column header="Gross">
          <template #body="{ data }">${{ Number(data.gross_pay).toFixed(2) }}</template>
        </Column>
        <Column header="Effective rate">
          <template #body="{ data }">
            <span v-if="Number(data.hours_paid) > 0">
              ${{ (Number(data.gross_pay) / Number(data.hours_paid)).toFixed(2) }}/hr
            </span>
          </template>
        </Column>
        <Column field="source" header="Source" />
        <Column header="">
          <template #body="{ data }">
            <Button v-tooltip="'Delete'" icon="pi pi-trash" severity="danger" text @click="del(data.id)" />
          </template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showForm" header="New payroll entry" modal style="width:500px">
        <div class="form-grid">
          <div class="form-field"><label>Tech user_id</label>
            <InputText v-model="form.tech_user_id" />
          </div>
          <div class="form-field"><label>Period start</label>
            <Calendar v-model="form.period_start" showTime hourFormat="12" />
          </div>
          <div class="form-field"><label>Period end</label>
            <Calendar v-model="form.period_end" showTime hourFormat="12" />
          </div>
          <div class="form-field"><label>Hours paid</label>
            <InputNumber v-model="form.hours_paid" :minFractionDigits="2" :maxFractionDigits="2" :min="0" />
          </div>
          <div class="form-field"><label>Gross pay ($)</label>
            <InputNumber v-model="form.gross_pay" mode="currency" currency="USD" :min="0" />
          </div>
          <div class="form-field"><label>Notes</label>
            <Textarea v-model="form.notes" rows="2" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showForm = false" />
          <Button label="Save" icon="pi pi-save" :loading="saving" :disabled="!canSave" @click="save" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { useApiWithToast as useApi } from "../../composables/useApiWithToast";
import Button from "primevue/button";
import Calendar from "primevue/calendar";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import Select from "primevue/select";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";

const api = useApi();
const items = ref([]);
const config = reactive({ payroll_source: "manual", candidates: ["manual"] });
const error = ref("");
const showForm = ref(false);
const saving = ref(false);
const form = reactive({
  tech_user_id: "", period_start: null, period_end: null,
  hours_paid: null, gross_pay: null, notes: "",
});

const canSave = computed(() =>
  form.tech_user_id && form.period_start && form.period_end &&
  form.hours_paid != null && form.gross_pay != null,
);

function formatDate(v) {
  if (!v) return "—";
  const d = new Date(typeof v === "string" ? v.replace(" ", "T") : v);
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleDateString();
}

async function fetchEntries() {
  try {
    const r = await api.get("/api/payroll/entries");
    items.value = r?.items || [];
  } catch (e) { error.value = e?.message || "Failed to load entries"; }
}
async function fetchConfig() {
  try {
    const c = await api.get("/api/payroll/config");
    Object.assign(config, c);
  } catch (_e) { /* ignore */ }
}
async function fetchAll() { await Promise.allSettled([fetchEntries(), fetchConfig()]); }

async function save() {
  saving.value = true;
  try {
    await api.post("/api/payroll/entries", { ...form }, { successMessage: "Entry saved" });
    showForm.value = false;
    Object.assign(form, { tech_user_id: "", period_start: null, period_end: null, hours_paid: null, gross_pay: null, notes: "" });
    await fetchEntries();
  } catch (e) { error.value = e?.message || "Save failed"; }
  finally { saving.value = false; }
}

async function del(id) {
  try {
    await api.delete(`/api/payroll/entries/${id}`, {}, { successMessage: "Entry deleted" });
    await fetchEntries();
  } catch (e) { error.value = e?.message || "Delete failed"; }
}

async function saveSource() {
  try {
    await api.patch("/api/payroll/config", { payroll_source: config.payroll_source });
  } catch (_e) { /* ignore */ }
}

onMounted(fetchAll);
</script>

<style scoped>
.form-grid { display: grid; gap: 0.5rem; }
.form-field { display: grid; gap: 0.25rem; }
</style>

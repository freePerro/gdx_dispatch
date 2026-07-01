<!--
  Labor Pricing Matrix — Sprint S97 slice 3.

  Tenant-configurable size/SKU-keyed flat-rate labor pricing. Each row
  carries a flat price and an assumed *man-hours* budget. Backend at
  /api/labor-pricing/items (gdx/routers/labor_pricing_admin.py).

  Permissions: pricing.labor_matrix.read (view) / .write (edit).
-->
<template>
    <section class="labor-matrix-view view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Labor Pricing Matrix</h1>
        </template>
        <template #end>
          <Select
            v-model="serviceFilter"
            :options="serviceOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="All services"
            class="filter-select"
            data-testid="labor-matrix-service-filter"
          />
          <Select
            v-model="activeFilter"
            :options="activeOptions"
            optionLabel="label"
            optionValue="value"
            class="filter-select"
            data-testid="labor-matrix-active-filter"
          />
          <Button
            label="Refresh"
            icon="pi pi-refresh"
            severity="secondary"
            @click="fetchItems"
          />
          <Button
            label="+ New Row"
            icon="pi pi-plus"
            data-testid="labor-matrix-new-btn"
            @click="openCreate"
          />
        </template>
      </Toolbar>

      <p class="hint">
        Flat-rate labor priced by size or SKU. Each row's <strong>assumed man-hours</strong>
        feeds scheduler block sizing (man-hours ÷ crew size) and job-costing variance.
        Door removal, springs, opener installs are separate rows. No materials.
      </p>

      <div v-if="error" class="error-banner">{{ error }}</div>
      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
        v-else
        :value="filteredItems"
        :paginator="true"
        :rows="25"
        stripedRows
        responsiveLayout="scroll"
        
        data-testid="labor-matrix-table"
        @row-click="(e) => openEdit(e.data)"
      >
        <template #empty>
          <div class="empty-message">
            No labor matrix rows yet. Click <strong>+ New Row</strong> to add the first one
            (e.g. 10x8 Install, $500, 5 man-hours).
          </div>
        </template>

        <Column field="service_type" header="Service" style="width: 130px" sortable />
        <Column header="Size" style="width: 110px">
          <template #body="{ data }">
            <span v-if="data.width_ft && data.height_ft">{{ data.width_ft }}×{{ data.height_ft }}</span>
            <span v-else class="muted">—</span>
          </template>
        </Column>
        <Column field="sku" header="SKU" style="width: 140px">
          <template #body="{ data }"><span :class="{ muted: !data.sku }">{{ data.sku || '—' }}</span></template>
        </Column>
        <Column field="description" header="Description" />
        <Column field="flat_price" header="Price" style="width: 110px" sortable>
          <template #body="{ data }">{{ formatCurrency(data.flat_price) }}</template>
        </Column>
        <Column field="assumed_man_hours" header="Man-hrs" style="width: 100px" sortable>
          <template #body="{ data }">{{ data.assumed_man_hours }}</template>
        </Column>
        <Column header="Implied $/hr" style="width: 130px">
          <template #body="{ data }">
            <span v-if="data.implied_hourly_rate !== null">{{ formatCurrency(data.implied_hourly_rate) }}/hr</span>
            <span v-else class="muted">—</span>
          </template>
        </Column>
        <Column header="Status" style="width: 100px">
          <template #body="{ data }">
            <Tag
              :value="data.active ? 'Active' : 'Archived'"
              :severity="data.active ? 'success' : 'warning'"
            />
          </template>
        </Column>
        <Column header="" style="width: 80px">
          <template #body="{ data }">
            <Button
              v-tooltip="'Edit'"
              icon="pi pi-pencil"
              text size="small"
              aria-label="Edit"
              data-testid="labor-matrix-row-edit-btn"
              @click.stop="openEdit(data)"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="editingId ? 'Edit Labor Row' : 'New Labor Row'"
        modal
        :style="{ width: '560px' }"
        data-testid="labor-matrix-dialog"
      >
        <div class="form-grid">
          <div class="form-row">
            <label>Service Type *</label>
            <InputText
              v-model="form.service_type"
              placeholder="install / removal / repair / spring / opener"
              data-testid="labor-matrix-service"
            />
          </div>
          <div class="form-row">
            <label>Description *</label>
            <InputText v-model="form.description" placeholder='e.g. "10x8 Sectional Install"' />
          </div>
          <div class="form-row two-col">
            <div>
              <label>Width (in)</label>
              <InputNumber v-model="form.width_ft" :min="1" :max="999" />
            </div>
            <div>
              <label>Height (in)</label>
              <InputNumber v-model="form.height_ft" :min="1" :max="999" />
            </div>
          </div>
          <div class="form-row">
            <label>SKU (optional)</label>
            <InputText v-model="form.sku" placeholder="leave blank if size-keyed" />
          </div>
          <div class="form-row two-col">
            <div>
              <label>Flat Price ($) *</label>
              <InputNumber v-model="form.flat_price" :min="0" mode="currency" currency="USD" />
            </div>
            <div>
              <label>Assumed Man-Hours *</label>
              <InputNumber v-model="form.assumed_man_hours" :min="0" :minFractionDigits="2" :maxFractionDigits="2" />
            </div>
          </div>
          <div class="form-row">
            <small v-if="impliedRate !== null" class="hint">
              Implied rate at this configuration: <strong>{{ formatCurrency(impliedRate) }}/man-hr</strong>
            </small>
          </div>
          <div class="form-row">
            <label>Notes</label>
            <Textarea v-model="form.notes" rows="2" />
          </div>
          <div class="form-row inline">
            <Checkbox v-model="form.active" :binary="true" inputId="active-cb" />
            <label for="active-cb">Active (available on estimates)</label>
          </div>
        </div>

        <template #footer>
          <Button
            v-if="editingId"
            label="Archive"
            icon="pi pi-archive"
            severity="warn"
            text
            data-testid="labor-matrix-archive-btn"
            @click="archive"
          />
          <Button label="Cancel" severity="secondary" text @click="showDialog = false" />
          <Button
            label="Save"
            icon="pi pi-check"
            :loading="saving"
            :disabled="!canSave"
            data-testid="labor-matrix-save-btn"
            @click="save"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import Button from 'primevue/button';
import Checkbox from 'primevue/checkbox';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import Toolbar from 'primevue/toolbar';
import { useApiWithToast } from '../composables/useApiWithToast';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const items = ref([]);
const loading = ref(false);
const error = ref('');
const saving = ref(false);

const showDialog = ref(false);
const editingId = ref(null);
const form = ref(emptyForm());

const serviceFilter = ref(null);
const activeFilter = ref(true);

const activeOptions = [
  { label: 'Active only', value: true },
  { label: 'Archived only', value: false },
  { label: 'All', value: null },
];

const serviceOptions = computed(() => {
  const seen = new Set(items.value.map((r) => r.service_type).filter(Boolean));
  return [
    { label: 'All services', value: null },
    ...[...seen].sort().map((s) => ({ label: s, value: s })),
  ];
});

const filteredItems = computed(() => {
  return items.value.filter((r) => {
    if (activeFilter.value !== null && r.active !== activeFilter.value) return false;
    if (serviceFilter.value && r.service_type !== serviceFilter.value) return false;
    return true;
  });
});

const impliedRate = computed(() => {
  const p = Number(form.value.flat_price) || 0;
  const h = Number(form.value.assumed_man_hours) || 0;
  return h > 0 ? Math.round((p / h) * 100) / 100 : null;
});

const canSave = computed(() => {
  return (
    form.value.service_type?.trim() &&
    form.value.description?.trim() &&
    form.value.flat_price !== null && form.value.flat_price >= 0 &&
    form.value.assumed_man_hours !== null && form.value.assumed_man_hours >= 0 &&
    (form.value.width_ft == null) === (form.value.height_ft == null)
  );
});

function emptyForm() {
  return {
    sku: '',
    description: '',
    service_type: 'install',
    width_ft: null,
    height_ft: null,
    flat_price: 0,
    assumed_man_hours: 0,
    notes: '',
    active: true,
    sort_order: 0,
  };
}

function formatCurrency(n) {
  if (n == null) return '—';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);
}

async function fetchItems() {
  loading.value = true;
  error.value = '';
  try {
    items.value = await api.get('/api/labor-pricing/items');
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || 'Failed to load labor matrix';
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingId.value = null;
  form.value = emptyForm();
  showDialog.value = true;
}

function openEdit(row) {
  editingId.value = row.id;
  form.value = {
    sku: row.sku || '',
    description: row.description || '',
    service_type: row.service_type || 'install',
    width_ft: row.width_ft,
    height_ft: row.height_ft,
    flat_price: Number(row.flat_price) || 0,
    assumed_man_hours: Number(row.assumed_man_hours) || 0,
    notes: row.notes || '',
    active: !!row.active,
    sort_order: row.sort_order || 0,
  };
  showDialog.value = true;
}

async function save() {
  if (!canSave.value) return;
  saving.value = true;
  try {
    const payload = {
      sku: form.value.sku || null,
      description: form.value.description.trim(),
      service_type: form.value.service_type.trim(),
      width_ft: form.value.width_ft,
      height_ft: form.value.height_ft,
      flat_price: Number(form.value.flat_price),
      assumed_man_hours: Number(form.value.assumed_man_hours),
      notes: form.value.notes || null,
      active: form.value.active,
      sort_order: form.value.sort_order || 0,
    };
    if (editingId.value) {
      await api.put(`/api/labor-pricing/items/${editingId.value}`, payload);
    } else {
      await api.post('/api/labor-pricing/items', payload);
    }
    showDialog.value = false;
    await fetchItems();
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || 'Save failed';
  } finally {
    saving.value = false;
  }
}

async function archive() {
  if (!editingId.value) return;
  if (!(await confirmAsync({ header: 'Confirm', message: 'Archive this row? It stays in the database for historical estimates but won\'t appear on new ones.' }))) return;
  saving.value = true;
  try {
    await api.delete(`/api/labor-pricing/items/${editingId.value}`);
    showDialog.value = false;
    await fetchItems();
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || 'Archive failed';
  } finally {
    saving.value = false;
  }
}

onMounted(fetchItems);
</script>

<style scoped>
.labor-matrix-view { padding: 1rem; }
.view-heading { margin: 0; font-size: 1.4rem; }
.hint { color: var(--text-muted, #666); margin: 0.5rem 0 1rem; font-size: 0.9rem; }
.error-banner {
  background: var(--p-content-hover-background);
  color: var(--error, #b91c1c);
  padding: 0.75rem;
  border-radius: 4px;
  margin-bottom: 1rem;
}
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.empty-message { text-align: center; padding: 2rem; color: var(--text-muted, #888); }
.muted { color: var(--text-muted, #999); }
.filter-select { min-width: 160px; }
.row-clickable :deep(tbody tr) { cursor: pointer; }

.form-grid { display: flex; flex-direction: column; gap: 0.85rem; }
.form-row { display: flex; flex-direction: column; gap: 0.35rem; }
.form-row label { font-size: 0.85rem; font-weight: 500; }
.form-row.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
.form-row.inline { flex-direction: row; align-items: center; gap: 0.5rem; }
</style>

<template>
    <section class="pricing-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Pricing</h2>
        </template>
        <template #end>
          <Button
            label="+ Add Price Book Entry"
            icon="pi pi-plus"
            class="p-button-outlined"
            data-testid="pricing-add-btn"
            @click="openDialog()"
          />
        </template>
      </Toolbar>

      <div class="filter-tabs" data-testid="pricing-tabs">
        <Button
          v-for="tab in pricingTabs"
          :key="tab"
          :label="tabLabel(tab)"
          :severity="statusFilter === tab ? undefined : 'secondary'"
          size="small"
          class="p-button-text"
          :data-testid="`pricing-tab-${tab}`"
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
        :rows="15"
        striped-rows
        class="clickable-row"
        data-testid="pricing-table"
      >
        <Column field="sku" header="SKU" :style="{ minWidth: '120px' }" />
        <Column field="name" header="Name" />
        <Column header="Category" :style="{ minWidth: '160px' }">
          <template #body="{ data }">{{ data.category || 'Uncategorized' }}</template>
        </Column>
        <Column field="unit_price" header="Unit Price" :style="{ width: '140px' }" sortable>
          <template #body="{ data }">{{ formatCurrency(data.unit_price) }}</template>
        </Column>
        <Column field="cost" header="Cost" :style="{ width: '140px' }">
          <template #body="{ data }">{{ formatCurrency(data.cost) }}</template>
        </Column>
        <Column field="margin_pct" header="Margin" :style="{ width: '120px' }">
          <template #body="{ data }">{{ formatPercent(data.margin_pct) }}</template>
        </Column>
        <Column header="Active" :style="{ width: '120px' }">
          <template #body="{ data }">
            <Tag :value="data.active ? 'Active' : 'Archived'" :severity="data.active ? 'success' : 'warning'" />
          </template>
        </Column>
        <Column header="Actions" :style="{ width: '140px' }">
          <template #body="{ data }">
            <Button
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              label="Edit"
              @click.stop="openDialog(data)"
              data-testid="pricing-row-edit-btn"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="editingEntry ? `Edit ${editingEntry.sku || 'Entry'}` : 'New Price Book Entry'"
        :modal="true"
        :style="{ width: '520px' }"
        data-testid="pricing-dialog"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label for="pricing-sku">SKU</label>
            <InputText
              id="pricing-sku"
              v-model="form.sku"
              class="w-full"
              data-testid="pricing-sku-input"
            />
          </div>
          <div class="form-field full-width">
            <label for="pricing-name">Name</label>
            <InputText
              id="pricing-name"
              v-model="form.name"
              class="w-full"
              data-testid="pricing-name-input"
            />
          </div>
          <div class="form-field full-width">
            <label for="pricing-category">Category</label>
            <Select
              id="pricing-category"
              v-model="form.category"
              :options="categoryOptions"
              optionLabel="label"
              optionValue="value"
              showClear
              class="w-full"
              data-testid="pricing-category-select"
            />
          </div>
          <div class="form-field">
            <label for="pricing-unit">Unit Price</label>
            <InputNumber
              id="pricing-unit"
              v-model="form.unit_price"
              mode="currency"
              currency="USD"
              class="w-full"
              data-testid="pricing-unit-input"
            />
          </div>
          <div class="form-field">
            <label for="pricing-cost">Cost</label>
            <InputNumber
              id="pricing-cost"
              v-model="form.cost"
              mode="currency"
              currency="USD"
              class="w-full"
              data-testid="pricing-cost-input"
            />
          </div>
          <div class="form-field">
            <label for="pricing-margin">Margin %</label>
            <InputNumber
              id="pricing-margin"
              v-model="form.margin_pct"
              mode="decimal"
              min="0"
              max="100"
              suffix="%"
              class="w-full"
              data-testid="pricing-margin-input"
            />
          </div>
          <div class="form-field full-width">
            <label for="pricing-active">Active</label>
            <ToggleSwitch
              id="pricing-active"
              v-model="form.active"
              data-testid="pricing-active-toggle"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button
            label="Save"
            icon="pi pi-check"
            :loading="saving"
            @click="saveEntry"
            data-testid="pricing-save-btn"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import ToggleSwitch from 'primevue/toggleswitch';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();

const pricingEntries = ref([]);
const loading = ref(true);
const statusFilter = ref('all');
const showDialog = ref(false);
const editingEntry = ref(null);
const saving = ref(false);

const pricingTabs = ['all', 'active', 'archived'];

const form = ref({
  sku: '',
  name: '',
  category: '',
  unit_price: null,
  cost: null,
  margin_pct: null,
  active: true,
});

const filteredEntries = computed(() => {
  if (statusFilter.value === 'active') {
    return pricingEntries.value.filter((entry) => entry.active);
  }
  if (statusFilter.value === 'archived') {
    return pricingEntries.value.filter((entry) => !entry.active);
  }
  return pricingEntries.value;
});

const counts = computed(() => {
  const map = { all: pricingEntries.value.length, active: 0, archived: 0 };
  pricingEntries.value.forEach((entry) => {
    if (entry.active) map.active += 1;
    else map.archived += 1;
  });
  return map;
});

const categoryOptions = computed(() => {
  const seen = new Set();
  return pricingEntries.value
    .map((entry) => entry.category)
    .filter((category) => !!category)
    .filter((category) => {
      if (seen.has(category)) return false;
      seen.add(category);
      return true;
    })
    .map((category) => ({ label: category, value: category }));
});

const tabLabel = (tab) => {
  const label = tab.charAt(0).toUpperCase() + tab.slice(1);
  const suffix = counts.value[tab] ? ` (${counts.value[tab]})` : '';
  return `${label}${suffix}`;
};

const formatCurrency = (value) => {
  if (value == null) return '—';
  return `$${Number(value).toFixed(2)}`;
};

const formatPercent = (value) => {
  if (value == null) return '—';
  return `${Number(value).toFixed(2)}%`;
};

const loadPricing = async () => {
  loading.value = true;
  try {
    const data = await api.get('/api/pricing');
    const list = Array.isArray(data) ? data : data?.items || [];
    pricingEntries.value = list;
  } finally {
    loading.value = false;
  }
};

const openDialog = (entry = null) => {
  editingEntry.value = entry;
  form.value = entry
    ? {
        sku: entry.sku || '',
        name: entry.name || '',
        category: entry.category || '',
        unit_price: entry.unit_price ?? null,
        cost: entry.cost ?? null,
        margin_pct: entry.margin_pct ?? null,
        active: entry.active !== false,
      }
    : { sku: '', name: '', category: '', unit_price: null, cost: null, margin_pct: null, active: true };
  showDialog.value = true;
};

const saveEntry = async () => {
  if (!form.value.sku?.trim() || !form.value.name?.trim()) return;
  saving.value = true;
  const payload = {
    sku: form.value.sku.trim(),
    name: form.value.name.trim(),
    category: form.value.category || undefined,
    unit_price: form.value.unit_price != null ? Number(form.value.unit_price) : null,
    cost: form.value.cost != null ? Number(form.value.cost) : null,
    margin_pct: form.value.margin_pct != null ? Number(form.value.margin_pct) : null,
    active: !!form.value.active,
  };
  try {
    if (editingEntry.value) {
      await api.patch(`/api/pricing/${editingEntry.value.id}`, payload, { successMessage: 'Entry updated' });
    } else {
      await api.post('/api/pricing', payload, { successMessage: 'Entry created' });
    }
    showDialog.value = false;
    await loadPricing();
  } finally {
    saving.value = false;
  }
};

onMounted(loadPricing);
</script>

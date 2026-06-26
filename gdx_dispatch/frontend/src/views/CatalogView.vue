<template>
    <section class="catalog-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Catalogs</h2>
        </template>
        <template #end>
          <Button label="+ New Catalog" icon="pi pi-plus" data-testid="new-catalog-btn" @click="showNewCatalog = true" />
          <Button label="Import CSV" icon="pi pi-upload" severity="secondary" data-testid="import-csv-btn"
            :disabled="!selectedCatalog" @click="showImportDialog = true" />
          <Button label="AI Import" icon="pi pi-sparkles" severity="success" data-testid="ai-import-btn"
            :disabled="!selectedCatalog" @click="showAiImportDialog = true" />
          <Button v-if="selectedCatalog" label="Sync QBO" icon="pi pi-sync" severity="info" @click="syncQbo" :loading="syncing" />
          <Button v-if="selectedCatalog && !selectedCatalog.read_only"
            :label="selectedCatalog.active === false ? 'Activate' : 'Deactivate'"
            :icon="selectedCatalog.active === false ? 'pi pi-eye' : 'pi pi-eye-slash'"
            severity="warning" outlined data-testid="toggle-catalog-active-btn"
            @click="toggleCatalogActive" :loading="togglingActive" />
          <Button v-if="selectedCatalog && !selectedCatalog.read_only" label="Delete Catalog" icon="pi pi-trash"
            severity="danger" outlined data-testid="delete-catalog-btn" @click="confirmDeleteCatalog" :loading="deletingCatalog" />
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error">{{ loadError }}</div>
      <div v-if="isLoading" class="spinner-wrap"><ProgressSpinner /></div>

      <template v-if="!isLoading && !loadError">
        <!-- Catalog picker row — one button per catalog. Type label shows
             which product class each catalog holds (Parts/Doors/etc). -->
        <div class="catalog-tabs">
          <Button v-for="cat in catalogs" :key="cat.id"
            :label="`${cat.name} · ${classLabel(cat)}${cat.active === false ? ' · Inactive' : ''}`"
            :severity="selectedCatalog?.id === cat.id ? undefined : 'secondary'"
            :outlined="cat.active === false"
            size="small"
            @click="selectCatalog(cat)"
            :data-testid="`catalog-${cat.id}`" />
        </div>

        <div v-if="!catalogs.length" class="empty-state">
          <i class="pi pi-inbox" style="font-size:3rem; color:#64748b;"></i>
          <h3>No Catalogs Yet</h3>
          <p>Create a catalog (parts, doors, openers, …) to track your inventory and pricing.</p>
          <Button label="+ Create First Catalog" @click="showNewCatalog = true" />
        </div>

        <!-- Items table — columns rendered from the registry per product_class -->
        <div v-if="selectedCatalog" class="items-section">
          <Message
            v-if="pricingStatus && pricingStatus.kind === 'not_configured'"
            severity="warn"
            :closable="false"
            class="pricing-warn"
            data-testid="catalog-pricing-warn"
          >
            <strong>Retail prices unavailable —</strong>
            {{ pricingStatus.message }}
            <router-link to="/margin-tiers" class="pricing-warn-link">Configure margin tiers</router-link>
          </Message>
          <div class="items-header">
            <h3>{{ selectedCatalog.name }} — {{ items.length }} {{ classLabel(selectedCatalog).toLowerCase() }}</h3>
            <div style="display:flex; gap:0.5rem;">
              <InputText v-model="searchQuery" placeholder="Search..." data-testid="catalog-search" />
              <Button :label="`+ Add ${singularLabel(selectedCatalog)}`" icon="pi pi-plus" @click="openAddItem" />
            </div>
          </div>

          <DataTable
      responsiveLayout="scroll" :value="filteredItems" paginator :rows="20" striped-rows data-testid="catalog-items-table"
            sort-field="name" :sort-order="1">
            <template #empty>
              <div class="empty-state">
                <p>No items in this catalog yet.</p>
                <Button :label="`+ Add First ${singularLabel(selectedCatalog)}`" @click="openAddItem" />
              </div>
            </template>
            <Column v-for="col in tableColumns" :key="col.field"
              :field="col.field" :header="col.header"
              :sortable="col.sortable" :style="col.style">
              <template #body="{ data }">
                <span v-if="col.format === 'currency'">
                  <template v-if="readField(data, col.field) == null">—</template>
                  <template v-else>
                    ${{ Number(readField(data, col.field)).toFixed(2) }}<span
                      v-if="col.field === 'price' && data.price_source === 'computed'"
                      class="muted"
                      title="Retail computed from cost × tenant margin tier (no fixed catalog price set)"
                    > *</span>
                  </template>
                </span>
                <span v-else>{{ readField(data, col.field) ?? '—' }}</span>
              </template>
            </Column>
            <Column header="Actions" style="width:110px">
              <template #body="{ data }">
                <Button icon="pi pi-pencil" aria-label="Edit" text size="small" @click="editItem(data)" />
                <Button icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click="confirmDeleteItem(data)" />
              </template>
            </Column>
          </DataTable>
        </div>
      </template>

      <!-- New Catalog Dialog — Type select drives downstream form/columns -->
      <Dialog v-model:visible="showNewCatalog" header="New Catalog" modal :style="{width: '440px'}">
        <div class="form-field">
          <label>Catalog Type</label>
          <Select v-model="newCatalogForm.product_class" :options="productClassOptions"
            option-label="label" option-value="value" placeholder="Choose type"
            data-testid="new-catalog-type" class="w-full" />
          <small v-if="selectedClassDescription" class="muted">{{ selectedClassDescription }}</small>
        </div>
        <div class="form-field">
          <label>Catalog Name</label>
          <InputText v-model="newCatalogForm.name" placeholder="e.g., Custom Doors 2026" data-testid="new-catalog-name" class="w-full" />
        </div>

        <!-- ADR-015 — custom catalog field builder. Only shown for the "Custom…" type. -->
        <div v-if="isCustomNew" class="form-field custom-fields" data-testid="custom-field-builder">
          <label>Fields</label>
          <small class="muted">Define the columns this catalog tracks. Name, SKU, cost &amp; price are always included.</small>
          <div v-for="(f, idx) in customFields" :key="idx" class="custom-field-row">
            <InputText v-model="f.label" placeholder="Field label (e.g. Tonnage)"
              :data-testid="`custom-field-label-${idx}`" class="cf-label" />
            <Select v-model="f.type" :options="fieldTypeOptions" option-label="label" option-value="value" class="cf-type" />
            <InputText v-if="f.type === 'select'" v-model="f.optionsText"
              placeholder="Options, comma-separated" class="cf-options" />
            <label class="cf-required"><Checkbox v-model="f.required" :binary="true" /> Required</label>
            <Button icon="pi pi-times" severity="danger" text aria-label="Remove field" @click="removeCustomField(idx)" />
          </div>
          <Button label="+ Add Field" size="small" severity="secondary" text
            data-testid="add-custom-field" @click="addCustomField" />
        </div>
        <div class="form-field">
          <label>Source System</label>
          <Select v-model="newCatalogForm.source" :options="sourceOptions" placeholder="Select source" class="w-full" />
        </div>

        <!-- ADR-015 Slice 2 — pricing strategy. Applied when an item is saved
             with no price. Pack types bring their own pricing, so hidden then. -->
        <div v-if="showPricingPicker" class="form-field">
          <label>Pricing</label>
          <Select v-model="newCatalogForm.pricing_strategy" :options="pricingStrategies"
            option-label="label" option-value="id" placeholder="Pricing strategy"
            data-testid="new-catalog-pricing" class="w-full" />
          <small class="muted">How retail is computed from cost when you leave an item's price blank.</small>
        </div>
        <div v-else class="form-field">
          <small class="muted">Pricing for this type is set by the Catalog Pack.</small>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showNewCatalog = false" />
          <Button label="Create" icon="pi pi-check" @click="createCatalog" :loading="creating" />
        </template>
      </Dialog>

      <!-- Item Dialog — fields rendered from the registry per product_class -->
      <Dialog v-model:visible="showItemDialog"
        :header="editingItem ? `Edit ${singularLabel(selectedCatalog)}` : `Add ${singularLabel(selectedCatalog)}`"
        modal :style="{width: '720px'}">
        <div class="form-grid">
          <div v-for="field in formFields" :key="field.name"
            class="form-field" :class="{ 'full-width': field.fullWidth || field.type === 'textarea' }">
            <label>{{ field.label }}{{ field.required ? ' *' : '' }}</label>
            <InputText v-if="field.type === 'text'"
              :modelValue="readField(itemForm, field.name)"
              @update:modelValue="(v) => writeField(itemForm, field.name, v)"
              :data-testid="`item-${field.name.replace('.', '-')}`" class="w-full" />
            <Textarea v-else-if="field.type === 'textarea'" rows="2"
              :modelValue="readField(itemForm, field.name)"
              @update:modelValue="(v) => writeField(itemForm, field.name, v)" class="w-full" />
            <InputNumber v-else-if="field.type === 'number'"
              :modelValue="readField(itemForm, field.name)"
              @update:modelValue="(v) => writeField(itemForm, field.name, v)"
              :min="0" :maxFractionDigits="2" class="w-full" />
            <InputNumber v-else-if="field.type === 'currency'"
              :modelValue="readField(itemForm, field.name)"
              @update:modelValue="(v) => writeField(itemForm, field.name, v)"
              mode="currency" currency="USD" class="w-full" />
            <Select v-else-if="field.type === 'select'" :options="field.options || []"
              :modelValue="readField(itemForm, field.name)"
              @update:modelValue="(v) => writeField(itemForm, field.name, v)"
              placeholder="Choose…" class="w-full" />
            <Checkbox v-else-if="field.type === 'checkbox'" :binary="true"
              :modelValue="readField(itemForm, field.name)"
              @update:modelValue="(v) => writeField(itemForm, field.name, v)" />
            <input v-else-if="field.type === 'date'" type="date" class="w-full native-date"
              :value="readField(itemForm, field.name)"
              @input="(e) => writeField(itemForm, field.name, e.target.value)" />
            <InputText v-else
              :modelValue="readField(itemForm, field.name)"
              @update:modelValue="(v) => writeField(itemForm, field.name, v)" class="w-full" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showItemDialog = false" />
          <Button :label="editingItem ? 'Save' : 'Add'" icon="pi pi-check" @click="saveItem" :loading="savingItem" />
        </template>
      </Dialog>

      <!-- Import CSV Dialog -->
      <Dialog v-model:visible="showImportDialog" header="Import Catalog from CSV" modal :style="{width: '500px'}">
        <p class="muted">Upload a CSV file with columns: SKU, Name, Description, Cost, Retail, Stock, Vendor</p>
        <FileUpload mode="basic" accept=".csv,text/csv" :maxFileSize="5000000"
          @select="onFileSelect" :auto="false" chooseLabel="Choose CSV" />
        <div v-if="importFile" class="muted" style="margin-top:0.5rem;">
          Selected: {{ importFile.name }} ({{ (importFile.size / 1024).toFixed(1) }} KB)
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showImportDialog = false" />
          <Button label="Import" icon="pi pi-upload" @click="importCsv" :loading="importing" :disabled="!importFile" />
        </template>
      </Dialog>

      <!-- AI Import Dialog -->
      <Dialog v-model:visible="showAiImportDialog" header="AI Import — Extract Parts from Any File" modal :style="{width: '600px'}">
        <p class="muted">
          Upload any file (PDF, CSV, TXT, vendor quote) and AI will extract the parts
          into your catalog. PDFs are read server-side. Supports CHI, Clopay, Amarr,
          Wayne Dalton price sheets and more — multi-page sheets are paginated automatically.
        </p>
        <FileUpload mode="basic" accept=".csv,.txt,.json,.pdf,text/*,application/pdf" :maxFileSize="20000000"
          @select="onAiFileSelect" :auto="false" chooseLabel="Choose File" data-testid="ai-file-select" />
        <div v-if="aiFile" class="muted" style="margin-top:0.5rem;">
          Selected: {{ aiFile.name }} ({{ (aiFile.size / 1024).toFixed(1) }} KB)
        </div>
        <div v-if="aiImportResult" class="ai-result">
          <h4>AI Extraction Complete</h4>
          <p><strong>{{ aiImportResult.imported }}</strong> parts imported of <strong>{{ aiImportResult.total_extracted }}</strong> extracted.</p>
          <Message v-if="aiImportResult.partial" severity="warn" :closable="false" data-testid="ai-partial-warn">
            Partial import: {{ aiImportResult.failed_chunks }} of {{ aiImportResult.chunks }} page-chunks
            failed to parse. Some parts may be missing — re-run on a cleaner/smaller file if needed.
          </Message>
          <div v-if="aiImportResult.sample?.length" class="sample-parts">
            <strong>Sample:</strong>
            <div v-for="(part, i) in aiImportResult.sample" :key="i" class="sample-part">
              {{ part.sku }} — {{ part.name }} (${{ part.cost }} / ${{ part.price }})
            </div>
          </div>
        </div>
        <template #footer>
          <Button label="Close" severity="secondary" @click="closeAiImport" />
          <Button label="Extract with AI" icon="pi pi-sparkles" severity="success"
            @click="runAiImport" :loading="aiImporting" :disabled="!aiFile" data-testid="run-ai-import" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import {
  PRODUCT_CLASS_OPTIONS,
  getCatalogClass,
  emptyItemForCatalog,
  createPayloadFromPackType,
  readField,
  writeField,
} from "../catalog/types.js";
import Button from "primevue/button";
import Checkbox from "primevue/checkbox";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import FileUpload from "primevue/fileupload";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import Message from "primevue/message";
import ProgressSpinner from "primevue/progressspinner";
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const catalogs = ref([]);
const selectedCatalog = ref(null);
const items = ref([]);
const isLoading = ref(true);
const loadError = ref("");
const pricingStatus = ref(null);
const searchQuery = ref("");

const showNewCatalog = ref(false);
const showItemDialog = ref(false);
const showImportDialog = ref(false);
const showAiImportDialog = ref(false);
const editingItem = ref(null);
const creating = ref(false);
const savingItem = ref(false);
const importing = ref(false);
const syncing = ref(false);
const deletingCatalog = ref(false);
const togglingActive = ref(false);
const importFile = ref(null);
const aiFile = ref(null);
const aiImporting = ref(false);
const aiImportResult = ref(null);

// ADR-015 Slice 2/3 — pricing strategies + pack-contributed catalog types,
// loaded from the API on mount.
const pricingStrategies = ref([]);
const packTypes = ref([]);

// Type dropdown = built-ins + Custom… + one entry per installed pack type.
const productClassOptions = computed(() => [
  ...PRODUCT_CLASS_OPTIONS,
  ...packTypes.value.map((t) => ({
    label: `${t.label} (Pack)`,
    value: `pack:${t.key}`,
    description: `From the ${t.plugin} Catalog Pack — fields and pricing are predefined.`,
  })),
]);

// ADR-015 — field types offered in the custom-catalog builder.
const fieldTypeOptions = [
  { label: "Text", value: "text" },
  { label: "Long text", value: "textarea" },
  { label: "Number", value: "number" },
  { label: "Currency", value: "currency" },
  { label: "Yes/No", value: "checkbox" },
  { label: "Dropdown", value: "select" },
  { label: "Date", value: "date" },
];

// classLabel/singularLabel take a catalog object so custom catalogs resolve to
// their own field_schema-derived label rather than the static registry.
function classLabel(cat) {
  return getCatalogClass(cat).label;
}

function singularLabel(cat) {
  const klass = getCatalogClass(cat);
  if (klass.singular) return klass.singular;
  const label = klass.label;
  // 'Parts' → 'Part', 'Doors' → 'Door'
  return label.endsWith("s") ? label.slice(0, -1) : label;
}

const isCustomNew = computed(() => newCatalogForm.value.product_class === "custom");
const isPackNew = computed(() => String(newCatalogForm.value.product_class).startsWith("pack:"));
// Pack types bring their own pricing; the picker is for built-in/custom catalogs.
const showPricingPicker = computed(() => !isPackNew.value);

const selectedClassDescription = computed(() => {
  const opt = productClassOptions.value.find(
    (o) => o.value === newCatalogForm.value.product_class
  );
  return opt ? opt.description : "";
});

const formFields = computed(() => {
  if (!selectedCatalog.value) return [];
  return getCatalogClass(selectedCatalog.value).formFields;
});

const tableColumns = computed(() => {
  if (!selectedCatalog.value) return [];
  return getCatalogClass(selectedCatalog.value).tableColumns;
});

const sourceOptions = ["manual", "chi", "qb"];

const newCatalogForm = ref({ name: "", source: "manual", product_class: "parts", pricing_strategy: "manual" });
// Builder rows for a custom catalog: { label, type, required, optionsText }.
const customFields = ref([]);
const itemForm = ref(emptyItemForCatalog({ product_class: "parts" }));

function addCustomField() {
  customFields.value.push({ label: "", type: "text", required: false, optionsText: "" });
}

function removeCustomField(idx) {
  customFields.value.splice(idx, 1);
}

// label → safe schema name (matches backend _FIELD_NAME_RE).
function slugifyFieldName(label) {
  let s = (label || "")
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 40);
  if (!s) s = "field";
  if (!/^[a-z]/.test(s)) s = `f_${s}`.slice(0, 40);
  return s;
}

// Build the backend field_schema from the builder rows, ensuring unique names.
function buildFieldSchema() {
  const seen = new Set();
  return customFields.value
    .filter((f) => (f.label || "").trim())
    .map((f) => {
      let name = slugifyFieldName(f.label);
      let n = name;
      let i = 2;
      while (seen.has(n)) n = `${name}_${i++}`.slice(0, 40);
      seen.add(n);
      const entry = { name: n, label: f.label.trim(), type: f.type, required: !!f.required };
      if (f.type === "select") {
        entry.options = (f.optionsText || "")
          .split(",")
          .map((o) => o.trim())
          .filter(Boolean);
      }
      return entry;
    });
}

const filteredItems = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return items.value;
  return items.value.filter((i) =>
    (i.sku || "").toLowerCase().includes(q) ||
    (i.name || "").toLowerCase().includes(q) ||
    (i.description || "").toLowerCase().includes(q)
  );
});

async function loadCatalogs() {
  isLoading.value = true;
  try {
    const data = await api.get("/api/catalogs");
    catalogs.value = Array.isArray(data) ? data : data?.items || [];
    if (catalogs.value.length > 0 && !selectedCatalog.value) {
      await selectCatalog(catalogs.value[0]);
    }
  } catch (e) {
    loadError.value = e.message || "Failed to load catalogs";
  } finally {
    isLoading.value = false;
  }
  // ADR-015 — pricing strategies (Slice 2) + installed pack types (Slice 3).
  // Non-fatal: a missing plugin-host just means no pack types.
  try { pricingStrategies.value = await api.get("/api/catalogs/pricing-strategies") || []; } catch { /* noop */ }
  try { packTypes.value = await api.get("/api/catalogs/pack-types") || []; } catch { /* noop */ }
}

async function selectCatalog(catalog) {
  selectedCatalog.value = catalog;
  pricingStatus.value = null;
  try {
    const data = await api.get(`/api/catalogs/${catalog.id}/items`);
    items.value = Array.isArray(data) ? data : data?.items || [];
    // S113 — surface pricing-engine status when CHI catalog endpoint
    // signals tier configuration is missing. The bare object response
    // always carries the field; we only render the banner when it
    // explicitly says NOT ok.
    if (!Array.isArray(data) && data?.pricing_status && data.pricing_status !== "ok") {
      pricingStatus.value = {
        kind: data.pricing_status,
        message: data.pricing_status_message || "",
      };
    }
  } catch {
    items.value = [];
  }
}

async function createCatalog() {
  if (!newCatalogForm.value.name) return;
  let payload;
  if (isPackNew.value) {
    // Pack type: expand the template into a self-contained custom catalog.
    const key = newCatalogForm.value.product_class.slice("pack:".length);
    const packType = packTypes.value.find((t) => t.key === key);
    if (!packType) {
      loadError.value = "That Catalog Pack type is no longer available.";
      return;
    }
    payload = createPayloadFromPackType(newCatalogForm.value.name, newCatalogForm.value.source, packType);
  } else {
    payload = { ...newCatalogForm.value };
    if (isCustomNew.value) {
      payload.field_schema = buildFieldSchema();
      if (!payload.field_schema.length) {
        loadError.value = "Add at least one field for a custom catalog.";
        return;
      }
    }
    // Attach the chosen strategy's declarative spec so pricing is self-contained
    // (pack strategies picked here must carry their {kind,params}; built-ins too).
    const strat = pricingStrategies.value.find((s) => s.id === payload.pricing_strategy);
    if (strat) payload.pricing_config = { kind: strat.kind, params: strat.params || {} };
  }
  creating.value = true;
  try {
    const created = await api.post("/api/catalogs", payload);
    catalogs.value.push(created);
    await selectCatalog(created);
    showNewCatalog.value = false;
    newCatalogForm.value = { name: "", source: "manual", product_class: "parts", pricing_strategy: "manual" };
    customFields.value = [];
  } catch (err) {
    console.error('create_catalog_failed', err?.message || err);
  } finally {
    creating.value = false;
  }
}

function openAddItem() {
  editingItem.value = null;
  itemForm.value = emptyItemForCatalog(selectedCatalog.value || { product_class: "parts" });
  showItemDialog.value = true;
}

function editItem(item) {
  editingItem.value = item;
  // Deep-copy to avoid mutating the row in place before save.
  itemForm.value = JSON.parse(JSON.stringify(item));
  const klass = getCatalogClass(selectedCatalog.value);
  if (klass.defaultSpec !== null && !itemForm.value.spec) itemForm.value.spec = {};
  if (klass.isCustom && !itemForm.value.attributes) itemForm.value.attributes = {};
  showItemDialog.value = true;
}

async function saveItem() {
  savingItem.value = true;
  try {
    if (editingItem.value) {
      await api.patch(`/api/catalogs/${selectedCatalog.value.id}/items/${editingItem.value.id}`, itemForm.value);
    } else {
      await api.post(`/api/catalogs/${selectedCatalog.value.id}/items`, itemForm.value);
    }
    showItemDialog.value = false;
    await selectCatalog(selectedCatalog.value);
  } finally {
    savingItem.value = false;
  }
}

async function confirmDeleteItem(item) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete "${item.name}"?` }))) return;
  await api.delete(`/api/catalogs/${selectedCatalog.value.id}/items/${item.id}`);
  await selectCatalog(selectedCatalog.value);
}

async function toggleCatalogActive() {
  const cat = selectedCatalog.value;
  if (!cat) return;
  togglingActive.value = true;
  try {
    const updated = await api.patch(`/api/catalogs/${cat.id}`, { active: cat.active === false });
    Object.assign(cat, updated);
    const idx = catalogs.value.findIndex((c) => c.id === cat.id);
    if (idx >= 0) catalogs.value[idx] = { ...catalogs.value[idx], ...updated };
  } finally {
    togglingActive.value = false;
  }
}

async function confirmDeleteCatalog() {
  const cat = selectedCatalog.value;
  if (!cat) return;
  if (!(await confirmAsync({
    header: 'Delete catalog',
    message: `Delete catalog "${cat.name}" and hide all its items from the pickers? An admin can restore it.`,
  }))) return;
  deletingCatalog.value = true;
  try {
    await api.delete(`/api/catalogs/${cat.id}`);
    catalogs.value = catalogs.value.filter((c) => c.id !== cat.id);
    selectedCatalog.value = null;
    if (catalogs.value.length) await selectCatalog(catalogs.value[0]);
  } finally {
    deletingCatalog.value = false;
  }
}

function onFileSelect(event) {
  importFile.value = event.files[0];
}

async function importCsv() {
  if (!importFile.value) return;
  importing.value = true;
  try {
    const formData = new FormData();
    formData.append("file", importFile.value);
    await api.post(`/api/catalogs/${selectedCatalog.value.id}/import`, formData);
    showImportDialog.value = false;
    importFile.value = null;
    await selectCatalog(selectedCatalog.value);
  } finally {
    importing.value = false;
  }
}

async function syncQbo() {
  syncing.value = true;
  try {
    await api.post(`/api/catalogs/${selectedCatalog.value.id}/sync/qb/pull`);
    await selectCatalog(selectedCatalog.value);
  } finally {
    syncing.value = false;
  }
}

function onAiFileSelect(event) {
  aiFile.value = event.files[0];
  aiImportResult.value = null;
}

async function runAiImport() {
  if (!aiFile.value) return;
  aiImporting.value = true;
  try {
    const formData = new FormData();
    formData.append("file", aiFile.value);
    const result = await api.post(`/api/catalogs/${selectedCatalog.value.id}/ai-import`, formData);
    aiImportResult.value = result;
    await selectCatalog(selectedCatalog.value);
  } finally {
    aiImporting.value = false;
  }
}

function closeAiImport() {
  showAiImportDialog.value = false;
  aiFile.value = null;
  aiImportResult.value = null;
}

onMounted(loadCatalogs);
</script>

<style scoped>
.page-title { margin: 0; }
.catalog-tabs { display: flex; gap: 0.5rem; margin: 1rem 0; flex-wrap: wrap; }
.items-section { margin-top: 1rem; }
.items-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 1rem; }
.items-header h3 { margin: 0; }

.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.form-field { display: flex; flex-direction: column; gap: 0.3rem; }
.form-field label { font-size: 0.82rem; font-weight: 600; color: var(--p-text-muted-color); }
.full-width { grid-column: 1 / -1; }
.w-full { width: 100%; }

.empty-state { text-align: center; padding: 3rem; color: var(--p-text-muted-color); }
.empty-state h3 { margin: 1rem 0 0.5rem; color: var(--text-color); }

.muted { color: var(--p-text-muted-color); }

/* ADR-015 — custom catalog field builder */
.custom-fields { gap: 0.5rem; }
.custom-field-row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.custom-field-row .cf-label { flex: 1 1 140px; }
.custom-field-row .cf-type { flex: 0 0 130px; }
.custom-field-row .cf-options { flex: 1 1 160px; }
.custom-field-row .cf-required { display: flex; align-items: center; gap: 0.3rem; font-size: 0.8rem; white-space: nowrap; }
.native-date { padding: 0.5rem 0.75rem; border: 1px solid var(--p-inputtext-border-color, #cbd5e1); border-radius: 6px; background: var(--p-inputtext-background, #fff); color: inherit; }
.inline-error { color: #ef4444; padding: 0.5rem; }
.spinner-wrap { display: flex; justify-content: center; padding: 3rem; }

.ai-result {
  margin-top: 1rem;
  padding: 1rem;
  background: var(--p-content-hover-background);
  border-radius: 8px;
  border-left: 3px solid var(--p-primary-color);
}
.ai-result h4 { margin: 0 0 0.5rem; color: var(--p-primary-color); }
.sample-parts { margin-top: 0.5rem; font-size: 0.85rem; }
.sample-part { padding: 0.25rem 0; color: var(--p-text-muted-color); }
</style>

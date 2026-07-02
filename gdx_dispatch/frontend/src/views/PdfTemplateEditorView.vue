<template>
    <section class="pdf-editor view-card">
      <div class="page-header">
        <h2>PDF Template Editor</h2>
        <Button label="Save Template" icon="pi pi-save" data-testid="save-btn" @click="saveTemplate" />
      </div>

      <div v-if="loading" class="spinner-wrap" data-testid="pdf-editor-loading">
        <ProgressSpinner />
      </div>

      <div v-else class="editor-layout">
        <!-- Left Sidebar -->
        <div class="sidebar">
          <div class="form-field">
            <label>Template Type</label>
            <Select v-model="selectedType" :options="templateTypes" optionLabel="label" optionValue="key"
              @change="loadTemplate" data-testid="template-type-select" class="w-full" />
          </div>

          <div class="form-field">
            <label>Brand Color</label>
            <div class="flex align-items-center gap-2">
              <ColorPicker v-model="config.brand_color" data-testid="brand-color-picker" />
              <InputText v-model="config.brand_color" class="w-full" />
            </div>
          </div>

          <div class="form-field">
            <label>Font</label>
            <Select v-model="config.font_family" :options="fontOptions" data-testid="font-family-select" class="w-full" />
          </div>

          <div class="form-field">
            <label>Header Text</label>
            <InputText v-model="config.header_content" placeholder="Company tagline or header" class="w-full" data-testid="header-input" />
          </div>

          <div class="form-field">
            <label>Footer Text</label>
            <InputText v-model="config.footer_content" placeholder="Thank you for your business!" class="w-full" data-testid="footer-input" />
          </div>

          <h4 class="mt-4">Layout Blocks</h4>
          <div class="block-list" data-testid="block-list">
            <div v-for="block in sortedBlocks" :key="block.id" class="block-item"
              :class="{ 'block-hidden': !block.visible, 'block-selected': selectedBlock?.id === block.id }"
              @click="selectedBlock = block">
              <i class="pi pi-bars drag-handle" />
              <span class="block-label">{{ formatName(block.type) }}</span>
              <ToggleSwitch v-model="block.visible" data-testid="block-visibility" />
            </div>
          </div>

          <!-- Block settings -->
          <div v-if="selectedBlock" class="block-settings mt-3">
            <h4>{{ formatName(selectedBlock.type) }} Settings</h4>
            <div class="form-field">
              <label>Font Size</label>
              <InputNumber v-model="selectedBlock.styles.font_size" suffix="pt" :min="8" :max="36" class="w-full" />
            </div>
            <div class="form-field">
              <label>Alignment</label>
              <Select v-model="selectedBlock.styles.alignment" :options="['left', 'center', 'right']" class="w-full" />
            </div>
            <div v-if="selectedBlock.type === 'line_items'" class="form-field">
              <label>Show Unit Price</label>
              <ToggleSwitch v-model="selectedBlock.settings.show_unit_price" />
            </div>
            <div v-if="selectedBlock.type === 'line_items'" class="form-field">
              <label>Show Tax</label>
              <ToggleSwitch v-model="selectedBlock.settings.show_tax" />
            </div>
            <div v-if="selectedBlock.type === 'signature'" class="form-field">
              <label>Require Signature</label>
              <ToggleSwitch v-model="selectedBlock.settings.required" />
            </div>
          </div>
        </div>

        <!-- Preview Area -->
        <div class="preview-area" data-testid="preview-area">
          <div class="pdf-page" :style="{ fontFamily: config.font_family }">
            <div v-if="config.header_content" class="pdf-header" :style="{ borderColor: config.brand_color }">
              {{ config.header_content }}
            </div>

            <div v-for="block in visibleBlocks" :key="block.id" class="pdf-block"
              :class="{ 'selected': selectedBlock?.id === block.id }"
              @click="selectedBlock = block">
              <div class="block-type-label">{{ formatName(block.type) }}</div>
              <div v-if="block.type === 'logo'" class="preview-logo">
                <i class="pi pi-image" style="font-size: 2rem" />
                <span>Company Logo</span>
              </div>
              <div v-else-if="block.type === 'company_info'" class="preview-company">
                <strong>Example Garage Doors</strong><br>123 Main St, Anytown USA<br>(218) 555-0100
              </div>
              <div v-else-if="block.type === 'customer_info'" class="preview-customer">
                <strong>Bill To:</strong><br>John Smith<br>456 Oak Ave<br>john@example.com
              </div>
              <div v-else-if="block.type === 'line_items'" class="preview-items">
                <table>
                  <thead><tr><th>Item</th><th>Qty</th><th>Price</th><th>Total</th></tr></thead>
                  <tbody>
                    <tr><td>CHI 2283 16x7 Door</td><td>1</td><td>$1,200</td><td>$1,200</td></tr>
                    <tr><td>Torsion Springs (pair)</td><td>1</td><td>$180</td><td>$180</td></tr>
                    <tr><td>Labor — Installation</td><td>1</td><td>$450</td><td>$450</td></tr>
                  </tbody>
                </table>
              </div>
              <div v-else-if="block.type === 'totals'" class="preview-totals">
                Subtotal: $1,830 | Tax: $128.10 | <strong>Total: $1,958.10</strong>
              </div>
              <div v-else-if="block.type === 'notes'" class="preview-notes">
                Notes: Weather permitting, installation scheduled for next Tuesday.
              </div>
              <div v-else-if="block.type === 'terms'" class="preview-terms">
                Terms: Payment due upon completion. 1-year warranty on labor.
              </div>
              <div v-else-if="block.type === 'signature'" class="preview-signature">
                Signature: _________________________ Date: __________
              </div>
            </div>

            <div v-if="config.footer_content" class="pdf-footer">
              {{ config.footer_content }}
            </div>
          </div>
        </div>
      </div>
    </section>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import { useToast } from "primevue/usetoast";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Select from "primevue/select";
import ToggleSwitch from "primevue/toggleswitch";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";

const toast = useToast();
const api = useApi();

const selectedType = ref("estimate");
const selectedBlock = ref(null);
const loading = ref(true);
const config = ref({
  brand_color: "#0057a8",
  font_family: "Helvetica",
  header_content: "",
  footer_content: "",
  logo_url: null,
  blocks: [],
});

const templateTypes = [
  { key: "estimate", label: "Estimate" },
  { key: "invoice", label: "Invoice" },
  { key: "work_order", label: "Work Order" },
  { key: "install_sheet", label: "Install Sheet" },
  { key: "safety_checklist", label: "Safety Checklist" },
  { key: "purchase_order", label: "Purchase Order" },
];

const fontOptions = ["Helvetica", "Arial", "Times New Roman", "Georgia", "Courier New"];

const sortedBlocks = computed(() =>
  [...(config.value.blocks || [])].sort((a, b) => a.order - b.order)
);

const visibleBlocks = computed(() =>
  sortedBlocks.value.filter((b) => b.visible)
);

function formatName(type) {
  return (type || "").split("_").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}

async function loadTemplate() {
  loading.value = true;
  try {
    const data = await api.get(`/api/pdf-templates/${selectedType.value}`);
    config.value = data;
    selectedBlock.value = null;
  } catch (e) {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to load template", life: 4000 });
  } finally {
    loading.value = false;
  }
}

async function saveTemplate() {
  try {
    await api.put(`/api/pdf-templates/${selectedType.value}`, config.value);
    toast.add({ severity: "success", summary: "Saved", detail: "Template saved successfully", life: 3000 });
  } catch (e) {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to save template", life: 4000 });
  }
}

onMounted(() => {
  loadTemplate();
});
</script>

<style scoped>
.pdf-editor { padding: 1.5rem; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
.page-header h2 { margin: 0; }
.editor-layout { display: flex; gap: 1.5rem; min-height: calc(100vh - 180px); }
.sidebar { width: 300px; flex-shrink: 0; }
.form-field { margin-bottom: 0.75rem; }
.form-field label { display: block; font-weight: 600; margin-bottom: 0.25rem; font-size: 0.85rem; }

.block-list { display: flex; flex-direction: column; gap: 0.25rem; }
.block-item { display: flex; align-items: center; gap: 0.5rem; padding: 0.5rem; border-radius: 6px; cursor: pointer; background: var(--surface-ground); }
.block-item:hover { background: var(--surface-hover); }
.block-item.block-hidden { opacity: 0.4; }
.block-item.block-selected { outline: 2px solid var(--p-primary-color); }
.drag-handle { cursor: grab; color: var(--p-text-muted-color); }
.block-label { flex: 1; font-size: 0.85rem; }
.block-settings { padding: 0.75rem; background: var(--surface-ground); border-radius: 6px; }

.spinner-wrap { display: flex; justify-content: center; margin: 2rem 0; }

.preview-area { flex: 1; background: var(--p-content-border-color); border-radius: 8px; padding: 2rem; display: flex; justify-content: center; overflow: auto; }
/* Everything inside .pdf-page simulates the printed white page, so its greys
   stay hard-coded regardless of app theme. */
.pdf-page { width: 210mm; min-height: 297mm; background: white; color: #1f2937; padding: 20mm; box-shadow: 0 4px 16px rgba(0,0,0,0.15); border-radius: 2px; } /* printed-page preview: intentionally light (fixed dark ink so dark-theme text doesn't inherit white-on-white) */
.pdf-header { text-align: center; padding-bottom: 0.5rem; border-bottom: 3px solid; margin-bottom: 1rem; font-weight: 600; }
.pdf-footer { text-align: center; margin-top: 2rem; padding-top: 0.5rem; border-top: 1px solid #ccc; font-size: 0.8rem; color: #666; } /* printed-page preview: intentionally light */
.pdf-block { padding: 0.75rem; margin: 0.5rem 0; border: 1px dashed #ddd; border-radius: 4px; cursor: pointer; position: relative; } /* printed-page preview: intentionally light */
.pdf-block.selected { border-color: var(--p-primary-color); background: rgba(0, 87, 168, 0.03); }
.pdf-block:hover { border-color: #aaa; } /* printed-page preview: intentionally light */
.block-type-label { position: absolute; top: -0.5rem; left: 0.5rem; background: white; padding: 0 0.3rem; font-size: 0.65rem; color: #999; text-transform: uppercase; } /* printed-page preview: intentionally light */
.preview-items table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.preview-items th, .preview-items td { padding: 0.3rem 0.5rem; border-bottom: 1px solid #eee; text-align: left; } /* printed-page preview: intentionally light */
.preview-items th { font-weight: 600; border-bottom: 2px solid #ccc; } /* printed-page preview: intentionally light */
.preview-totals { text-align: right; font-size: 0.9rem; }
.preview-signature { margin-top: 2rem; }
.preview-logo { display: flex; align-items: center; gap: 0.5rem; color: #999; } /* printed-page preview: intentionally light */
</style>

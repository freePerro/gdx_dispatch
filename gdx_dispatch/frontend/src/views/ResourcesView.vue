<template>
    <section class="resources-view view-card" data-testid="resources-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Resources</h2>
        </template>
        <template #end>
          <Button
            label="+ Upload Resource"
            icon="pi pi-cloud-upload"
            data-testid="resource-upload-btn"
            @click="showDialog = true"
          />
        </template>
      </Toolbar>

      <div class="filter-tabs" data-testid="resources-tabs">
        <Button
          v-for="option in categoryFilters"
          :key="option.value"
          :label="option.label + (option.count ? ` (${option.count})` : '')"
          :severity="categoryFilter === option.value ? undefined : 'secondary'"
          size="small"
          @click="categoryFilter = option.value"
        />
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
      responsiveLayout="scroll"
        v-else
        :value="filteredResources"
        paginator
        :rows="15"
        striped-rows
        data-testid="resources-table"
      >
        <Column field="name" header="Name" />
        <Column field="category" header="Category" style="width:150px">
          <template #body="{ data }">
            <Tag :value="data.category?.toUpperCase()" :severity="categorySeverity(data.category)" />
          </template>
        </Column>
        <Column field="file_size" header="Size" style="width:120px">
          <template #body="{ data }">{{ formatFileSize(data.file_size) }}</template>
        </Column>
        <Column field="uploaded_by" header="Uploaded By" />
        <Column field="updated_at" header="Updated" style="width:140px">
          <template #body="{ data }">{{ formatDate(data.updated_at) }}</template>
        </Column>
        <Column header="Actions" style="width:120px">
          <template #body="{ data }">
            <Button
              icon="pi pi-download"
              text
              size="small"
              data-testid="resource-download-btn"
              @click.stop="downloadResource(data)"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showDialog" header="Upload Resource" modal :style="{ width: '520px' }">
        <div class="form-grid">
          <div class="form-field full-width">
            <label>File</label>
            <div class="file-input-row">
              <Button
                label="Select File"
                icon="pi pi-file"
                @click="triggerFilePicker"
                data-testid="resource-file-btn"
              />
              <span v-if="uploadFile" class="muted" style="margin-left:0.5rem">{{ uploadFile.name }}</span>
            </div>
            <input
              ref="fileInput"
              type="file"
              style="display:none"
              @change="handleFileChange"
            />
          </div>
          <div class="form-field full-width">
            <label>Name</label>
            <InputText v-model="uploadForm.name" data-testid="resource-name-input" class="w-full" />
          </div>
          <div class="form-field">
            <label>Category</label>
            <Select
              v-model="uploadForm.category"
              :options="categoryOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="resource-category-select"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="cancelUpload" />
          <Button
            label="Upload"
            icon="pi pi-check"
            :loading="uploading"
            :disabled="!uploadFile || !uploadForm.name"
            @click="uploadResource"
            data-testid="resource-upload-submit"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Select from "primevue/select";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const resources = ref([]);
const loading = ref(true);
const categoryFilter = ref("all");
const showDialog = ref(false);
const uploadFile = ref(null);
const uploading = ref(false);
const fileInput = ref(null);

const uploadForm = ref({ name: "", category: "sop" });

const categoryOptions = [
  { label: "SOP", value: "sop" },
  { label: "Training", value: "training" },
  { label: "Forms", value: "forms" },
  { label: "Templates", value: "templates" },
];

const categoryFilters = computed(() => {
  const tally = { all: resources.value.length };
  resources.value.forEach((resource) => {
    const key = resource.category || "other";
    tally[key] = (tally[key] || 0) + 1;
  });
  return [
    { label: "All", value: "all", count: tally.all },
    ...categoryOptions.map((option) => ({
      label: option.label,
      value: option.value,
      count: tally[option.value] || 0,
    })),
  ];
});

const filteredResources = computed(() => {
  if (categoryFilter.value === "all") return resources.value;
  return resources.value.filter((resource) => resource.category === categoryFilter.value);
});

function formatFileSize(value) {
  if (!value && value !== 0) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value) {
  return value ? value.split("T")[0] : "—";
}

function categorySeverity(category) {
  return {
    sop: "info",
    training: "success",
    forms: "warning",
    templates: "primary",
  }[category] || "secondary";
}

async function loadResources() {
  loading.value = true;
  try {
    const data = await api.get("/api/resources");
    resources.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

function triggerFilePicker() {
  fileInput.value?.click();
}

function handleFileChange(event) {
  const file = event.target?.files?.[0] || null;
  if (!file) return;
  uploadFile.value = file;
  if (!uploadForm.value.name) {
    uploadForm.value.name = file.name;
  }
}

function cancelUpload() {
  showDialog.value = false;
  uploadFile.value = null;
  uploadForm.value = { name: "", category: "sop" };
  if (fileInput.value) {
    fileInput.value.value = null;
  }
}

async function uploadResource() {
  if (!uploadFile.value || !uploadForm.value.name.trim()) {
    return;
  }
  uploading.value = true;
  try {
    const payload = new FormData();
    payload.append("file", uploadFile.value);
    payload.append("name", uploadForm.value.name);
    payload.append("category", uploadForm.value.category);
    await api.post("/api/resources", payload, { successMessage: "Resource uploaded" });
    await loadResources();
    cancelUpload();
  } finally {
    uploading.value = false;
  }
}

function downloadResource(resource) {
  const { download_url, file_url, id } = resource;
  const url = download_url || file_url || `/api/resources/${id}/download`;
  window.open(url, "_blank");
}

onMounted(loadResources);
</script>

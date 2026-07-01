<template>
    <section class="uploads-view view-card" data-testid="uploads-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title" data-testid="uploads-title">File Uploads</h2>
        </template>
        <template #end>
          <div class="uploads-toolbar-actions">
            <Select
              v-model="entityFilter"
              :options="entityOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All entities"
              class="mr-2"
              data-testid="uploads-entity-filter"
            />
            <FileUpload
              mode="basic"
              customUpload
              :uploadHandler="handleUpload"
              chooseLabel="Upload"
              :disabled="uploading"
              data-testid="uploads-file-upload"
            />
          </div>
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap" data-testid="uploads-loading">
        <ProgressSpinner />
      </div>

      <DataTable
        v-else
        :value="filteredUploads"
        striped-rows
        data-testid="uploads-table"
        responsiveLayout="scroll"
        emptyMessage="No uploads yet"
      >
        <Column field="filename" header="File" />
        <Column field="file_type" header="Type" />
        <Column field="file_size" header="Size" :body="({ data }) => formatBytes(data.file_size)" />
        <Column field="entity_type" header="Entity" :body="({ data }) => formatEntity(data.entity_type)" />
        <Column field="entity_id" header="Entity ID" />
        <Column field="uploaded_by" header="Uploaded By" />
        <Column field="uploaded_at" header="Uploaded" :body="({ data }) => formatTimestamp(data.uploaded_at)" />
        <Column header="Actions" style="width:160px">
          <template #body="{ data }">
            <Button
              v-tooltip="'Delete'"
              icon="pi pi-trash" aria-label="Delete"
              severity="danger"
              text
              size="small"
              :loading="deletingId === getUploadId(data)"
              @click.stop="deleteUpload(data)"
              data-testid="upload-delete"
            />
          </template>
        </Column>
      </DataTable>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Select from "primevue/select";
import FileUpload from "primevue/fileupload";
import ProgressSpinner from "primevue/progressspinner";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const uploads = ref([]);
const loading = ref(true);
const uploading = ref(false);
const deletingId = ref(null);
const entityFilter = ref("all");

const entityOptions = [
  { label: "All Entities", value: "all" },
  { label: "Job", value: "job" },
  { label: "Customer", value: "customer" },
  { label: "Invoice", value: "invoice" },
];

const filteredUploads = computed(() => {
  if (entityFilter.value === "all") {
    return uploads.value;
  }
  return uploads.value.filter((item) => item.entity_type === entityFilter.value);
});

function getUploadId(item) {
  return item.id ?? item.upload_id ?? item.file_id;
}

function formatBytes(value) {
  if (value === null || value === undefined) return "—";
  const num = Number(value);
  if (isNaN(num)) return "—";
  if (num < 1024) return `${num} B`;
  if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KB`;
  return `${(num / 1024 / 1024).toFixed(1)} MB`;
}

function formatTimestamp(value) {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch (error) {
    return value;
  }
}

function formatEntity(value) {
  if (!value) return "—";
  return value.charAt(0).toUpperCase() + value.slice(1);
}

async function loadUploads() {
  loading.value = true;
  try {
    const data = await api.get("/api/uploads");
    uploads.value = Array.isArray(data) ? data : data?.items ?? [];
  } finally {
    loading.value = false;
  }
}

async function handleUpload(event) {
  if (!event.files?.length) return;
  uploading.value = true;
  try {
    const file = event.files[0];
    const formData = new FormData();
    formData.append("file", file);
    await api.post("/api/uploads", formData, { successMessage: "File uploaded" });
    await loadUploads();
    event.options?.clear?.();
    event.options?.clearUpload?.();
    event.clear?.();
  } finally {
    uploading.value = false;
  }
}

async function deleteUpload(item) {
  const id = getUploadId(item);
  if (!id) return;
  if (!(await confirmAsync({ header: 'Confirm', message: "Delete this upload?" }))) return;
  deletingId.value = id;
  try {
    await api.del(`/api/uploads/${encodeURIComponent(id)}`, { successMessage: "Upload deleted" });
    await loadUploads();
  } finally {
    deletingId.value = null;
  }
}

onMounted(loadUploads);
</script>

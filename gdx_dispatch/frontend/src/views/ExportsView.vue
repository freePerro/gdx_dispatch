<template>
    <section class="exports-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Exports</h2>
        </template>
      </Toolbar>

      <div class="exports-grid">
        <Card v-for="entity in exportEntities" :key="entity.key" class="export-card">
          <div>
            <h3>{{ entity.label }}</h3>
            <p class="muted">Download a CSV copy of {{ entity.label.toLowerCase() }}.</p>
          </div>
          <Button
            :label="`Download ${entity.label}`"
            icon="pi pi-download"
            :loading="downloading[entity.key]"
            @click="downloadEntity(entity.key)"
            class="w-full"
          />
        </Card>
      </div>

      <Card class="export-all-card">
        <div class="card-title">
          <div>
            <h3>Export all</h3>
            <p class="muted">Bundle multiple datasets into a single JSON package.</p>
          </div>
        </div>
        <div class="export-all-controls">
          <MultiSelect
            v-model="selectedEntities"
            :options="entityOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="Select entities"
            display="chip"
            class="multi-select"
          />
          <Button
            label="Download JSON package"
            icon="pi pi-cloud-download"
            :loading="exportingAll"
            @click="downloadAll"
            class="w-full"
          />
        </div>
      </Card>
    </section>
</template>

<script setup>
import { computed, reactive, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { useAuthStore } from "../stores/auth";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Card from "primevue/card";
import MultiSelect from "primevue/multiselect";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();
const auth = useAuthStore();
const toast = useToast();

const EXPORT_ENTITIES = [
  { key: "customers", label: "Customers" },
  { key: "jobs", label: "Jobs" },
  { key: "invoices", label: "Invoices" },
  { key: "estimates", label: "Estimates" },
  { key: "payments", label: "Payments" },
  { key: "technicians", label: "Technicians" },
  { key: "leads", label: "Leads" },
];

const exportEntities = EXPORT_ENTITIES;
const entityOptions = computed(() =>
  EXPORT_ENTITIES.map((entity) => ({ label: entity.label, value: entity.key }))
);

const selectedEntities = ref(EXPORT_ENTITIES.map((entity) => entity.key));
const downloading = reactive({});
const exportingAll = ref(false);

function deriveTenantId() {
  const parts = window.location.hostname.split(".");
  const slug = parts.length >= 3 ? parts[0] : null;
  if (slug && slug !== "gdx") return slug;
  return "886a5b78-6bff-4b19-823c-a2c16684447e";
}

function buildHeaders() {
  const tenantId = deriveTenantId();
  const headers = new Headers();
  headers.set("x-tenant-id", tenantId);
  if (auth.accessToken) {
    headers.set("Authorization", `Bearer ${auth.accessToken}`);
  }
  return headers;
}

function filenameFromResponse(entity, response) {
  const disposition = response.headers.get("Content-Disposition");
  if (disposition) {
    const match = disposition.match(/filename="(.+)"/);
    if (match?.[1]) {
      return match[1];
    }
  }
  const date = new Date().toISOString().slice(0, 10);
  return `${entity}-${date}.csv`;
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

async function downloadEntity(entity) {
  downloading[entity] = true;
  try {
    const response = await fetch(`/api/exports/${entity}`, {
      headers: buildHeaders(),
      credentials: "include",
    });
    if (!response.ok) {
      throw new Error(`Failed to download ${entity}`);
    }
    const blob = await response.blob();
    const filename = filenameFromResponse(entity, response);
    triggerDownload(blob, filename);
  } catch (error) {
    toast.add({ severity: "error", summary: "Export failed", detail: error.message || "Try again" });
  } finally {
    downloading[entity] = false;
  }
}

async function downloadAll() {
  if (!selectedEntities.value.length) {
    toast.add({ severity: "warn", summary: "Select entities", detail: "Pick one or more exports" });
    return;
  }
  exportingAll.value = true;
  try {
    const payload = selectedEntities.value.join(",");
    const data = await api.get(`/api/exports/all?entities=${encodeURIComponent(payload)}`);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const suffix = new Date().toISOString().slice(0, 10);
    const filename = `exports-${suffix}.json`;
    triggerDownload(blob, filename);
    toast.add({ severity: "success", summary: "Export ready", detail: "JSON package downloaded" });
  } catch (error) {
    toast.add({ severity: "error", summary: "Export failed", detail: error.message || "Try again" });
  } finally {
    exportingAll.value = false;
  }
}
</script>

<style scoped>
.exports-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 1rem;
  margin-bottom: 1rem;
}

.export-card {
  padding: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  min-height: 160px;
  justify-content: space-between;
}

.export-all-card {
  padding: 1.25rem;
}

.export-all-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: center;
}

.multi-select {
  min-width: 220px;
  flex: 1;
}

.muted {
  color: var(--text-secondary);
  font-size: 0.9rem;
}
</style>

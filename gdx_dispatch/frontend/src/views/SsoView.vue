<template>
    <section class="sso-view view-card" data-testid="sso-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title">SSO Configuration</h2>
        </template>
        <template #end>
          <Button
            label="Test Connection"
            icon="pi pi-plug"
            severity="info"
            :loading="testing"
            data-testid="sso-test-btn"
            @click="testConnection"
          />
        </template>
      </Toolbar>

      <div class="form-grid sso-form" data-testid="sso-form">
        <div class="form-field">
          <label>Provider</label>
          <Select
            v-model="ssoForm.provider"
            :options="providerOptions"
            optionLabel="label"
            optionValue="value"
            class="w-full"
            data-testid="sso-provider"
          />
        </div>
        <div class="form-field full-width">
          <label>Entity ID</label>
          <InputText v-model="ssoForm.entity_id" data-testid="sso-entity" class="w-full" />
        </div>
        <div class="form-field full-width">
          <label>Metadata URL</label>
          <InputText v-model="ssoForm.metadata_url" data-testid="sso-metadata" class="w-full" />
        </div>
        <div class="form-field full-width">
          <label>Certificate</label>
          <Textarea v-model="ssoForm.cert" rows="4" data-testid="sso-cert" class="w-full" />
        </div>
        <div class="form-field">
          <label>Active</label>
          <ToggleSwitch v-model="ssoForm.active" data-testid="sso-active" />
        </div>
        <div class="form-field actions">
          <Button
            label="Save Settings"
            icon="pi pi-save"
            severity="success"
            :loading="saving"
            data-testid="sso-save-btn"
            @click="saveSettings"
          />
        </div>
      </div>

      <div class="filter-tabs" data-testid="sso-tabs">
        <Button
          v-for="filter in logFilters"
          :key="filter.value"
          :label="filter.label + (logCounts[filter.value] ? ` (${logCounts[filter.value]})` : '')"
          :severity="logFilter === filter.value ? undefined : 'secondary'"
          size="small"
          @click="logFilter = filter.value"
        />
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
      responsiveLayout="scroll"
        v-else
        :value="filteredLogs"
        paginator
        :rows="10"
        striped-rows
        data-testid="sso-logs"
      >
        <Column field="timestamp" header="Timestamp" style="width:170px">
          <template #body="{ data }">{{ formatTimestamp(data.timestamp) }}</template>
        </Column>
        <Column field="user" header="User" />
        <Column field="event" header="Event" />
        <Column field="status" header="Status" style="width:120px">
          <template #body="{ data }">
            <Tag :value="data.status?.toUpperCase()" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="ip" header="IP" style="width:140px" />
        <Column header="Actions" style="width:120px">
          <template #body="{ data }">
            <Button
              icon="pi pi-eye"
              text
              size="small"
              data-testid="sso-log-view"
              @click.stop="viewLog(data)"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showLogDialog" header="Log Details" modal :style="{ width: '520px' }" data-testid="sso-log-dialog">
        <div v-if="selectedLog">
          <p><strong>User:</strong> {{ selectedLog.user || '—' }}</p>
          <p><strong>Event:</strong> {{ selectedLog.event || '—' }}</p>
          <p><strong>Status:</strong> {{ selectedLog.status || '—' }}</p>
          <p><strong>Timestamp:</strong> {{ formatTimestamp(selectedLog.timestamp) }}</p>
          <p v-if="selectedLog.details"><strong>Details:</strong> {{ selectedLog.details }}</p>
        </div>
        <template #footer>
          <Button label="Close" severity="secondary" @click="showLogDialog = false" />
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
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";
import ToggleSwitch from "primevue/toggleswitch";

const api = useApiWithToast();

const ssoForm = ref({
  provider: "google",
  entity_id: "",
  metadata_url: "",
  cert: "",
  active: false,
});
const auditLogs = ref([]);
const loading = ref(true);
const saving = ref(false);
const testing = ref(false);
const logFilter = ref("all");
const showLogDialog = ref(false);
const selectedLog = ref(null);

const providerOptions = [
  { label: "Google", value: "google" },
  { label: "Microsoft", value: "microsoft" },
  { label: "Okta", value: "okta" },
  { label: "Custom", value: "custom" },
];

const logFilters = [
  { label: "All", value: "all" },
  { label: "Success", value: "success" },
  { label: "Failure", value: "failure" },
];

const filteredLogs = computed(() => {
  if (logFilter.value === "all") return auditLogs.value;
  return auditLogs.value.filter((log) => log.status?.toLowerCase() === logFilter.value);
});

const logCounts = computed(() => {
  const counts = { all: auditLogs.value.length, success: 0, failure: 0 };
  auditLogs.value.forEach((log) => {
    const normalized = log.status?.toLowerCase();
    if (normalized === "success") counts.success += 1;
    if (normalized === "failure") counts.failure += 1;
  });
  return counts;
});

function formatTimestamp(value) {
  return value ? value.replace("T", " @ ").split(".")[0] : "—";
}

function statusSeverity(status) {
  if (!status) return "secondary";
  const normalized = status.toLowerCase();
  return normalized === "success" ? "success" : normalized === "failure" ? "danger" : "info";
}

async function loadSso() {
  loading.value = true;
  try {
    const data = await api.get("/api/sso");
    if (data?.config) {
      ssoForm.value = { ...ssoForm.value, ...data.config };
    }
    auditLogs.value = Array.isArray(data?.logs)
      ? data.logs
      : Array.isArray(data)
        ? data
        : [];
  } finally {
    loading.value = false;
  }
}

async function saveSettings() {
  saving.value = true;
  try {
    await api.patch("/api/sso", ssoForm.value, { successMessage: "SSO settings saved" });
    await loadSso();
  } finally {
    saving.value = false;
  }
}

async function testConnection() {
  testing.value = true;
  try {
    await api.post("/api/sso/test-connection", ssoForm.value, { successMessage: "Connection validated" });
  } finally {
    testing.value = false;
  }
}

function viewLog(log) {
  selectedLog.value = log;
  showLogDialog.value = true;
}

onMounted(loadSso);
</script>

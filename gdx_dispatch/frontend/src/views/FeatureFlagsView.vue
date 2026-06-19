<template>
    <section class="feature-flags-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Feature Flags</h2>
        </template>
        <template #end>
          <Button label="+ New Flag" icon="pi pi-plus" @click="openCreateDialog" />
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        v-else
        :value="featureFlags"
        striped-rows
        responsiveLayout="scroll"
        emptyMessage="No feature flags yet"
      >
        <Column field="name" header="Name" />
        <Column field="description" header="Description">
          <template #body="{ data }">
            <span>{{ data.description || '—' }}</span>
          </template>
        </Column>
        <Column header="Enabled" style="width:130px">
          <template #body="{ data }">
            <ToggleSwitch
              :model-value="data.enabled"
              :disabled="busyFlag[data.name]"
              @update:model-value="(value) => toggleFlag(data, value)"
            />
          </template>
        </Column>
        <Column header="Actions" style="width: 120px">
          <template #body="{ data }">
            <Button
              icon="pi pi-trash" aria-label="Delete"
              severity="danger"
              text
              size="small"
              :disabled="busyFlag[data.name]"
              @click="deleteFlag(data)"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        header="New Feature Flag"
        modal
        :style="{ width: '420px' }"
      >
        <div class="form-field">
          <label for="feature-name">Flag key</label>
          <InputText
            id="feature-name"
            v-model="flagForm.name"
            placeholder="Lowercase letters, numbers, _"
            class="w-full"
          />
        </div>
        <div class="form-field">
          <label for="feature-description">Description</label>
          <Textarea
            id="feature-description"
            v-model="flagForm.description"
            rows="3"
            class="w-full"
          />
        </div>
        <div class="form-field toggle-field">
          <label>Enabled by default</label>
          <ToggleSwitch v-model="flagForm.enabled" />
        </div>
        <template #footer>
          <Button label="Cancel" text @click="showDialog = false" />
          <Button
            label="Create"
            icon="pi pi-check"
            :loading="saving"
            @click="saveFlag"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { onMounted, reactive, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Textarea from "primevue/textarea";
import ToggleSwitch from "primevue/toggleswitch";
import Toolbar from "primevue/toolbar";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const featureFlags = ref([]);
const loading = ref(false);
const showDialog = ref(false);
const saving = ref(false);
const busyFlag = reactive({});
const flagForm = reactive({ name: "", description: "", enabled: false });

async function loadFlags() {
  loading.value = true;
  try {
    const data = await api.get("/api/tenant/feature-flags");
    featureFlags.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

function openCreateDialog() {
  flagForm.name = "";
  flagForm.description = "";
  flagForm.enabled = false;
  showDialog.value = true;
}

async function saveFlag() {
  if (!flagForm.name.trim()) return;
  saving.value = true;
  try {
    await api.post("/api/tenant/feature-flags", {
      name: flagForm.name.trim(),
      description: flagForm.description.trim() || null,
      enabled: flagForm.enabled,
    }, { successMessage: "Feature flag saved" });
    showDialog.value = false;
    await loadFlags();
  } finally {
    saving.value = false;
  }
}

async function toggleFlag(flag, value) {
  busyFlag[flag.name] = true;
  try {
    const endpoint = `/api/tenant/feature-flags/${encodeURIComponent(flag.name)}/${value ? "enable" : "disable"}`;
    await api.post(endpoint, {}, { successMessage: `Feature ${value ? "enabled" : "disabled"}` });
    await loadFlags();
  } finally {
    busyFlag[flag.name] = false;
  }
}

async function deleteFlag(flag) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete feature flag ${flag.name}? This cannot be undone.` }))) return;
  busyFlag[flag.name] = true;
  try {
    await api.del(`/api/tenant/feature-flags/${encodeURIComponent(flag.name)}`, { successMessage: "Flag removed" });
    await loadFlags();
  } finally {
    busyFlag[flag.name] = false;
  }
}

onMounted(() => {
  loadFlags();
});
</script>

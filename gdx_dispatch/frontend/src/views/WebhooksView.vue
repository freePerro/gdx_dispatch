<template>
    <section class="webhooks-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Webhooks</h2>
        </template>
        <template #end>
          <Button label="+ New Subscription" icon="pi pi-plus" severity="primary" @click="openCreate" />
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <div v-else>
        <DataTable
      responsiveLayout="scroll" :value="subscriptions" striped-rows>
          <template #empty>
            <EmptyState icon="pi pi-bell" title="No webhook subscriptions" message="Create a webhook to push event data to an external URL." />
          </template>
          <Column field="name" header="Name" />
          <Column field="url" header="URL">
            <template #body="{ data }">
              <div class="url-cell">{{ data.url }}</div>
            </template>
          </Column>
          <Column header="Events">
            <template #body="{ data }">
              <div class="event-list">
                <Tag v-for="event in data.events" :key="event" :value="event" class="event-chip" />
              </div>
            </template>
          </Column>
          <Column header="Status">
            <template #body="{ data }">
              <Tag :value="data.active ? 'Active' : 'Disabled'" :severity="data.active ? 'success' : 'danger'" />
            </template>
          </Column>
          <Column header="Actions" :style="{ width: '250px' }">
            <template #body="{ data }">
              <Button icon="pi pi-bolt" severity="info" text size="small" @click.stop="testSubscription(data)" :loading="testing === data.id" />
              <Button icon="pi pi-history" severity="secondary" text size="small" @click.stop="openDeliveries(data)" />
              <Button icon="pi pi-pencil" aria-label="Edit" text size="small" @click.stop="openEdit(data)" />
              <Button icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click.stop="confirmDelete(data)" />
            </template>
          </Column>
        </DataTable>
      </div>

      <Dialog v-model:visible="showDialog" :header="editing ? 'Edit subscription' : 'New subscription'" :style="{ width: '520px' }" modal>
        <div class="form-grid">
          <div class="form-field">
            <label class="form-label">Name *</label>
            <InputText v-model="form.name" class="w-full" maxlength="200" />
          </div>
          <div class="form-field full-width">
            <label class="form-label">URL *</label>
            <InputText v-model="form.url" class="w-full" maxlength="2048" />
          </div>
          <div class="form-field full-width">
            <label class="form-label">Secret</label>
            <InputText v-model="form.secret" class="w-full" maxlength="200" />
          </div>
          <div class="form-field full-width">
            <label class="form-label">Events *</label>
            <Select
              v-model="form.events"
              :options="eventOptions"
              optionLabel="label"
              optionValue="value"
              multiple
              class="w-full"
            />
          </div>
          <div class="form-field full-width">
            <label class="form-label">Active</label>
            <ToggleSwitch v-model="form.active" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="closeDialog" />
          <Button label="Save" icon="pi pi-save" :loading="saving" @click="saveSubscription" />
        </template>
      </Dialog>

      <Drawer v-model:visible="deliveryDrawerVisible" position="right" :style="{ width: '420px' }">
        <div class="drawer-header">
          <h3>Delivery history</h3>
          <Button icon="pi pi-times" aria-label="Remove" class="p-button-text" @click="deliveryDrawerVisible = false" />
        </div>
        <div v-if="deliveryLoading" class="spinner-wrap">
          <ProgressSpinner />
        </div>
        <div v-else>
          <DataTable
      responsiveLayout="scroll" :value="deliveries" striped-rows>
            <Column field="event" header="Event" />
            <Column header="Status">
              <template #body="{ data }">
                <Tag
                  :value="data.response_status || 'Unknown'"
                  :severity="deliverySeverity(data.response_status)"
                />
              </template>
            </Column>
            <Column header="Delivered">
              <template #body="{ data }">{{ data.delivered_at?.split('T')[0] || '—' }}</template>
            </Column>
            <Column header="Duration">
              <template #body="{ data }">{{ data.duration_ms ? data.duration_ms + 'ms' : '—' }}</template>
            </Column>
            <Column header="Error">
              <template #body="{ data }">{{ data.error || 'None' }}</template>
            </Column>
          </DataTable>
        </div>
      </Drawer>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import EmptyState from "../components/EmptyState.vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Select from "primevue/select";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";
import ToggleSwitch from "primevue/toggleswitch";
import ProgressSpinner from "primevue/progressspinner";
import Drawer from "primevue/drawer";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();
const subscriptions = ref([]);
const events = ref([]);
const loading = ref(true);
const showDialog = ref(false);
const editing = ref(false);
const saving = ref(false);
const testing = ref(null);
const deliveryDrawerVisible = ref(false);
const deliveries = ref([]);
const deliveryLoading = ref(false);

const form = reactive({
  id: null,
  name: "",
  url: "",
  secret: "",
  events: [],
  active: true,
});

const eventOptions = computed(() => events.value.map((evt) => ({ label: evt, value: evt })));

async function loadSubscriptions() {
  loading.value = true;
  try {
    const data = await api.get("/api/webhooks/subscriptions");
    subscriptions.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

async function loadEvents() {
  try {
    const data = await api.get("/api/webhooks/events");
    events.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    events.value = [];
  }
}

function resetForm() {
  form.id = null;
  form.name = "";
  form.url = "";
  form.secret = "";
  form.events = [];
  form.active = true;
}

function openCreate() {
  editing.value = false;
  resetForm();
  showDialog.value = true;
}

function openEdit(sub) {
  editing.value = true;
  form.id = sub.id;
  form.name = sub.name;
  form.url = sub.url;
  form.secret = "";
  form.events = Array.isArray(sub.events) ? [...sub.events] : [];
  form.active = sub.active;
  showDialog.value = true;
}

function closeDialog() {
  showDialog.value = false;
}

async function saveSubscription() {
  if (!form.name.trim() || !form.url.trim() || !form.events.length) return;
  saving.value = true;
  try {
    const payload = {
      name: form.name,
      url: form.url,
      events: form.events,
      active: form.active,
    };
    if (form.secret.trim()) {
      payload.secret = form.secret.trim();
    }

    if (editing.value && form.id) {
      await api.patch(`/api/webhooks/subscriptions/${form.id}`, payload, { successMessage: "Subscription updated" });
    } else {
      await api.post("/api/webhooks/subscriptions", payload, { successMessage: "Subscription created" });
    }
    showDialog.value = false;
    await loadSubscriptions();
  } finally {
    saving.value = false;
  }
}

async function confirmDelete(sub) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete webhook ${sub.name}?` }))) return;
  await api.del(`/api/webhooks/subscriptions/${sub.id}`, { successMessage: "Subscription deleted" });
  await loadSubscriptions();
}

async function testSubscription(sub) {
  testing.value = sub.id;
  try {
    await api.post(`/api/webhooks/subscriptions/${sub.id}/test`, {}, { successMessage: "Test sent" });
  } finally {
    testing.value = null;
  }
}

async function loadDeliveries(subId) {
  deliveryLoading.value = true;
  try {
    const data = await api.get(`/api/webhooks/subscriptions/${subId}/deliveries`);
    deliveries.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    deliveryLoading.value = false;
  }
}

function deliverySeverity(status) {
  if (!status) return "warning";
  return status >= 200 && status < 300 ? "success" : "danger";
}

async function openDeliveries(sub) {
  await loadDeliveries(sub.id);
  deliveryDrawerVisible.value = true;
}

onMounted(async () => {
  await Promise.all([loadEvents(), loadSubscriptions()]);
});
</script>

<style scoped>
.webhooks-view .page-title {
  margin: 0;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}
.full-width {
  grid-column: 1 / -1;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.form-label {
  font-weight: 600;
  color: var(--p-text-muted-color);
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}
.url-cell {
  word-break: break-all;
  font-size: 0.9rem;
  color: var(--p-text-muted-color);
}
.event-list {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}
.event-chip {
  font-size: 0.75rem;
}
.drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 1rem;
}
.status-chip {
  padding: 0.25rem 0.8rem;
  border-radius: 999px;
  font-size: 0.75rem;
}
</style>

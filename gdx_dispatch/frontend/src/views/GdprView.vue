<template>
    <section class="gdpr-view view-card">
      <Toolbar>
        <template #start>
          <InputText
            id="gdpr-customer-search"
            name="gdpr-customer-search"
            v-model="searchQuery"
            placeholder="Search customers by name, email, or phone..."
            class="search-input"
          />
        </template>
        <template #end>
          <Button label="Export My Data" icon="pi pi-download" severity="secondary" @click="exportMyData" />
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable v-else :value="filteredCustomers" striped-rows responsiveLayout="scroll" emptyMessage="No customers match">
        <Column field="name" header="Customer">
          <template #body="{ data }">
            <strong>{{ data.name || data.customer_name || 'Unknown' }}</strong>
            <div class="muted">{{ data.email || data.contact_email || 'No email' }}</div>
          </template>
        </Column>
        <Column field="phone" header="Phone" style="width: 160px">
          <template #body="{ data }">{{ formatPhone(data.phone) || formatPhone(data.primary_phone) || '—' }}</template>
        </Column>
        <Column field="created_at" header="Joined" style="width: 140px">
          <template #body="{ data }">{{ (data.created_at || data.added_at || '').split('T')[0] || '—' }}</template>
        </Column>
        <Column header="Actions" style="width: 360px">
          <template #body="{ data }">
            <Button
              label="Export JSON"
              icon="pi pi-download"
              class="me-2"
              size="small"
              @click.stop="exportCustomer(data)"
            />
            <Button
              label="Delete + Redact PII"
              icon="pi pi-trash" aria-label="Delete"
              size="small"
              severity="danger"
              class="me-2"
              @click.stop="prepareAction('delete', data)"
            />
            <Button
              label="CCPA Opt-out"
              icon="pi pi-ban"
              size="small"
              severity="warn"
              @click.stop="prepareAction('ccpa', data)"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showConfirmDialog"
        header="Confirm Destructive Action"
        modal
        :style="{ width: '420px' }"
      >
        <p>
          {{ actionCopy }}
        </p>
        <p class="muted">
          Type <strong>DELETE</strong> to confirm. This cannot be undone.
        </p>
        <div class="form-field">
          <label for="gdpr-confirm">Confirmation</label>
          <InputText
            id="gdpr-confirm"
            v-model="confirmationInput"
            placeholder="Type DELETE to continue"
          />
        </div>
        <template #footer>
          <Button label="Cancel" text @click="closeDialog" />
          <Button
            label="Confirm"
            severity="danger"
            :disabled="!isConfirmationValid"
            :loading="actionLoading"
            @click="confirmAction"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatPhone } from "../composables/useFormatters";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();
const toast = useToast();

const customers = ref([]);
const loading = ref(false);
const searchQuery = ref("");
const showConfirmDialog = ref(false);
const actionCustomer = ref(null);
const actionType = ref("");
const actionLoading = ref(false);
const confirmationInput = ref("");

const filteredCustomers = computed(() => {
  const term = searchQuery.value.trim().toLowerCase();
  if (!term) return customers.value;
  return customers.value.filter((customer) => {
    const values = [customer.name, customer.customer_name, customer.email, customer.contact_email, customer.phone, customer.primary_phone];
    return values.some((value) => (value || "").toLowerCase().includes(term));
  });
});

const actionCopy = computed(() => {
  if (!actionCustomer.value) return "";
  const name = actionCustomer.value.name || actionCustomer.value.customer_name || actionCustomer.value.email || "this customer";
  return actionType.value === "delete"
    ? `Delete and redact personal data for ${name}.`
    : `Mark ${name} as opted-out under CCPA.`;
});

const isConfirmationValid = computed(() => confirmationInput.value.trim().toUpperCase() === "DELETE");

async function loadCustomers() {
  loading.value = true;
  try {
    const data = await api.get("/api/customers?per_page=500");
    const list = Array.isArray(data) ? data : data?.items || data?.data || [];
    customers.value = list;
  } finally {
    loading.value = false;
  }
}

function closeDialog() {
  showConfirmDialog.value = false;
  confirmationInput.value = "";
  actionCustomer.value = null;
  actionType.value = "";
}

function prepareAction(type, customer) {
  actionType.value = type;
  actionCustomer.value = customer;
  confirmationInput.value = "";
  showConfirmDialog.value = true;
}

async function confirmAction() {
  if (!actionCustomer.value || !actionType.value) {
    return;
  }
  actionLoading.value = true;
  try {
    const id = actionCustomer.value.id;
    if (!id) throw new Error("Customer ID missing");
    const endpoint = actionType.value === "delete"
      ? `/api/gdpr/delete-customer/${encodeURIComponent(id)}`
      : `/api/ccpa/opt-out/${encodeURIComponent(id)}`;
    await api.post(endpoint, {}, {
      successMessage: actionType.value === "delete" ? "Customer data deleted" : "Customer opted out",
    });
    await loadCustomers();
    closeDialog();
  } finally {
    actionLoading.value = false;
  }
}

async function exportCustomer(customer) {
  try {
    const id = customer.id;
    if (!id) throw new Error("Customer ID missing");
    const payload = await api.get(`/api/gdpr/export-customer/${encodeURIComponent(id)}`);
    downloadJson(payload, `${customer.name || id}-gdpr-export.json`);
    toast.add({ severity: "success", summary: "Export ready", detail: "Customer data downloaded.", life: 2500 });
  } catch (error) {
    // errors surfaced by useApiWithToast
  }
}

async function exportMyData() {
  try {
    const payload = await api.get("/api/gdpr/export-my-data");
    downloadJson(payload, "my-data-export.json");
    toast.add({ severity: "success", summary: "Export ready", detail: "Your data download started.", life: 2500 });
  } catch (error) {
    // handled
  }
}

function downloadJson(data, filename) {
  if (!data) return;
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

onMounted(() => {
  loadCustomers();
});
</script>

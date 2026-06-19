<template>
    <section class="co-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Booking Slots</h2>
        </template>
        <template #end>
          <Button
            data-testid="booking-refresh-btn"
            label="Refresh"
            icon="pi pi-sync"
            @click="loadSlots"
          />
        </template>
      </Toolbar>

      <div class="filter-tabs">
        <Button
          v-for="tab in statusTabs"
          :key="tab"
          :label="`${tab} (${counts[tab] || 0})`"
          :severity="statusFilter === tab ? undefined : 'secondary'"
          size="small"
          :data-testid="`booking-tab-${tab}`"
          @click="statusFilter = tab"
        />
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-else
        :value="filtered"
        paginator
        :rows="20"
        striped-rows
        :empty-message="emptyMessage"
        @row-click="openEdit($event.data)"
        
      >
        <Column header="Date" style="width: 130px">
          <template #body="{ data }">{{ formatDate(data.date) }}</template>
        </Column>
        <Column field="time" header="Time" style="width: 110px" />
        <Column field="service" header="Service" />
        <Column field="customer" header="Customer" />
        <Column field="status" header="Status" style="width: 140px">
          <template #body="{ data }">
            <Tag :value="normalizeStatus(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="selectedSlot ? `Edit Slot for ${selectedSlot.customer || selectedSlot.customer_name || 'Customer'}` : 'Edit Slot'"
        modal
        :style="{ width: '520px' }"
        @hide="closeDialog"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Date</label>
            <DatePicker
              data-testid="booking-date-input"
              v-model="form.date"
              class="w-full"
              :show-icon="true"
            />
          </div>
          <div class="form-field">
            <label>Time</label>
            <InputText
              data-testid="booking-time-input"
              v-model="form.time"
              placeholder="14:30"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Service</label>
            <InputText
              data-testid="booking-service-input"
              v-model="form.service"
              class="w-full"
            />
          </div>
          <div class="form-field full-width">
            <label>Customer</label>
            <InputText
              data-testid="booking-customer-input"
              v-model="form.customer"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Status</label>
            <Select
              data-testid="booking-status-select"
              v-model="form.status"
              :options="statusTabs"
              class="w-full"
            />
          </div>
        </div>

        <div class="dialog-actions" style="margin-bottom: 1rem; display: flex; gap: .5rem; flex-wrap: wrap;">
          <Button
            data-testid="booking-confirm-btn"
            label="Confirm"
            severity="success"
            size="small"
            @click="confirmSlot"
          />
          <Button
            data-testid="booking-reschedule-btn"
            label="Reschedule"
            severity="info"
            size="small"
            @click="rescheduleSlot"
          />
          <Button
            data-testid="booking-cancel-btn"
            label="Cancel"
            severity="danger"
            size="small"
            @click="cancelSlot"
          />
        </div>

        <template #footer>
          <Button
            data-testid="booking-dialog-close-btn"
            label="Close"
            severity="secondary"
            @click="showDialog = false"
          />
          <Button
            data-testid="booking-save-btn"
            label="Save changes"
            icon="pi pi-check"
            :loading="saving"
            @click="saveSlot"
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
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();
const slots = ref([]);
const loading = ref(true);
const statusFilter = ref('upcoming');
const showDialog = ref(false);
const selectedSlot = ref(null);
const saving = ref(false);

const statusTabs = ['upcoming', 'pending', 'confirmed', 'cancelled'];

const emptyForm = () => ({
  date: null,
  time: '',
  service: '',
  customer: '',
  status: statusTabs[0],
});
const form = ref(emptyForm());

const counts = computed(() => {
  const result = {};
  statusTabs.forEach((tab) => (result[tab] = 0));
  slots.value.forEach((slot) => {
    const status = normalizeStatus(slot.status);
    if (result[status] !== undefined) {
      result[status] += 1;
    }
  });
  return result;
});

const filtered = computed(() =>
  slots.value.filter((slot) => normalizeStatus(slot.status) === statusFilter.value)
);

function normalizeStatus(value) {
  return typeof value === 'string' ? value.toLowerCase() : 'upcoming';
}

function statusSeverity(value) {
  const normalized = normalizeStatus(value);
  return {
    upcoming: 'info',
    pending: 'warning',
    confirmed: 'success',
    cancelled: 'danger',
  }[normalized] || 'secondary';
}

function formatDate(value) {
  if (!value) return '—';
  return value.split('T')[0];
}

async function loadSlots() {
  loading.value = true;
  try {
    const data = await api.get('/api/booking');
    slots.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

function openEdit(slot) {
  selectedSlot.value = slot;
  form.value = {
    date: slot.date ? new Date(slot.date) : null,
    time: slot.time || '',
    service: slot.service || '',
    customer: slot.customer || slot.customer_name || '',
    status: normalizeStatus(slot.status),
  };
  showDialog.value = true;
}

function closeDialog() {
  showDialog.value = false;
  selectedSlot.value = null;
  form.value = emptyForm();
}

function payloadFromForm(overrides = {}) {
  const dateValue = form.value.date ? form.value.date.toISOString().split('T')[0] : null;
  return {
    date: dateValue,
    time: form.value.time,
    service: form.value.service,
    customer: form.value.customer,
    status: form.value.status,
    ...overrides,
  };
}

async function submitSlot(overrides = {}, options = {}) {
  if (!selectedSlot.value) return;
  saving.value = true;
  try {
    await api.patch(`/api/booking/${selectedSlot.value.id}`, payloadFromForm(overrides), options);
    await loadSlots();
    closeDialog();
  } finally {
    saving.value = false;
  }
}

function saveSlot() {
  submitSlot({}, { successMessage: 'Booking updated' });
}

function confirmSlot() {
  form.value.status = 'confirmed';
  submitSlot({ status: 'confirmed' }, { successMessage: 'Slot confirmed' });
}

function rescheduleSlot() {
  form.value.status = 'upcoming';
  submitSlot({ status: 'upcoming' }, { successMessage: 'Slot rescheduled' });
}

function cancelSlot() {
  form.value.status = 'cancelled';
  submitSlot({ status: 'cancelled' }, { successMessage: 'Slot cancelled' });
}

const emptyMessage = 'No booking slots found';

onMounted(loadSlots);
</script>

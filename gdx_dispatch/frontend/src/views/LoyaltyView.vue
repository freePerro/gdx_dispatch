<template>
    <section class="loyalty-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Loyalty</h2>
        </template>
        <template #end>
          <div class="toolbar-actions">
            <label class="toggle-label">
              <span class="toggle-copy">Above base tier only</span>
              <ToggleSwitch v-model="eliteOnly" data-testid="loyalty-toggle-elite" />
            </label>
            <Button
              label="+ Award Points"
              icon="pi pi-star"
              class="primary-action"
              data-testid="loyalty-dialog-btn"
              @click="openDialog"
            />
          </div>
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <div v-else-if="loadError" class="empty-state" data-testid="loyalty-load-error">
        <h3>Loyalty members could not be loaded</h3>
        <p>{{ loadError }}</p>
        <Button label="Retry" severity="secondary" size="small" @click="loadLoyalty" />
      </div>

      <DataTable
        v-else
        responsiveLayout="scroll"
        :value="filteredMembers"
        striped-rows
        :paginator="filteredMembers.length > 15"
        :rows="15"
        data-testid="loyalty-members-table"
      >
        <template #empty>
          <div class="empty-state">
            <h3>No loyalty members</h3>
            <p>Customers appear here once they have been awarded points.</p>
          </div>
        </template>
        <Column header="Customer">
          <template #body="{ data }">
            <router-link
              v-if="data.customer_name && !data.customer_deleted"
              :to="`/customers/${data.customer_id}`"
              data-testid="loyalty-member-link"
            >{{ data.customer_name }}</router-link>
            <span v-else-if="data.customer_name" class="muted">{{ data.customer_name }} (deleted)</span>
            <span v-else class="muted">{{ data.customer_id }}</span>
          </template>
        </Column>
        <Column field="points" header="Points" />
        <Column header="Tier">
          <template #body="{ data }">
            <Tag :value="tierLabel(data.tier)" :severity="tierSeverity(data.tier)" />
          </template>
        </Column>
        <Column header="First points" style="width:140px">
          <template #body="{ data }">{{ formatDate(data.joined_at) }}</template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showDialog" header="Award points" modal :style="{ width: '560px', maxWidth: '95vw' }">
        <div class="form-grid">
          <div class="form-field full-width">
            <label for="loyalty-customer">Customer</label>
            <AutoComplete
              id="loyalty-customer"
              v-model="customerPick"
              :suggestions="customerSuggestions"
              option-label="label"
              forceSelection
              placeholder="Search name, email or phone…"
              class="w-full"
              data-testid="loyalty-customer"
              @complete="onCustomerComplete"
            />
          </div>
          <div class="form-field">
            <label for="loyalty-points">Points</label>
            <InputNumber
              id="loyalty-points"
              v-model="form.amount"
              mode="decimal"
              :min="1"
              :max="10000000"
              class="w-full"
              data-testid="loyalty-points"
            />
          </div>
          <div class="form-field">
            <label for="loyalty-reason">Reason</label>
            <InputText
              id="loyalty-reason"
              v-model="form.reason"
              maxlength="200"
              class="w-full"
              data-testid="loyalty-reason"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" data-testid="loyalty-cancel-btn" @click="showDialog = false" />
          <Button
            label="Award points"
            icon="pi pi-check"
            :loading="saving"
            :disabled="!canSave"
            data-testid="loyalty-save-btn"
            @click="saveEntry"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDate } from '../composables/useFormatters';
import AutoComplete from 'primevue/autocomplete';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';
import ToggleSwitch from 'primevue/toggleswitch';

// Reads the real points ledger (GET /api/loyalty/members) and awards through
// POST /api/loyalty/customers/{id}/points. Until GDXA-316 this page read a
// ui_compat stub that always answered an empty list, and posted to
// /api/loyalty/adjust and /redeem, which never existed. There is no
// redemption store, so the page no longer offers one.
const api = useApiWithToast();
const loading = ref(true);
const loadError = ref('');
const members = ref([]);
const showDialog = ref(false);
const saving = ref(false);
const eliteOnly = ref(false);
const customerPick = ref(null);
const customerSuggestions = ref([]);
const form = ref(emptyForm());

function emptyForm() {
  return { amount: null, reason: '' };
}

// A tier name is configurable, so "above base" is any tier with a discount_pct.
// Nothing in pricing reads that percentage yet, so the label does not promise
// a discount.
const filteredMembers = computed(() => {
  if (!eliteOnly.value) return members.value;
  return members.value.filter((member) => Number(member.tier_discount_pct) > 0);
});

const canSave = computed(() =>
  Boolean(customerPick.value?.value)
  && Number(form.value.amount) > 0
  && form.value.reason.trim().length > 0
);

function tierLabel(value) {
  return value ? value.replace('_', ' ').toUpperCase() : 'Member';
}

function tierSeverity(value) {
  const tier = (value || '').toLowerCase();
  if (tier === 'platinum' || tier === 'diamond') return 'success';
  if (tier === 'gold') return 'warn';
  return 'info';
}

async function loadLoyalty() {
  loading.value = true;
  loadError.value = '';
  try {
    const data = await api.get('/api/loyalty/members');
    members.value = Array.isArray(data) ? data : [];
  } catch (err) {
    members.value = [];
    loadError.value = err?.message || 'The server did not answer.';
  } finally {
    loading.value = false;
  }
}

async function onCustomerComplete(event) {
  const q = (event?.query || '').trim();
  if (!q) {
    customerSuggestions.value = [];
    return;
  }
  try {
    const rows = await api.get(`/api/customers/search?q=${encodeURIComponent(q)}`);
    customerSuggestions.value = (Array.isArray(rows) ? rows : []).map((c) => ({
      label: c.name || c.email || c.phone || c.id,
      value: c.id,
    }));
  } catch {
    customerSuggestions.value = [];
  }
}

function openDialog() {
  form.value = emptyForm();
  customerPick.value = null;
  customerSuggestions.value = [];
  showDialog.value = true;
}

async function saveEntry() {
  if (!canSave.value) return;
  saving.value = true;
  const customerId = customerPick.value.value;
  try {
    await api.post(
      `/api/loyalty/customers/${encodeURIComponent(customerId)}/points`,
      { amount: Number(form.value.amount), reason: form.value.reason.trim() },
      { successMessage: 'Points awarded' },
    );
    showDialog.value = false;
    await loadLoyalty();
  } catch {
    // useApiWithToast has already shown the error; keep the dialog open.
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  loadLoyalty();
});
</script>

<style scoped>
.page-title {
  margin: 0;
}
.toolbar-actions {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  flex-wrap: wrap;
}
.toggle-label {
  display: flex;
  gap: 0.4rem;
  align-items: center;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
.toggle-copy {
  font-size: 0.85rem;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem 0;
}
.form-grid {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.form-field.full-width {
  grid-column: 1 / -1;
}
.form-field label {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--p-text-muted-color);
}
.w-full {
  width: 100%;
}
.muted {
  color: var(--p-text-muted-color);
}
.empty-state {
  text-align: center;
  padding: 3rem;
  color: var(--p-text-muted-color);
}
.empty-state h3 {
  margin: 1rem 0 0.5rem;
  color: var(--text-color);
}
</style>

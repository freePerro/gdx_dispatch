<template>
  <Dialog
    :visible="visible"
    modal
    class="lead-intake-dialog"
    :header="dialogTitle"
    :style="isMobileViewport ? { width: '100vw', maxWidth: '640px', height: '100dvh', maxHeight: '100dvh' } : { width: '640px', maxWidth: '95vw', maxHeight: '90vh' }"
    :breakpoints="{ '768px': '100vw' }"
    :position="isMobileViewport ? 'bottom' : 'center'"
    @update:visible="emit('update:visible', $event)"
  >
    <!-- Post-submission result view -->
    <div v-if="submittedResult" class="intake-result" data-test="intake-result-pane">
      <div class="result-banner" :class="canStartEstimate ? 'banner-office' : 'banner-tech'">
        <i class="pi pi-check-circle result-icon" aria-hidden="true" />
        <h2 class="result-title">{{ canStartEstimate ? 'Lead saved' : 'Saved — the office will follow up' }}</h2>
        <p v-if="canStartEstimate && submittedResult.lead?.name" class="result-subtitle">
          Lead #{{ submittedResult.lead.id.slice(0, 8) }} for {{ submittedResult.lead.name }}
        </p>
      </div>

      <!-- Duplicate warning banner -->
      <div v-if="submittedResult.possible_duplicate" class="dup-banner" data-test="intake-dup-warning">
        <i class="pi pi-exclamation-triangle" aria-hidden="true" />
        <div>
          <strong>Possible duplicate lead:</strong>&nbsp;<span>An open lead already exists for this contact.</span>
        </div>
      </div>

      <!-- The server returns matched_customer only to customers.read_all, so
           a technician never receives who owns a number (#817 review). -->
      <div v-if="submittedResult.matched_customer" class="match-banner" data-test="intake-match-info">
        <i class="pi pi-user" aria-hidden="true" />
        <div>
          <strong>Matched customer:</strong>&nbsp;<span>{{ submittedResult.matched_customer.name || 'Existing customer' }}</span>
        </div>
      </div>

      <!-- Action buttons -->
      <div class="result-actions">
        <!-- Same keys the start-estimate route requires: leads.write + estimates.write -->
        <Button
          v-if="canStartEstimate"
          label="Start estimate"
          icon="pi pi-file-edit"
          severity="primary"
          class="w-full start-estimate-btn"
          :loading="startingEstimate"
          data-test="intake-start-estimate-btn"
          @click="startEstimate"
        />
        <Button
          label="Done"
          :severity="canStartEstimate ? 'secondary' : 'primary'"
          class="w-full"
          data-test="intake-done-btn"
          @click="closeDialog"
        />
      </div>
    </div>

    <!-- Active intake form -->
    <form v-else class="intake-form" @submit.prevent="save">
      <div v-if="loadError" class="intake-error-banner" data-test="intake-load-error">
        {{ loadError }}
      </div>

      <div class="form-section">
        <h3 class="section-title">Contact</h3>

        <div class="field-row">
          <label for="intake-name" class="field-label required">Customer name</label>
          <InputText
            id="intake-name"
            v-model="form.name"
            placeholder="e.g. Jane Doe or Acme Corp"
            class="w-full"
            required
            data-test="intake-name-input"
          />
        </div>

        <div class="field-row">
          <label for="intake-phone" class="field-label">Phone</label>
          <PhoneInput
            id="intake-phone"
            v-model="form.phone"
            placeholder="(555) 123-4567"
            class="w-full"
            data-test="intake-phone-input"
          />
          <p v-if="phoneMatch.matched" class="phone-hint phone-hint--hit" data-test="intake-phone-match">
            <i class="pi pi-user" aria-hidden="true" />
            <span>Existing customer{{ phoneMatch.name ? `: ${phoneMatch.name}` : '' }}</span>
          </p>
          <p v-else-if="phoneClean && phoneMatch.checked" class="phone-hint phone-hint--miss">
            New customer contact
          </p>
        </div>

        <div class="field-row">
          <label for="intake-email" class="field-label">Email</label>
          <InputText
            id="intake-email"
            v-model="form.email"
            type="email"
            placeholder="caller@example.com"
            class="w-full"
            data-test="intake-email-input"
          />
        </div>

        <div class="field-row">
          <label for="intake-address" class="field-label">Jobsite address</label>
          <InputText
            id="intake-address"
            v-model="form.address"
            placeholder="123 Main St, Springfield"
            class="w-full"
            data-test="intake-address-input"
          />
        </div>
      </div>

      <div class="form-section">
        <h3 class="section-title">Request</h3>

        <!-- Dynamic lead custom fields (Job kind, Door count, Door size, etc.) -->
        <div
          v-for="f in customFieldDefs"
          :key="f.id || f.field_key"
          class="field-row"
          :data-test="`intake-custom-field-${f.field_key}`"
        >
          <label :for="`custom-${f.field_key}`" class="field-label" :class="{ required: f.required }">
            {{ f.label || f.field_name }}
          </label>

          <!-- Select dropdown -->
          <Select
            v-if="f.field_type === 'select'"
            :id="`custom-${f.field_key}`"
            v-model="form.custom_fields[f.field_key]"
            :options="f.options || []"
            placeholder="Choose one..."
            class="w-full"
            :data-test="`intake-input-${f.field_key}`"
          />

          <!-- Number input -->
          <InputNumber
            v-else-if="f.field_type === 'number'"
            :id="`custom-${f.field_key}`"
            v-model="form.custom_fields[f.field_key]"
            class="w-full"
            :data-test="`intake-input-${f.field_key}`"
          />

          <!-- Boolean checkbox -->
          <div v-else-if="f.field_type === 'boolean'" class="bool-field">
            <Checkbox
              :id="`custom-${f.field_key}`"
              v-model="form.custom_fields[f.field_key]"
              :binary="true"
              :data-test="`intake-input-${f.field_key}`"
            />
            <label :for="`custom-${f.field_key}`" class="bool-label">Yes</label>
          </div>

          <!-- Date input -->
          <InputText
            v-else-if="f.field_type === 'date'"
            :id="`custom-${f.field_key}`"
            v-model="form.custom_fields[f.field_key]"
            type="date"
            class="w-full"
            :data-test="`intake-input-${f.field_key}`"
          />

          <!-- Default text input -->
          <InputText
            v-else
            :id="`custom-${f.field_key}`"
            v-model="form.custom_fields[f.field_key]"
            class="w-full"
            :data-test="`intake-input-${f.field_key}`"
          />
        </div>

        <div class="field-row">
          <label for="intake-source" class="field-label">How they found us</label>
          <Select
            id="intake-source"
            v-model="form.source"
            :options="sourcesList"
            placeholder="Select source..."
            class="w-full"
            data-test="intake-source-select"
          />
        </div>

        <div class="field-row">
          <label for="intake-notes" class="field-label">Request notes</label>
          <Textarea
            id="intake-notes"
            v-model="form.notes"
            rows="3"
            placeholder="What does the customer need? (e.g. spring broken, needs quote on 16x7 insulated)"
            class="w-full"
            data-test="intake-notes-input"
          />
        </div>
      </div>

      <div class="intake-footer">
        <Button
          label="Cancel"
          severity="secondary"
          text
          data-test="intake-cancel-btn"
          @click="closeDialog"
        />
        <Button
          type="submit"
          label="Save lead"
          icon="pi pi-check"
          :loading="saving"
          :disabled="!form.name.trim()"
          data-test="intake-save-btn"
        />
      </div>
    </form>
  </Dialog>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import Dialog from 'primevue/dialog';
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import InputNumber from 'primevue/inputnumber';
import Select from 'primevue/select';
import Textarea from 'primevue/textarea';
import Checkbox from 'primevue/checkbox';
import { useToast } from 'primevue/usetoast';
import { useApi } from '../composables/useApi';
import { usePermission } from '../composables/usePermission';
import { useViewMode } from '../composables/useViewMode';
import PhoneInput from './PhoneInput.vue';
import { formatPhone } from '../composables/useFormatters';

const props = defineProps({
  visible: { type: Boolean, default: false },
  initialPhone: { type: String, default: '' },
  initialName: { type: String, default: '' },
  initialEmail: { type: String, default: '' },
  initialAddress: { type: String, default: '' },
  initialNotes: { type: String, default: '' },
  initialSource: { type: String, default: '' },
  originRef: { type: String, default: '' },
});

const emit = defineEmits(['update:visible', 'saved', 'estimate-started']);

const api = useApi();
const router = useRouter();
const toast = useToast();
const { hasPermission } = usePermission();
// A phone gets the full-height bottom sheet; a desk gets a centered dialog.
const { isMobileViewport } = useViewMode();

// Permission-shaped, not role-shaped: the backend gates on these keys.
const canStartEstimate = computed(
  () => hasPermission('leads.write') && hasPermission('estimates.write')
);
// The live phone lookup names the customer who owns a number. Only roles that
// can already read every customer get it — the same rule the intake response
// applies to matched_customer. This form never asks on a tech's behalf, rather
// than asking and hiding the answer. (The endpoint itself does not check.)
const canSeeMatches = computed(() => hasPermission('customers.read_all'));

const dialogTitle = computed(() => {
  return submittedResult.value ? 'Request saved' : 'Estimate request';
});

const saving = ref(false);
const startingEstimate = ref(false);
const loadError = ref('');
// From GET /api/leads/intake-form — the server's list is the one reports read.
const sourcesList = ref([]);
const customFieldDefs = ref([]);
const submittedResult = ref(null);

const form = reactive({
  name: '',
  phone: '',
  email: '',
  address: '',
  source: '',
  notes: '',
  custom_fields: {},
});

const phoneMatch = reactive({
  matched: false,
  name: '',
  checked: false,
});

let phoneDebounceTimer = null;

const phoneClean = computed(() => (form.phone || '').replace(/\D/g, ''));

function resetForm() {
  form.name = props.initialName || '';
  // A call's number arrives as E.164 ("+16125550199"). PhoneInput's mask takes
  // the first ten digits it sees, so the country code must go first or the
  // lead saves as "(161)255-5019" — seen in the browser walk.
  form.phone = formatPhone(props.initialPhone);
  form.email = props.initialEmail || '';
  form.address = props.initialAddress || '';
  form.source = props.initialSource || '';
  form.notes = props.initialNotes || '';
  form.custom_fields = {};
  submittedResult.value = null;
  phoneMatch.matched = false;
  phoneMatch.name = '';
  phoneMatch.checked = false;
}

watch(
  () => props.visible,
  (isOpen) => {
    if (isOpen) {
      resetForm();
      fetchIntakeForm();
      if (form.phone && canSeeMatches.value) checkPhoneMatch(form.phone);
    }
  },
  { immediate: true }
);

watch(
  () => form.phone,
  (newPhone) => {
    phoneMatch.matched = false;
    phoneMatch.name = '';
    phoneMatch.checked = false;
    if (phoneDebounceTimer) clearTimeout(phoneDebounceTimer);
    const digits = (newPhone || '').replace(/\D/g, '');
    if (digits.length >= 7 && canSeeMatches.value) {
      phoneDebounceTimer = setTimeout(() => {
        checkPhoneMatch(digits);
      }, 350);
    }
  }
);

// Permissions can land after the form opens with a prefilled number; look
// the number up then rather than never.
watch(canSeeMatches, (can) => {
  if (can && props.visible && phoneClean.value.length >= 7 && !phoneMatch.checked) {
    checkPhoneMatch(phoneClean.value);
  }
});

async function checkPhoneMatch(rawDigits) {
  try {
    const res = await api.get(`/api/planner/match-phone?phone=${encodeURIComponent(rawDigits)}`);
    if (res?.customer_id) {
      phoneMatch.matched = true;
      phoneMatch.name = res.name || '';
    } else {
      phoneMatch.matched = false;
      phoneMatch.name = '';
    }
    phoneMatch.checked = true;
  } catch {
    phoneMatch.checked = true;
  }
}

async function fetchIntakeForm() {
  loadError.value = '';
  try {
    const res = await api.get('/api/leads/intake-form');
    if (Array.isArray(res?.sources) && res.sources.length) {
      sourcesList.value = res.sources;
    }
    if (Array.isArray(res?.custom_fields)) {
      customFieldDefs.value = res.custom_fields;
      for (const f of res.custom_fields) {
        if (form.custom_fields[f.field_key] === undefined) {
          form.custom_fields[f.field_key] = f.field_type === 'boolean' ? false : null;
        }
      }
    }
  } catch (err) {
    loadError.value = err?.message || 'Failed to load intake definitions';
  }
}

function cleanCustomFields(rawFields) {
  const cleaned = {};
  for (const [k, v] of Object.entries(rawFields || {})) {
    if (v !== null && v !== undefined && v !== '') {
      cleaned[k] = v;
    }
  }
  return cleaned;
}

async function save() {
  if (!form.name.trim() || saving.value) return;
  saving.value = true;
  try {
    const payload = {
      name: form.name.trim(),
      phone: form.phone.trim() || null,
      email: form.email.trim() || null,
      address: form.address.trim() || null,
      source: form.source || null,
      notes: form.notes.trim() || null,
      origin_ref: props.originRef || null,
      custom_fields: cleanCustomFields(form.custom_fields),
    };
    const res = await api.post('/api/leads/intake', payload);
    submittedResult.value = res;
    emit('saved', res);
    toast.add({
      severity: 'success',
      summary: 'Request saved',
      life: 2500,
    });
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Save failed',
      detail: err?.message || 'Could not save lead',
      life: 4000,
    });
  } finally {
    saving.value = false;
  }
}

async function startEstimate() {
  if (!submittedResult.value?.lead?.id || startingEstimate.value) return;
  startingEstimate.value = true;
  const leadId = submittedResult.value.lead.id;
  try {
    const res = await api.post(`/api/leads/${leadId}/start-estimate`);
    toast.add({
      severity: 'success',
      summary: res?.reused ? 'Draft estimate reopened' : 'Draft estimate created',
      life: 2500,
    });
    emit('estimate-started', res);
    closeDialog();
    if (res?.estimate?.id) {
      if (router?.push) router.push(`/estimates/${res.estimate.id}`);
    }
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Could not start estimate',
      detail: err?.message || 'Failed to create estimate draft',
      life: 4000,
    });
  } finally {
    startingEstimate.value = false;
  }
}

function closeDialog() {
  emit('update:visible', false);
}
</script>

<style scoped>
.lead-intake-dialog :deep(.p-dialog-content) {
  padding: 1rem;
  overflow-y: auto;
}

.intake-form {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.form-section {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}

.section-title {
  font-size: 0.95rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--p-text-muted-color, #6b7280);
  margin: 0;
  padding-bottom: 0.25rem;
  border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
}

.field-row {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.field-label {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--p-text-color, #374151);
}

.field-label.required::after {
  content: ' *';
  color: var(--p-red-500, #ef4444);
}

.bool-field {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.25rem 0;
}

.bool-label {
  font-size: 0.875rem;
  cursor: pointer;
}

.phone-hint {
  font-size: 0.8rem;
  margin: 0.25rem 0 0;
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.phone-hint--hit {
  color: var(--p-green-600, #16a34a);
}

.phone-hint--miss {
  color: var(--p-text-muted-color, #6b7280);
}

.intake-footer {
  display: flex;
  justify-content: flex-end;
  gap: 0.75rem;
  padding-top: 1rem;
  margin-top: 0.5rem;
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}

.intake-error-banner {
  background-color: var(--color-danger-bg);
  color: var(--p-text-color);
  padding: 0.75rem;
  border-radius: 6px;
  font-size: 0.875rem;
}

/* Post-submission result view */
.intake-result {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
  padding: 1.5rem 0.5rem;
}

.result-banner {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 1.5rem 1rem;
  border-radius: 8px;
}

.banner-tech {
  background-color: var(--color-success-bg);
  color: var(--p-text-color);
}

.banner-office {
  background-color: var(--color-info-bg);
  color: var(--p-text-color);
}

.result-icon {
  font-size: 2.5rem;
  margin-bottom: 0.75rem;
  color: var(--color-success-500);
}

.result-title {
  font-size: 1.25rem;
  font-weight: 600;
  margin: 0;
}

.result-subtitle {
  font-size: 0.875rem;
  margin: 0.35rem 0 0;
  opacity: 0.85;
}

.dup-banner {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  background-color: var(--color-warning-bg);
  color: var(--p-text-color);
  border: 1px solid var(--color-warning-border);
  border-radius: 6px;
  font-size: 0.875rem;
}

.match-banner {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  background-color: var(--color-info-bg);
  color: var(--p-text-color);
  border: 1px solid var(--color-info-border);
  border-radius: 6px;
  font-size: 0.875rem;
}

.result-actions {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  margin-top: 1rem;
}
</style>

<template>
  <Dialog
    :visible="visible"
    modal
    class="lead-intake-dialog"
    :header="dialogTitle"
    :style="{ width: '100vw', maxWidth: '640px', height: '100dvh', maxHeight: '100dvh' }"
    :breakpoints="{ '768px': '100vw' }"
    position="bottom"
    @update:visible="emit('update:visible', $event)"
  >
    <!-- Post-submission result view -->
    <div v-if="submittedResult" class="intake-result" data-test="intake-result-pane">
      <div class="result-banner" :class="isTech ? 'banner-tech' : 'banner-office'">
        <i class="pi pi-check-circle result-icon" aria-hidden="true" />
        <h2 class="result-title">{{ isTech ? 'Saved — the office will follow up' : 'Lead created successfully' }}</h2>
        <p v-if="!isTech && submittedResult.lead?.name" class="result-subtitle">
          Lead #{{ submittedResult.lead.id.slice(0, 8) }} for {{ submittedResult.lead.name }}
        </p>
      </div>

      <!-- Duplicate warning banner -->
      <div v-if="submittedResult.possible_duplicate" class="dup-banner" data-test="intake-dup-warning">
        <i class="pi pi-exclamation-triangle" aria-hidden="true" />
        <div>
          <strong>Possible duplicate lead:</strong>
          <span> An open lead already exists for this contact.</span>
        </div>
      </div>

      <!-- Customer match info (Office only — redacted for techs to prevent enumeration) -->
      <div v-if="!isTech && submittedResult.matched_customer" class="match-banner" data-test="intake-match-info">
        <i class="pi pi-user" aria-hidden="true" />
        <div>
          <strong>Matched customer:</strong>
          <span> {{ submittedResult.matched_customer.name || 'Existing customer' }}</span>
        </div>
      </div>

      <!-- Action buttons -->
      <div class="result-actions">
        <!-- Start estimate is available for office users holding leads.write + estimates.write -->
        <Button
          v-if="!isTech && canStartEstimate"
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
          :severity="isTech || !canStartEstimate ? 'primary' : 'secondary'"
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
            <span v-if="!isTech && phoneMatch.name">Matched: {{ phoneMatch.name }}</span>
            <span v-else>Matches an existing customer</span>
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
import { getActivePinia } from 'pinia';
import { useAuthStore } from '../stores/auth';
import { usePermission } from '../composables/usePermission';
import { isTechnician } from '../constants/roles';
import PhoneInput from './PhoneInput.vue';

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
let auth = null;
try {
  auth = useAuthStore();
} catch {
  auth = null;
}

let permissionComposable = null;
try {
  permissionComposable = usePermission();
} catch {
  permissionComposable = null;
}

function checkPermission(key) {
  if (permissionComposable?.hasPermission) {
    return permissionComposable.hasPermission(key);
  }
  if (auth?.hasPermission) {
    return auth.hasPermission(key);
  }
  return false;
}

const effectiveRole = computed(() => auth?.user?.role || auth?.role || '');
const isTech = computed(() => isTechnician(effectiveRole.value));
const canStartEstimate = computed(
  () => Boolean(checkPermission('leads.write') && checkPermission('estimates.write'))
);

const dialogTitle = computed(() => {
  if (submittedResult.value) return 'Request Recorded';
  return isTech.value ? 'New Estimate Request' : 'New Lead Intake';
});

const saving = ref(false);
const startingEstimate = ref(false);
const loadError = ref('');
const sourcesList = ref([
  'Google Search',
  'Referral',
  'Repeat Customer',
  'Yard Sign',
  'Truck / Vehicle',
  'Flyer / Mailer',
  'Other',
]);
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
  form.phone = props.initialPhone || '';
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
      if (form.phone) checkPhoneMatch(form.phone);
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
    if (digits.length >= 7) {
      phoneDebounceTimer = setTimeout(() => {
        checkPhoneMatch(digits);
      }, 350);
    }
  }
);

async function checkPhoneMatch(rawDigits) {
  try {
    const res = await api.get(`/api/planner/match-phone?phone=${encodeURIComponent(rawDigits)}`);
    if (res?.name || res?.customer_id) {
      phoneMatch.matched = true;
      phoneMatch.name = res?.name || '';
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
      summary: isTech.value ? 'Request submitted' : 'Lead created',
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
  background-color: var(--p-red-50, #fef2f2);
  color: var(--p-red-700, #b91c1c);
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
  background-color: var(--p-green-50, #f0fdf4);
  color: var(--p-green-800, #166534);
}

.banner-office {
  background-color: var(--p-primary-50, #eff6ff);
  color: var(--p-primary-800, #1e40af);
}

.result-icon {
  font-size: 2.5rem;
  margin-bottom: 0.75rem;
  color: var(--p-green-600, #16a34a);
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
  background-color: var(--p-amber-50, #fffbeb);
  color: var(--p-amber-800, #92400e);
  border: 1px solid var(--p-amber-200, #fde68a);
  border-radius: 6px;
  font-size: 0.875rem;
}

.match-banner {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  background-color: var(--p-blue-50, #eff6ff);
  color: var(--p-blue-800, #1e40af);
  border: 1px solid var(--p-blue-200, #bfdbfe);
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

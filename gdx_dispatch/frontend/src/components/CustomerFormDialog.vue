<template>
  <Dialog
    :visible="visible"
    :header="isEditMode ? 'Edit Customer' : 'Create Customer'"
    :style="{ width: '500px' }"
    modal
    :closable="!isDirty"
    :close-on-escape="!isDirty"
    data-testid="customer-form-dialog"
    @update:visible="$emit('update:visible', $event)"
  >
    <form :id="formId" class="dialog-form" @submit.prevent="submitForm">
      <!-- name/phone/email/address stay raw label+InputText: their
           data-testids are consumed as real <input> elements by e2e
           (daily-ux-improvements) + unit specs, and FormField puts
           fall-through attrs on its wrapper div, not the input. -->
      <div class="form-field">
        <label for="cfd-name">Full name *</label>
        <InputText id="cfd-name" v-model="form.name" data-testid="customer-name-input" class="w-full" />
      </div>

      <!-- Non-blocking duplicate hint. Fires on a debounced lookup as the
           user types name/phone/email. The user can still save — it may be a
           different person who shares a name. -->
      <div v-if="duplicateMatch" class="dup-warning" data-testid="customer-dup-warning">
        <i class="pi pi-exclamation-triangle" aria-hidden="true"></i>
        <span>
          Possible duplicate of
          <strong>{{ duplicateMatch.customer.name }}</strong>
          <template v-if="duplicateMatch.customer.phone"> · {{ formatPhone(duplicateMatch.customer.phone) }}</template>
          <template v-else-if="duplicateMatch.customer.email"> · {{ duplicateMatch.customer.email }}</template>
          (matched on {{ duplicateMatch.on }}). Save anyway if this is a different customer.
        </span>
      </div>

      <div class="form-row">
        <div class="form-field">
          <label for="cfd-phone">Phone</label>
          <PhoneInput
            id="cfd-phone"
            v-model="form.phone"
            data-testid="customer-phone-input"
            class="w-full"
            maxlength="30"
          />
        </div>
        <div class="form-field">
          <label for="cfd-email">Email</label>
          <InputText id="cfd-email" v-model="form.email" type="email" data-testid="customer-email-input" class="w-full" />
        </div>
      </div>

      <div class="form-field">
        <label for="cfd-address">Address</label>
        <Textarea id="cfd-address" v-model="form.address" rows="2" data-testid="customer-address-input" class="w-full" />
      </div>

      <FormField
        id="cfd-notes"
        v-model="form.notes"
        label="Notes"
        as="textarea"
        :rows="3"
        data-testid="customer-notes-input"
        placeholder="Billing instructions, referral context, or equipment notes"
      />

      <FormField
        id="cfd-referral"
        v-model="form.referral_source"
        label="Referral source"
        data-testid="customer-referral-input"
        placeholder="Referral partner or campaign"
        maxlength="50"
      />

      <FormField
        id="cfd-type"
        v-model="form.customer_type"
        label="Customer Type"
        as="select"
        :options="customerTypeOptionsFor(form.customer_type)"
        optionLabel="label"
        optionValue="value"
        data-testid="customer-type-dropdown"
      />

      <!-- Pre-2026-05-21 the dialog also rendered an Access notes textarea,
           a Tax exempt toggle, and a Send portal invite toggle. None of
           those survived the round-trip: access_notes lives on
           CustomerLocation (not Customer), tax_exempt lives in tax_exemption
           (separate module), and send_invite was never wired to anything.
           Stripped so the saved-toast can't lie about persisting them. -->


      <div v-if="error" ref="errorEl" class="inline-error" data-testid="customer-form-error">{{ error }}</div>
    </form>

    <!-- The actions live in the dialog footer, not the form: the content area
         scrolls, and on a phone a row at its end starts below the dialog's
         edge (GDXA-872). form= keeps Save a submit of the form above, so Enter
         and the button both go through submitForm. -->
    <template #footer>
      <Button type="button" label="Cancel" text data-testid="customer-cancel-btn" @click="cancel" />
      <Button
        type="submit"
        :form="formId"
        :label="isEditMode ? 'Save Changes' : 'Create Customer'"
        :loading="saving"
        data-testid="customer-submit-btn"
      />
    </template>
  </Dialog>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from "vue";
import { useToast } from "primevue/usetoast";
import { useApiWithToast } from "../composables/useApiWithToast";
import { useApi } from "../composables/useApi";
import { useDirtyDialog } from "../composables/useDirtyDialog";
import { findDuplicateMatch, lookupTerms } from "../utils/customerMatch";
import Button from "primevue/button";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";
import FormField from "./FormField.vue";
import PhoneInput from "./PhoneInput.vue";
import { formatPhone } from "../composables/useFormatters";
import { customerTypeOptionsFor, normalizeCustomerType } from "../constants/customerTypes";

const props = defineProps({
  visible: { type: Boolean, default: false },
  mode: { type: String, default: "create" },
  customer: { type: Object, default: null },
});
const emit = defineEmits(["update:visible", "saved"]);

const api = useApiWithToast();
// Silent client for the background duplicate lookup — suppresses error toasts
// so a transient blip mid-typing never nags the user.
const lookupApi = useApi();
const toast = useToast();
// Per-instance, so two mounted dialogs never point a Save at the wrong form.
const formId = `customer-form-${useId()}`;

const trimOrNull = (value) => {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
};

function emptyForm() {
  return {
    id: null,
    name: "",
    phone: "",
    email: "",
    address: "",
    notes: "",
    referral_source: "",
    customer_type: "Residential",
    version: null,
  };
}

function formFromCustomer(customer) {
  if (!customer) return emptyForm();
  return {
    id: customer.id,
    name: customer.name || "",
    phone: customer.phone || "",
    email: customer.email || "",
    address: customer.address || "",
    notes: customer.notes || "",
    // Backend serializer exposes Customer.source as referral_source on the
    // payload (2026-05-21). Fall back to source for older clients.
    referral_source: customer.referral_source || customer.source || "",
    customer_type: normalizeCustomerType(customer.customer_type),
    // The token this dialog loaded (GDXA-448): sent back on save, so a
    // colleague's edit in between is a 409 here, not silently reverted.
    version: customer.version ?? null,
  };
}

const form = ref(emptyForm());
const error = ref("");
const saving = ref(false);
const errorEl = ref(null);

// Save is pinned in the footer, so it can be tapped with the form scrolled to
// the top and the error far below it (GDXA-872): bring the error into view.
// Called, not watched: a second "Name is required." is the same value, and a
// watcher would not fire for it.
async function revealError() {
  await nextTick();
  errorEl.value?.scrollIntoView?.({ block: "nearest" });
}

// Unsaved-changes guard — Esc / the header X are disabled while dirty, and
// Cancel prompts before discarding typed-in work (2026-07-01 UX audit).
const { snapshot: snapshotDirty, isDirty, confirmDiscard } = useDirtyDialog(() => form.value, {
  message: "Discard unsaved customer changes?",
});

// The form as loaded, so an edit PATCHes only the fields the user changed
// (GDXA-1824). The endpoint honours explicit nulls, so sending an untouched
// key writes whatever the form defaulted it to over the stored value.
let pristine = emptyForm();
function snapshot() {
  pristine = { ...form.value };
  snapshotDirty();
}

function payloadFrom(f) {
  return {
    name: f.name.trim(),
    phone: f.phone || "",
    email: f.email || "",
    address: f.address || "",
    notes: trimOrNull(f.notes),
    referral_source: trimOrNull(f.referral_source),
    customer_type: f.customer_type,
  };
}

// Name is always sent; every other key only when its value differs from the
// loaded one — a cleared field differs too, so it still goes as "" or null.
function changedPayload(full) {
  const loaded = payloadFrom(pristine);
  return Object.fromEntries(
    Object.entries(full).filter(([key, value]) => key === "name" || value !== loaded[key]),
  );
}

// At-entry duplicate detection. { customer, on } or null.
const duplicateMatch = ref(null);
let _dupTimer = null;
// Monotonic token so an earlier slow lookup that resolves out of order can't
// overwrite a newer one's result (TOCTOU on duplicateMatch).
let _dupSeq = 0;

function scheduleDuplicateCheck() {
  clearTimeout(_dupTimer);
  _dupTimer = setTimeout(runDuplicateCheck, 400);
}

function unwrapList(res) {
  return Array.isArray(res) ? res : res?.items || res?.data || [];
}

async function runDuplicateCheck() {
  const candidate = {
    name: form.value.name,
    phone: form.value.phone,
    email: form.value.email,
  };
  // Query every present identifier (phone/email/name) and merge the pools — a
  // single "best" term would miss a dupe keyed on a different field than the
  // one we picked.
  const terms = lookupTerms(candidate);
  if (!terms.length) {
    duplicateMatch.value = null;
    return;
  }
  const seq = ++_dupSeq;
  try {
    const pools = await Promise.all(
      terms.map((t) =>
        lookupApi
          .get(`/api/customers?q=${encodeURIComponent(t)}&per_page=20`, { suppressErrorToast: true })
          .then(unwrapList)
          .catch(() => []),
      ),
    );
    if (seq !== _dupSeq) return; // superseded by a newer keystroke
    const byId = new Map();
    for (const pool of pools) {
      for (const c of pool) {
        if (c && c.id != null) byId.set(String(c.id), c);
      }
    }
    // excludeId so editing an existing customer never flags itself.
    duplicateMatch.value = findDuplicateMatch(candidate, [...byId.values()], {
      excludeId: form.value.id,
    });
  } catch {
    // Network/permission blip — fail open (no warning), never block entry.
    if (seq === _dupSeq) duplicateMatch.value = null;
  }
}

const isEditMode = computed(() => props.mode === "edit");

// Repopulate the form each time the dialog opens — closed-then-reopened
// edits should always pull the freshest customer prop, not last session's
// scratch.
watch(
  () => [props.visible, props.customer],
  ([isVisible]) => {
    if (isVisible) {
      form.value = formFromCustomer(props.customer);
      error.value = "";
      duplicateMatch.value = null;
      clearTimeout(_dupTimer);
      // Pristine snapshot must exist before the first keystroke (the
      // watcher is `immediate`, so this also covers mounting already-open).
      snapshot();
    }
  },
  { immediate: true },
);

// Re-check for duplicates (debounced) as the user edits identity fields.
watch(
  () => [form.value.name, form.value.phone, form.value.email],
  () => {
    if (props.visible) scheduleDuplicateCheck();
  },
);

onBeforeUnmount(() => clearTimeout(_dupTimer));

function cancel() {
  if (!confirmDiscard()) return;
  emit("update:visible", false);
}

async function submitForm() {
  error.value = "";
  if (!form.value.name.trim()) {
    error.value = "Name is required.";
    revealError();
    return;
  }
  const full = payloadFrom(form.value);
  saving.value = true;
  try {
    let saved;
    if (isEditMode.value) {
      const payload = changedPayload(full);
      if (form.value.version != null) payload.expected_version = form.value.version;
      saved = await api.patch(`/api/customers/${form.value.id}`, payload);
      toast.add({
        severity: "success",
        summary: "Customer Updated",
        detail: "Customer saved successfully.",
        life: 3000,
      });
    } else {
      saved = await api.post("/api/customers", full);
      toast.add({
        severity: "success",
        summary: "Customer Created",
        detail: "Customer saved successfully.",
        life: 3000,
      });
    }
    emit("saved", saved || { ...full, id: form.value.id });
    emit("update:visible", false);
  } catch (e) {
    error.value = e?.message || "Failed to save customer.";
    revealError();
    // A colleague saved first (GDXA-448): show their version in the form, so
    // the user re-applies the edit against it instead of 409ing forever on a
    // token the parent will hand back unchanged on reopen.
    if (e?.code === "version_conflict" && form.value.id) {
      try {
        form.value = formFromCustomer(await api.get(`/api/customers/${form.value.id}`));
        // The reloaded row is the new pristine state: the user's edits are
        // already gone, so Cancel must not ask to discard them.
        snapshot();
      } catch {
        // The message already says to reload; nothing more to do here.
      }
    }
  } finally {
    saving.value = false;
  }
}
</script>

<style scoped>
.dialog-form {
  display: grid;
  gap: 0.75rem;
}
.form-field {
  display: grid;
  gap: 0.25rem;
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
}
.tax-toggle .p-inputswitch {
  margin-top: 0.2rem;
}
.inline-error {
  color: #b42318;
  margin: 0.5rem 0;
  font-size: 0.9rem;
}
.dup-warning {
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
  padding: 0.5rem 0.65rem;
  border-radius: 6px;
  background: var(--p-amber-50, #fffbeb);
  border: 1px solid var(--p-amber-200, #fde68a);
  color: var(--p-amber-700, #b45309);
  font-size: 0.85rem;
  line-height: 1.3;
}
.dup-warning .pi {
  margin-top: 0.1rem;
}
/* Dark mode: the amber-50 background is a light island in the dark dialog.
   Use a translucent amber tint + brighter text so the warning reads on a dark
   surface (matches the banner-warning treatment in MobileTimeclockView). */
[data-theme="dark"] .dup-warning {
  background: rgba(245, 158, 11, 0.15);
  border-color: rgba(245, 158, 11, 0.5);
  color: #fcd34d;
}
.w-full {
  width: 100%;
}
</style>

<template>
  <Dialog
    :visible="visible"
    :header="isEditMode ? 'Edit Customer' : 'Create Customer'"
    :style="{ width: '500px' }"
    modal
    data-testid="customer-form-dialog"
    @update:visible="$emit('update:visible', $event)"
  >
    <form class="dialog-form" @submit.prevent="submitForm">
      <div class="form-field">
        <label for="cfd-name">Full name *</label>
        <InputText id="cfd-name" v-model="form.name" data-testid="customer-name-input" class="w-full" />
      </div>

      <div class="form-row">
        <div class="form-field">
          <label for="cfd-phone">Phone</label>
          <InputText
            id="cfd-phone"
            v-model="form.phone"
            placeholder="(555) 555-5555"
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

      <div class="form-field">
        <label for="cfd-notes">Notes</label>
        <Textarea
          id="cfd-notes"
          v-model="form.notes"
          rows="3"
          data-testid="customer-notes-input"
          class="w-full"
          placeholder="Billing instructions, referral context, or equipment notes"
        />
      </div>

      <div class="form-field">
        <label for="cfd-referral">Referral source</label>
        <InputText
          id="cfd-referral"
          v-model="form.referral_source"
          data-testid="customer-referral-input"
          class="w-full"
          placeholder="Referral partner or campaign"
          maxlength="50"
        />
      </div>

      <div class="form-field">
        <label for="cfd-type">Customer Type</label>
        <Select
          id="cfd-type"
          v-model="form.customer_type"
          :options="customerTypeOptions"
          optionLabel="label"
          optionValue="value"
          data-testid="customer-type-dropdown"
          class="w-full"
        />
      </div>

      <!-- Pre-2026-05-21 the dialog also rendered an Access notes textarea,
           a Tax exempt toggle, and a Send portal invite toggle. None of
           those survived the round-trip: access_notes lives on
           CustomerLocation (not Customer), tax_exempt lives in tax_exemption
           (separate module), and send_invite was never wired to anything.
           Stripped so the saved-toast can't lie about persisting them. -->


      <div v-if="error" class="inline-error" data-testid="customer-form-error">{{ error }}</div>

      <div class="form-actions">
        <Button type="button" label="Cancel" text @click="cancel" />
        <Button
          type="submit"
          :label="isEditMode ? 'Save Changes' : 'Create Customer'"
          :loading="saving"
          data-testid="customer-submit-btn"
        />
      </div>
    </form>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { useToast } from "primevue/usetoast";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";

const props = defineProps({
  visible: { type: Boolean, default: false },
  mode: { type: String, default: "create" },
  customer: { type: Object, default: null },
});
const emit = defineEmits(["update:visible", "saved"]);

const api = useApiWithToast();
const toast = useToast();

const customerTypeOptions = [
  { label: "Residential", value: "Residential" },
  { label: "Commercial", value: "Commercial" },
  { label: "Retail", value: "Retail" },
  { label: "Contractor", value: "Contractor" },
  { label: "Wholesale", value: "Wholesale" },
  { label: "Property Manager", value: "Property Manager" },
];

function normalizeCustomerType(type) {
  const text = (type || "").toString().trim().toLowerCase();
  if (text === "commercial") return "Commercial";
  if (text === "retail") return "Retail";
  if (text === "contractor") return "Contractor";
  if (text === "wholesale") return "Wholesale";
  if (text === "property_manager" || text === "property manager") return "Property Manager";
  if (text === "residential") return "Residential";
  return type ? String(type) : "Residential";
}

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
  };
}

const form = ref(emptyForm());
const error = ref("");
const saving = ref(false);

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
    }
  },
  { immediate: true },
);

function cancel() {
  emit("update:visible", false);
}

async function submitForm() {
  error.value = "";
  if (!form.value.name.trim()) {
    error.value = "Name is required.";
    return;
  }
  const payload = {
    name: form.value.name.trim(),
    phone: form.value.phone || "",
    email: form.value.email || "",
    address: form.value.address || "",
    notes: trimOrNull(form.value.notes),
    referral_source: trimOrNull(form.value.referral_source),
    customer_type: form.value.customer_type,
  };
  saving.value = true;
  try {
    let saved;
    if (isEditMode.value) {
      saved = await api.patch(`/api/customers/${form.value.id}`, payload);
      toast.add({
        severity: "success",
        summary: "Customer Updated",
        detail: "Customer saved successfully.",
        life: 3000,
      });
    } else {
      saved = await api.post("/api/customers", payload);
    }
    emit("saved", saved || { ...payload, id: form.value.id });
    emit("update:visible", false);
  } catch (e) {
    error.value = e?.message || "Failed to save customer.";
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
.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 0.75rem;
}
.inline-error {
  color: #b42318;
  margin: 0.5rem 0;
  font-size: 0.9rem;
}
.w-full {
  width: 100%;
}
</style>

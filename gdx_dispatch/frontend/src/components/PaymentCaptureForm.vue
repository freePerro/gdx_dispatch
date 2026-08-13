<script setup>
/**
 * PaymentCaptureForm — the shared cash/check capture form.
 *
 * It exists because the hand-rolled pay forms had drifted apart on the details
 * that decide whether money is recorded correctly: InvoiceDetailView and
 * BillingView both opened with `amount: 0`, so the operator re-keyed a number
 * the app already knew, and MobileBillingView stamped the UTC day, so an
 * evening payment booked tomorrow.
 *
 * Scope, so this docblock does not overstate itself: the new accept-time
 * surfaces and MobileBillingView use this component. InvoiceDetailView and
 * BillingView keep their own dialogs (they only got the prefill fix), and
 * MobileInvoiceDialog still hand-rolls its own — which was already prefilled
 * and already tenant-zone-dated, but has no confirmation step. Folding those
 * three in is the obvious follow-up.
 *
 * It does NOT post. Callers own the request, because the accept-estimate flow
 * has no invoice to post to until the accept round-trip returns — the form is
 * filled before the invoice exists. Two ways to drive it:
 *
 *   standalone: render an own submit button, emit `submit` with the payload.
 *   inline:     parent calls `collect()` (exposed) when ITS button is pressed.
 *
 * Both paths run the same validation and the same cash confirmation.
 */
import { computed, ref, watch } from 'vue';
import Button from 'primevue/button';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import Select from 'primevue/select';
import SelectButton from 'primevue/selectbutton';
import { useTenantTimezone } from '../composables/useTenantTimezone';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
import { formatMoney } from '../composables/useFormatters';

const props = defineProps({
  /** Prefills the amount and, when capAtBalance, caps it. */
  balanceDue: { type: Number, default: 0 },
  // Literal, not a module constant: defineProps is hoisted out of setup() and
  // cannot close over locals.
  methods: {
    type: Array,
    default: () => ['Cash', 'Check', 'Card', 'Zelle', 'Venmo', 'ACH', 'Other'],
  },
  /** 'desktop' → Select dropdown; 'mobile' → thumb-sized SelectButton. */
  variant: { type: String, default: 'desktop' },
  /**
   * Cap the amount input at `balanceDue`.
   *
   * Note what that is and is not: it caps at whatever the PARENT passed, which
   * for the accept dialog is the deposit amount being requested — the invoice
   * does not exist yet. It is an input aid, not a server-side guarantee, and a
   * caller posting against an invoice whose real balance differs must re-check
   * before sending (EstimateView.recordDepositPayment does).
   */
  capAtBalance: { type: Boolean, default: true },
  standalone: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
  submitLabel: { type: String, default: 'Record payment' },
});

const emit = defineEmits(['submit']);

const { zonedDateKey } = useTenantTimezone();
const { confirmAsync } = useDestructiveConfirm();

/**
 * Tenant-zone day, never `toISOString().slice(0,10)`. Central is behind UTC,
 * so the UTC slice after ~7 PM is tomorrow — money booked on the wrong day and,
 * at month-end, in the wrong month.
 */
const today = () => zonedDateKey(new Date());

const method = ref(props.methods[0] || 'Cash');
const amount = ref(props.balanceDue > 0 ? props.balanceDue : null);
const date = ref(today());
const reference = ref('');

// Re-prefill when the payable amount arrives or changes (the accept dialog
// edits the deposit amount live, and dialogs are reused across invoices).
watch(
  () => props.balanceDue,
  (next) => { amount.value = next > 0 ? next : null; },
);

const maxAmount = computed(() =>
  props.capAtBalance && props.balanceDue > 0 ? props.balanceDue : null,
);
const isCash = computed(() => String(method.value || '').toLowerCase() === 'cash');
const isCheck = computed(() => String(method.value || '').toLowerCase() === 'check');
const valid = computed(
  () => Number(amount.value) > 0 && !!method.value && !!date.value,
);

/**
 * Confirm whenever the payment carries NO reference — not merely when it is
 * cash.
 *
 * The rule is about dedupe keys, not method names: `reference` is what
 * migration 056's partial unique index (`reference IS NOT NULL`) keys on, so a
 * payment without one has no server-side duplicate protection beyond a short
 * time window. Cash is the usual case, but a Check submitted with the check #
 * left blank is in exactly the same position — and an earlier version of this
 * component confirmed on method alone, so that one slipped through silently.
 */
const needsConfirm = computed(() => !reference.value.trim());

/**
 * Validate, confirm if cash, and return the payload — or null if the operator
 * backed out or the form is incomplete.
 *
 * A payment with NO reference gets a confirmation; one with a reference does
 * not. The rule is about dedupe keys rather than method names: `reference`
 * (a check #, a confirmation code) is what migration 056's partial unique
 * index keys on, so a payment without one has no server-side duplicate
 * protection beyond a short time window. Cash is the usual case; a Check with
 * the number left blank is in the same position. The prompt doubles as an
 * amount check, which matters more — the field is prefilled, and prefilled
 * money fields get edited wrong.
 *
 * What this prompt does NOT cover, and must not be relied on for: it only runs
 * on a human tap. It is absent from the offline queue's replay path, where a
 * request that errored AFTER the server committed is re-sent unattended. That
 * case is caught server-side by the reference-less dedupe window in
 * routers/invoices.record_payment — not here.
 */
async function collect() {
  if (!valid.value) return null;
  if (needsConfirm.value) {
    const ok = await confirmAsync({
      header: isCash.value ? 'Confirm cash payment' : 'Confirm payment',
      message: `Record ${formatMoney(Number(amount.value))} by ${String(method.value).toLowerCase()}? Check the amount before saving.`,
      acceptLabel: "Yes, that's right",
      rejectLabel: 'Go back',
    });
    if (!ok) return null;
  }
  return {
    amount: Number(amount.value),
    method: method.value,
    date: date.value,
    reference: reference.value.trim() || null,
  };
}

async function onSubmit() {
  const payload = await collect();
  if (payload) emit('submit', payload);
}

/** Reset to a clean prefilled state — callers reuse dialogs across invoices. */
function reset() {
  method.value = props.methods[0] || 'Cash';
  amount.value = props.balanceDue > 0 ? props.balanceDue : null;
  date.value = today();
  reference.value = '';
}

defineExpose({ collect, reset, valid });
</script>

<template>
  <div class="payment-capture" :class="`pc-${variant}`" data-testid="payment-capture-form">
    <SelectButton
      v-if="variant === 'mobile'"
      v-model="method"
      :options="methods"
      :allow-empty="false"
      aria-label="Payment method"
      data-testid="pc-method"
    />
    <div v-else class="pc-field">
      <label for="pc-method-select">Method</label>
      <Select
        id="pc-method-select"
        v-model="method"
        :options="methods"
        data-testid="pc-method"
      />
    </div>

    <div class="pc-field">
      <label for="pc-amount">Amount</label>
      <InputNumber
        v-model="amount"
        input-id="pc-amount"
        mode="currency"
        currency="USD"
        locale="en-US"
        :min="0"
        :max="maxAmount"
        data-testid="pc-amount"
      />
      <small v-if="maxAmount" class="pc-hint">
        Capped at the {{ formatMoney(maxAmount) }} owed — record any excess on the final invoice.
      </small>
    </div>

    <div class="pc-field">
      <label for="pc-date">Date received</label>
      <InputText
        id="pc-date"
        v-model="date"
        type="date"
        :max="today()"
        data-testid="pc-date"
      />
      <!-- Backdating is the point: the money often changed hands in the field
           days before anyone reached a desktop. Forward-dating is refused by
           the API — a post-dated check is not received cash. -->
      <small class="pc-hint">Backdate to the day it was actually received.</small>
    </div>

    <div class="pc-field">
      <label for="pc-reference">{{ isCheck ? 'Check #' : 'Reference' }}</label>
      <InputText
        id="pc-reference"
        v-model="reference"
        :placeholder="isCheck ? 'Check #' : 'Confirmation, memo…'"
        data-testid="pc-reference"
      />
    </div>

    <div v-if="standalone" class="pc-actions">
      <Button
        :label="submitLabel"
        icon="pi pi-check"
        severity="success"
        size="small"
        :disabled="!valid || busy"
        :loading="busy"
        data-testid="pc-submit"
        @click="onSubmit"
      />
    </div>
  </div>
</template>

<style scoped>
.payment-capture {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.pc-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.pc-field label {
  font-size: 0.85rem;
  opacity: 0.8;
}
.pc-hint {
  font-size: 0.75rem;
  opacity: 0.65;
}
.pc-actions {
  display: flex;
  justify-content: flex-end;
}
/* Mobile: full-width controls and 44px touch targets — this form gets used
   one-handed at a customer's door. */
.pc-mobile :deep(.p-selectbutton) {
  display: flex;
  width: 100%;
}
.pc-mobile :deep(.p-selectbutton .p-togglebutton) {
  flex: 1;
  min-height: 44px;
}
.pc-mobile :deep(.p-inputtext),
.pc-mobile :deep(.p-inputnumber-input) {
  width: 100%;
  min-height: 44px;
  /* 16px stops iOS Safari zooming the viewport on focus. */
  font-size: 16px;
}
</style>

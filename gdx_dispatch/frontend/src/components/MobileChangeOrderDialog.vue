<script setup>
// 2026-07-01 UX audit — field change-order initiation.
//
// Before this, a tech who discovered extra scope mid-job ("the upper hinge
// is shot too, that's $80 more") had to call dispatch to get a change order
// created. This mobile sheet posts the existing POST /api/change-orders
// (status: pending_approval) so the office reviews/approves from the
// desktop Change Orders view — same pipeline, no new backend.
//
// Offline-queueable: the request persists to IndexedDB and replays with an
// Idempotency-Key when the tech reconnects (useOfflineSync).

import { computed, ref, watch } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Textarea from 'primevue/textarea'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { useDirtyDialog } from '../composables/useDirtyDialog'

const props = defineProps({
  visible: { type: Boolean, default: false },
  jobId: { type: String, default: null },
  jobTitle: { type: String, default: '' },
  customerId: { type: String, default: null },
  customerName: { type: String, default: '' },
})
const emit = defineEmits(['update:visible', 'created'])

const api = useApi()
const toast = useToast()

const open = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

const title = ref('')
const amount = ref(null)
const description = ref('')
const saving = ref(false)

const { snapshot, isDirty, confirmDiscard } = useDirtyDialog(
  () => ({ title: title.value, amount: amount.value, description: description.value }),
  { message: 'Discard this change-order request?' }
)

const canSubmit = computed(() => title.value.trim().length > 0 && !saving.value)

// immediate: the dialog can be mounted already-visible; the pristine
// snapshot must exist before the first user keystroke either way.
watch(open, (v) => {
  if (v) {
    title.value = ''
    amount.value = null
    description.value = ''
    snapshot()
  }
}, { immediate: true })

function requestCancel() {
  if (!confirmDiscard()) return
  open.value = false
}

async function submit() {
  if (!canSubmit.value) return
  saving.value = true
  try {
    const payload = {
      job_id: props.jobId || null,
      customer_id: props.customerId || null,
      customer_name: props.customerName || null,
      title: title.value.trim(),
      description: description.value.trim() || null,
      reason: 'Field request (tech, mobile)',
      status: 'pending_approval',
      amount: Number(amount.value) || 0,
    }
    const r = await api.postQueued('/api/change-orders', payload, {
      actionType: 'change_order.create', resourceId: String(props.jobId || ''),
    })
    if (r?.queued) {
      toast.add({
        severity: 'warn',
        summary: 'Saved offline',
        detail: 'No signal — the change order will reach the office when you reconnect.',
        life: 5000,
      })
    } else {
      toast.add({
        severity: 'success',
        summary: 'Change order sent',
        detail: `${r?.co_number ? r.co_number + ' — ' : ''}the office will review and approve it.`,
        life: 4000,
      })
    }
    emit('created', r)
    open.value = false
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Could not send change order',
      detail: err?.message || 'Try again.',
      life: 5000,
    })
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Dialog
    v-model:visible="open"
    :header="`Change order — ${jobTitle || 'Job'}`"
    modal
    :closable="!isDirty"
    :close-on-escape="!isDirty"
    :style="{ width: '95vw', maxWidth: '480px' }"
    :breakpoints="{ '768px': '100vw' }"
    data-testid="mobile-change-order-dialog"
  >
    <p v-if="customerName" class="muted hint">{{ customerName }}</p>

    <form class="form-stack" @submit.prevent="submit">
      <div class="form-field">
        <label for="mco-title">What changed?<span class="req" aria-hidden="true">*</span></label>
        <InputText
          id="mco-title"
          v-model="title"
          placeholder="e.g. Replace upper hinge — extra scope"
          data-testid="mco-title"
          required
        />
      </div>

      <div class="form-field">
        <label for="mco-amount">Additional amount ($)</label>
        <InputNumber
          inputId="mco-amount"
          v-model="amount"
          mode="currency"
          currency="USD"
          locale="en-US"
          :min="0"
          placeholder="0.00"
          data-testid="mco-amount"
        />
        <small class="muted">
          Leave blank if the office should price it. Applicable tax is added
          on top — the customer signs the tax-inclusive total.
        </small>
      </div>

      <div class="form-field">
        <label for="mco-desc">Details for the office</label>
        <Textarea
          id="mco-desc"
          v-model="description"
          rows="3"
          autoResize
          placeholder="What you found, what's needed, customer's reaction"
          data-testid="mco-desc"
        />
      </div>
    </form>

    <template #footer>
      <Button label="Cancel" text severity="secondary" data-testid="mco-cancel" @click="requestCancel" />
      <Button
        label="Send to office"
        icon="pi pi-send"
        :disabled="!canSubmit"
        :loading="saving"
        data-testid="mco-submit"
        @click="submit"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.form-stack {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.form-field label {
  font-size: 0.85rem;
  font-weight: 600;
}
.req {
  color: var(--color-danger-500);
  margin-left: 0.15rem;
}
.muted {
  color: var(--text-muted);
  font-size: 0.8rem;
}
.hint {
  margin: 0 0 0.5rem;
}
/* Comfortable touch targets (Apple HIG / Material 48dp). */
.form-field :deep(input),
.form-field :deep(textarea) {
  min-height: 44px;
  font-size: 1rem;
}
</style>

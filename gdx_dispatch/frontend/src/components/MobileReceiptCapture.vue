<template>
  <!-- pi-receipt, not pi-camera (Doug 2026-08-16): a camera glyph reads as
       "take pictures", not "file a receipt" — the camera lives INSIDE the
       dialog where it really does mean "take the photo". -->
  <Button
    v-tooltip="'Add a receipt'"
    icon="pi pi-receipt"
    :label="buttonLabel"
    severity="secondary"
    size="small"
    aria-label="Add a receipt"
    data-testid="receipt-capture-open"
    @click="open"
  />

  <Dialog
    v-model:visible="visible"
    header="Receipt"
    modal
    class="receipt-capture-dialog"
    :style="{ width: 'min(26rem, 94vw)' }"
  >
    <div class="rc-form">
      <!-- capture="environment" opens the rear camera directly on phones;
           desktop falls back to a file picker. -->
      <input
        ref="fileInput"
        type="file"
        accept="image/*,application/pdf"
        capture="environment"
        class="rc-file-hidden"
        data-testid="receipt-file-input"
        @change="onFilePicked"
      />
      <button type="button" class="rc-photo-slot" data-testid="receipt-photo-slot" @click="fileInput?.click()">
        <img v-if="previewUrl" :src="previewUrl" alt="Receipt preview" class="rc-preview" />
        <span v-else class="rc-photo-hint">
          <i class="pi pi-camera" />
          Tap to take the photo
        </span>
      </button>

      <label class="rc-label" for="rc-amount">Amount</label>
      <InputNumber
        v-model="form.amount"
        inputId="rc-amount"
        mode="currency"
        currency="USD"
        locale="en-US"
        :min="0.01"
        inputmode="decimal"
        data-testid="receipt-amount"
      />

      <label class="rc-label" for="rc-vendor">Where</label>
      <InputText
        v-model="form.vendor"
        inputId="rc-vendor"
        placeholder="Kwik Trip, Menards…"
        maxlength="200"
        data-testid="receipt-vendor"
      />

      <label class="rc-label" for="rc-category">Category</label>
      <Select
        v-model="form.category"
        :options="categories"
        inputId="rc-category"
        data-testid="receipt-category"
      />

      <label class="rc-label" for="rc-note">Note (optional)</label>
      <InputText v-model="form.note" inputId="rc-note" placeholder="What it was for" />
    </div>

    <template #footer>
      <Button label="Cancel" severity="secondary" text :disabled="saving" @click="visible = false" />
      <Button
        label="Save receipt"
        icon="pi pi-check"
        :loading="saving"
        :disabled="!canSave"
        data-testid="receipt-save"
        @click="save"
      />
    </template>
  </Dialog>
</template>

<script setup>
// Field receipt capture (Doug, 2026-08-14): fuel and counter buys are
// receipt purchases, not bills — snap the receipt, key the total, done.
// Creates a DRAFT Expense (the office's existing review pipeline on
// /expenses) and attaches the photo through the hash-chained
// ExpenseReceipt store, so the paper trail is IRS-grade from the truck.
// The bank-side debit then auto-suggests against this expense on the
// Reconcile tab (exact amount ±3 business days), and under cash basis
// the purchase date IS the right expense date — no re-dating ever.
// No permission gate on purpose: every signed-in role can file a receipt.
import { computed, onBeforeUnmount, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

defineProps({
  buttonLabel: { type: String, default: 'Receipt' },
})

const api = useApi()
const toast = useToast()

const visible = ref(false)
const saving = ref(false)
const fileInput = ref(null)
const file = ref(null)
const previewUrl = ref(null)
const categories = ref(['Fuel', 'Parts/Supplies', 'Tools/Equipment', 'Vehicle Maintenance', 'Other'])
const form = ref({ amount: null, vendor: '', category: 'Fuel', note: '' })

const canSave = computed(() => !!file.value && !!form.value.amount && !!form.value.vendor.trim())

async function open() {
  visible.value = true
  try {
    const list = await api.get('/api/expense-categories')
    if (Array.isArray(list) && list.length) categories.value = list
  } catch {
    /* keep the built-in fallback list */
  }
}

function onFilePicked(event) {
  const picked = event.target?.files?.[0]
  if (!picked) return
  file.value = picked
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = picked.type?.startsWith('image/') ? URL.createObjectURL(picked) : null
}

function localToday() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function reset() {
  form.value = { amount: null, vendor: '', category: 'Fuel', note: '' }
  file.value = null
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = null
  if (fileInput.value) fileInput.value.value = ''
}

async function save() {
  saving.value = true
  try {
    const expense = await api.post('/api/expenses', {
      vendor: form.value.vendor.trim(),
      amount: form.value.amount,
      date: localToday(),
      category: form.value.category,
      description: form.value.note?.trim() || null,
    })
    try {
      const body = new FormData()
      body.append('file', file.value)
      await api.post(`/api/expenses/${expense.id}/receipts`, body)
      toast.add({ severity: 'success', summary: 'Receipt saved', detail: 'Photo attached — the office reviews it on Expenses.', life: 4000 })
    } catch {
      // The expense exists; losing the photo silently would defeat the
      // whole point — say exactly what happened and where to retry.
      toast.add({
        severity: 'warn',
        summary: 'Expense saved, photo failed to upload',
        detail: 'Re-attach it from the Expenses page when you have signal.',
        life: 9000,
      })
    }
    visible.value = false
    reset()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not save receipt', detail: err?.message || 'Try again.', life: 6000 })
  } finally {
    saving.value = false
  }
}

onBeforeUnmount(() => {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
})

defineExpose({ open, save, form, file, canSave, onFilePicked, visible })
</script>

<style scoped>
.rc-form {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.rc-label {
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--p-text-muted-color);
}
.rc-file-hidden {
  display: none;
}
.rc-photo-slot {
  border: 2px dashed var(--p-content-border-color);
  border-radius: 10px;
  background: var(--p-content-hover-background);
  min-height: 7.5rem;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  padding: 0;
  overflow: hidden;
}
.rc-photo-hint {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.35rem;
  color: var(--p-text-muted-color);
  font-size: 0.9rem;
}
.rc-photo-hint .pi {
  font-size: 1.6rem;
}
.rc-preview {
  max-width: 100%;
  max-height: 14rem;
  object-fit: contain;
}
/* Mobile pass rule: inputs >= 16px so iOS doesn't zoom the page. */
.receipt-capture-dialog :deep(input),
.receipt-capture-dialog :deep(.p-select-label) {
  font-size: 16px;
}
</style>

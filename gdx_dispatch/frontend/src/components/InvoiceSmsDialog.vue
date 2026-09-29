<!--
  Text an invoice: one SMS carrying the customer's view-and-pay link.

  Shared by the office invoice page (base '/api/invoices') and the tech's
  mobile billing screen (base '/api/mobile/invoices'); the server owns every
  rule (opt-out, verification, pay link, duplicate window) and this dialog only
  shows what it says. Opening it previews — nothing leaves until Send.
-->
<template>
  <Dialog
    :visible="visible"
    header="Text invoice"
    modal
    :style="{ width: '32rem', maxWidth: '95vw' }"
    data-testid="sms-dialog"
    @update:visible="emit('update:visible', $event)"
  >
    <div v-if="loading" class="sms-loading">Loading…</div>
    <div v-else class="sms-body">
      <Message v-if="blocked" severity="warn" :closable="false" data-testid="sms-blocked">
        {{ blocked.message }}
      </Message>
      <Message v-if="unconfirmed" severity="warn" :closable="false" data-testid="sms-unconfirmed">
        {{ unconfirmed }}
      </Message>
      <div class="form-field">
        <label for="sms-to">To</label>
        <InputText
          id="sms-to"
          v-model="to"
          type="tel"
          inputmode="tel"
          autocomplete="off"
          :placeholder="customerName ? `${customerName}'s mobile` : 'Mobile number'"
          data-testid="sms-to"
          @change="refresh"
        />
      </div>
      <div class="form-field">
        <label for="sms-body">Message</label>
        <Textarea id="sms-body" v-model="body" rows="4" auto-resize data-testid="sms-body" />
        <small class="sms-hint">
          {{ body.length }} characters · the pay link is always included, even if you edit it out.
        </small>
      </div>
    </div>
    <template #footer>
      <Button label="Cancel" text data-testid="sms-cancel" @click="emit('update:visible', false)" />
      <Button
        :label="unconfirmed ? 'Send anyway' : 'Send text'"
        icon="pi pi-comment"
        :loading="sending"
        :disabled="loading || !!blocked || !body.trim()"
        data-testid="sms-send"
        @click="send"
      />
    </template>
  </Dialog>
</template>

<script setup>
import { ref, watch } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Message from 'primevue/message'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

const props = defineProps({
  visible: { type: Boolean, default: false },
  invoiceId: { type: String, required: true },
  // '/api/invoices' (office) or '/api/mobile/invoices' (tech).
  base: { type: String, default: '/api/invoices' },
})
const emit = defineEmits(['update:visible', 'sent'])

const api = useApi()
const toast = useToast()

const loading = ref(false)
const sending = ref(false)
const to = ref('')
const body = ref('')
const customerName = ref('')
const blocked = ref(null)
// Set when the server refuses because an earlier attempt never confirmed; the
// next Send is then an explicit "send anyway" (resend_unconfirmed).
const unconfirmed = ref(null)

async function preview(toOverride) {
  const res = await api.post(
    `${props.base}/${props.invoiceId}/sms-preview`,
    toOverride ? { to: toOverride } : {},
    { suppressErrorToast: true },
  )
  return res?.data || res
}

async function load() {
  loading.value = true
  blocked.value = null
  unconfirmed.value = null
  try {
    const p = await preview(null)
    to.value = p.to || ''
    body.value = p.body || ''
    customerName.value = p.customer_name || ''
    blocked.value = p.blocked
  } catch (err) {
    blocked.value = { message: err?.message || 'Could not load the text preview.' }
  } finally {
    loading.value = false
  }
}

// Re-check only the recipient-dependent refusal (e.g. no valid phone) when
// the number is edited; the operator's message edits are kept.
async function refresh() {
  // A different number is a different recipient: an earlier "send anyway"
  // decision was about the old one and must not carry over.
  unconfirmed.value = null
  try {
    const p = await preview(to.value.trim() || null)
    blocked.value = p.blocked
  } catch (err) {
    blocked.value = { message: err?.message || 'Could not check that number.' }
  }
}

async function send() {
  sending.value = true
  try {
    const res = await api.post(
      `${props.base}/${props.invoiceId}/send-sms`,
      { to: to.value.trim() || null, body: body.value, resend_unconfirmed: !!unconfirmed.value },
      { suppressErrorToast: true },
    )
    const payload = res?.data || res
    toast.add({ severity: 'success', summary: 'Texted', detail: `Invoice texted to ${payload.to}.`, life: 5000 })
    emit('sent', payload)
    emit('update:visible', false)
  } catch (err) {
    if (err?.code === 'prior_attempt_unconfirmed' && !unconfirmed.value) {
      unconfirmed.value = err.message
      return
    }
    if (err?.code === 'sms_outcome_unknown') {
      // Not a failure: it may have arrived, and the server moved the invoice
      // to sent — say so and refresh the caller's view.
      toast.add({ severity: 'warn', summary: 'Text not confirmed', detail: err.message, life: 8000 })
      emit('sent', { to: to.value, confirmed: false })
      emit('update:visible', false)
      return
    }
    toast.add({ severity: 'error', summary: 'Text not sent', detail: err?.message || '', life: 6000 })
  } finally {
    sending.value = false
  }
}

watch(() => props.visible, (open) => { if (open) load() }, { immediate: true })
</script>

<style scoped>
.sms-body { display: flex; flex-direction: column; gap: 1rem; }
.form-field { display: flex; flex-direction: column; gap: 0.35rem; }
.form-field :deep(input), .form-field :deep(textarea) { width: 100%; }
.sms-hint { color: var(--p-text-muted-color); }
.sms-loading { padding: 1rem 0; color: var(--p-text-muted-color); }
</style>

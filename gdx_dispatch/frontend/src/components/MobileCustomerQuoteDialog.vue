<script setup>
// Sprint tech_mobile Phase 2.1 (S2-A3 + S2-A4 + S2-A5) — Customer-facing
// quote presentation. Tech hands the phone to the customer.
//
// Customer:
//   - Picks a tier (Good / Better / Best)
//   - Signs (canvas pad on tech's phone — phone_handoff surface)
//   - Taps Accept → POST /api/mobile/quotes/{id}/accept
//   OR
//   - Taps Decline → reason picker → POST /api/mobile/quotes/{id}/decline
//
// On accept the dialog auto-closes and the parent (MobileTodayView) reloads
// the job's quote state so the "Build invoice" CTA can light up.
import { computed, nextTick, ref, watch } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

const props = defineProps({
  visible: { type: Boolean, default: false },
  quote: { type: Object, default: null },
})
const emit = defineEmits(['update:visible', 'accepted', 'declined'])

const api = useApi()
const toast = useToast()

const open = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

// Tier picker
const chosenTierId = ref(null)
const submitting = ref(false)

// Signature pad
const sigCanvas = ref(null)
let sigCtx = null
const drawing = ref(false)
const hasInk = ref(false)
const signedBy = ref('')

// Decline state
const declining = ref(false)
const declineReasons = ref([])
const declineReason = ref(null)
const declineNotes = ref('')

watch(() => props.visible, (v) => {
  if (v && props.quote?.tiers?.length) {
    // Default to "better" (middle tier) — sales-data common default.
    const better = props.quote.tiers.find(t => t.tier_name === 'better')
    chosenTierId.value = (better || props.quote.tiers[0]).id
    hasInk.value = false
    drawing.value = false
    signedBy.value = ''
    declining.value = false
    declineReason.value = null
    declineNotes.value = ''
    nextTick(initCanvas)
    if (declineReasons.value.length === 0) loadReasons()
  }
})

async function loadReasons() {
  try {
    const data = await api.get('/api/mobile/quotes/decline-reasons')
    declineReasons.value = (data.reasons || []).map(r => ({ label: r, value: r }))
  } catch (e) {
    // Fall back to a sane default — don't block decline path.
    declineReasons.value = ['Priced too high', 'Not today', 'Other']
      .map(r => ({ label: r, value: r }))
  }
}

function initCanvas() {
  const c = sigCanvas.value
  if (!c) return
  // Sharp on retina
  const dpr = window.devicePixelRatio || 1
  const rect = c.getBoundingClientRect()
  c.width = rect.width * dpr
  c.height = rect.height * dpr
  sigCtx = c.getContext('2d')
  sigCtx.scale(dpr, dpr)
  sigCtx.lineWidth = 2
  sigCtx.lineCap = 'round'
  sigCtx.strokeStyle = '#111827'
}
function _pt(e) {
  const c = sigCanvas.value
  const r = c.getBoundingClientRect()
  const ev = e.touches ? e.touches[0] : e
  return { x: ev.clientX - r.left, y: ev.clientY - r.top }
}
function sigStart(e) {
  if (!sigCtx) initCanvas()
  drawing.value = true
  const p = _pt(e)
  sigCtx.beginPath()
  sigCtx.moveTo(p.x, p.y)
  e.preventDefault?.()
}
function sigMove(e) {
  if (!drawing.value || !sigCtx) return
  const p = _pt(e)
  sigCtx.lineTo(p.x, p.y)
  sigCtx.stroke()
  hasInk.value = true
  e.preventDefault?.()
}
function sigEnd() { drawing.value = false }
function sigClear() {
  if (!sigCtx) return
  const c = sigCanvas.value
  sigCtx.clearRect(0, 0, c.width, c.height)
  hasInk.value = false
}

function fmtMoney(n) { return `$${(Number(n) || 0).toFixed(2)}` }
function tierBadge(name) {
  if (name === 'good') return 'secondary'
  if (name === 'better') return 'info'
  if (name === 'best') return 'success'
  return 'secondary'
}

const chosenTier = computed(() =>
  props.quote?.tiers?.find(t => t.id === chosenTierId.value) || null
)

async function accept() {
  if (!chosenTierId.value) {
    toast.add({ severity: 'warn', summary: 'Pick an option', life: 2500 })
    return
  }
  if (!hasInk.value) {
    toast.add({ severity: 'warn', summary: 'Signature required', life: 2500 })
    return
  }
  submitting.value = true
  try {
    const sig = sigCanvas.value?.toDataURL('image/png') || ''
    const updated = await api.post(`/api/mobile/quotes/${props.quote.id}/accept`, {
      chosen_tier_id: chosenTierId.value,
      signature_data: sig,
      signed_by: signedBy.value || null,
    })
    toast.add({ severity: 'success', summary: 'Quote accepted', life: 2500 })
    emit('accepted', updated)
    open.value = false
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not accept', detail: e.message, life: 5000 })
  } finally {
    submitting.value = false
  }
}

async function submitDecline() {
  if (!declineReason.value) {
    toast.add({ severity: 'warn', summary: 'Pick a reason', life: 2500 })
    return
  }
  submitting.value = true
  try {
    const updated = await api.post(`/api/mobile/quotes/${props.quote.id}/decline`, {
      reason: declineReason.value,
      notes: declineNotes.value || null,
    })
    toast.add({ severity: 'info', summary: 'Quote declined', life: 2500 })
    emit('declined', updated)
    open.value = false
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not decline', detail: e.message, life: 5000 })
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog
    v-model:visible="open"
    :header="declining ? 'Decline quote' : 'Your options'"
    modal
    :style="{ width: '96vw', maxWidth: '560px' }"
  >
    <div v-if="!declining" class="cust-quote">
      <p class="muted cust-intro">{{ quote?.label || 'Service quote' }}</p>

      <div class="tier-grid">
        <label
          v-for="tier in quote?.tiers || []"
          :key="tier.id"
          class="tier-card"
          :class="{ 'tier-chosen': chosenTierId === tier.id, [`tier-${tier.tier_name}`]: true }"
        >
          <input
            v-model="chosenTierId"
            type="radio"
            :value="tier.id"
            class="tier-radio"
          >
          <div class="tier-head">
            <Tag :value="tier.tier_name?.toUpperCase()" :severity="tierBadge(tier.tier_name)" />
            <strong class="tier-price">{{ fmtMoney(tier.total_price) }}</strong>
          </div>
          <p class="tier-desc">{{ tier.description }}</p>
          <div v-if="tier.warranty_months" class="tier-warranty muted">
            <i class="pi pi-shield" />
            {{ tier.warranty_months >= 999 ? 'Lifetime warranty' : `${tier.warranty_months}-month warranty` }}
          </div>
        </label>
      </div>

      <div class="sig-block">
        <div class="form-field">
          <label>Your name</label>
          <InputText v-model="signedBy" placeholder="As it appears on your contract" />
        </div>
        <div class="form-field">
          <label>Signature</label>
          <canvas
            ref="sigCanvas"
            class="sig-canvas"
            @pointerdown="sigStart"
            @pointermove="sigMove"
            @pointerup="sigEnd"
            @pointerleave="sigEnd"
            @touchstart="sigStart"
            @touchmove="sigMove"
            @touchend="sigEnd"
          />
          <Button label="Clear" icon="pi pi-eraser" text size="small" class="sig-clear" @click="sigClear" />
        </div>
      </div>

      <div class="cust-summary" v-if="chosenTier">
        <span>You picked:</span>
        <strong>{{ chosenTier.tier_name?.toUpperCase() }}</strong>
        <span>·</span>
        <strong class="cust-total">{{ fmtMoney(chosenTier.total_price) }}</strong>
      </div>
    </div>

    <!-- DECLINE form -->
    <div v-else class="decline-form">
      <p class="muted">No problem. Pick a reason — it helps us serve you better.</p>
      <div class="form-field">
        <label>Reason</label>
        <Select
          v-model="declineReason"
          :options="declineReasons"
          optionLabel="label"
          optionValue="value"
          placeholder="Pick one"
        />
      </div>
      <div class="form-field">
        <label>Anything else (optional)</label>
        <Textarea v-model="declineNotes" rows="2" autoResize placeholder="What changed your mind?" />
      </div>
    </div>

    <template #footer>
      <template v-if="!declining">
        <Button label="Decline" text severity="secondary" @click="declining = true" />
        <Button
          label="Accept & sign"
          icon="pi pi-check"
          severity="success"
          :loading="submitting"
          :disabled="!hasInk || !chosenTierId"
          @click="accept"
        />
      </template>
      <template v-else>
        <Button label="Back" text @click="declining = false" />
        <Button label="Send decline" icon="pi pi-times" severity="secondary" :loading="submitting" @click="submitDecline" />
      </template>
    </template>
  </Dialog>
</template>

<style scoped>
.cust-intro { margin: 0 0 0.6rem 0; font-size: 0.85rem; }
.tier-grid { display: flex; flex-direction: column; gap: 0.65rem; margin-bottom: 0.85rem; }
.tier-card {
  display: block;
  border: 2px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.7rem;
  padding: 0.85rem 1rem;
  cursor: pointer;
  transition: all 0.15s ease;
  background: var(--p-content-background, white);
}
.tier-card:active { transform: scale(0.99); }
.tier-card.tier-chosen { border-color: #2563eb; box-shadow: 0 0 0 3px #2563eb40; }
.tier-card.tier-best.tier-chosen { border-color: #15803d; box-shadow: 0 0 0 3px #15803d40; }
.tier-radio { display: none; }
.tier-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.35rem;
}
.tier-price { font-size: 1.25rem; }
.tier-desc { margin: 0; font-size: 0.85rem; color: var(--p-text-muted-color); }
.tier-warranty {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  margin-top: 0.5rem;
  font-size: 0.78rem;
}

.sig-block {
  margin-top: 0.6rem;
  border-top: 1px dashed var(--p-content-border-color);
  padding-top: 0.6rem;
}
.form-field { display: flex; flex-direction: column; gap: 0.3rem; margin-bottom: 0.55rem; }
.form-field label { font-size: 0.85rem; color: var(--p-text-muted-color); }
.sig-canvas {
  width: 100%;
  height: 140px;
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.5rem;
  background: white;
  touch-action: none;
}
.sig-clear { align-self: flex-start; padding: 0; margin-top: 0.2rem; }

.cust-summary {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.6rem 0.85rem;
  background: var(--p-highlight-background, #f3f4f6);
  border-radius: 0.5rem;
  margin-top: 0.5rem;
  font-size: 0.9rem;
}
.cust-total { font-size: 1.05rem; }

.decline-form { padding: 0.5rem 0; }
.muted { color: var(--p-text-muted-color, #6b7280); font-size: 0.8rem; }
</style>

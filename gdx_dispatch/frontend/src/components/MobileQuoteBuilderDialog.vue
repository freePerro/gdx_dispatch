<script setup>
// Sprint tech_mobile Phase 2.1 (S2-A1 + S2-A2) — On-Truck Quote Builder.
//
// Tech taps a service ("Spring replacement"), sees three pre-priced tiers
// (Good / Better / Best), optionally tweaks line items, then either
// "Hands phone to customer" → opens MobileCustomerQuoteDialog, or saves
// for later. No discount path on mobile (S2-A6) — line prices come from
// the tenant's preset catalog and are not editable on-truck.
import { computed, onMounted, ref, watch } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

const props = defineProps({
  visible: { type: Boolean, default: false },
  job: { type: Object, default: null },
})
const emit = defineEmits(['update:visible', 'present', 'saved'])

const api = useApi()
const toast = useToast()

const services = ref([])
const loading = ref(false)
const building = ref(false)
const selectedService = ref(null)  // { service, label, description, tiers: [...] }
const notes = ref('')

// S-autosave Slice 6 — localStorage draft for mobile builder.
// Mobile flow is atomic (pick service → Send), so server-side incremental
// drafts don't fit. Instead, persist the in-progress selection so a phone
// call / app backgrounding doesn't lose the tap. Per-job key so two jobs
// in parallel don't clobber each other.
const _draftKey = computed(() => props.job?.id ? `mobile_quote_draft_${props.job.id}` : null)
const draftRestored = ref(false)
let _draftWriteTimer = null

function _writeDraftSoon() {
  if (!_draftKey.value) return
  if (_draftWriteTimer) clearTimeout(_draftWriteTimer)
  _draftWriteTimer = setTimeout(() => {
    if (!_draftKey.value) return
    if (!selectedService.value && !notes.value) {
      // Nothing meaningful to persist — clear any prior draft.
      try { localStorage.removeItem(_draftKey.value) } catch { /* quota / private mode */ }
      return
    }
    try {
      localStorage.setItem(_draftKey.value, JSON.stringify({
        service_key: selectedService.value?.service || null,
        notes: notes.value || '',
        saved_at: Date.now(),
      }))
    } catch { /* quota / private mode — silent */ }
  }, 300)
}

function _restoreDraft() {
  if (!_draftKey.value) return
  let raw
  try { raw = localStorage.getItem(_draftKey.value) } catch { return }
  if (!raw) return
  let parsed
  try { parsed = JSON.parse(raw) } catch { return }
  if (parsed?.service_key && services.value.length) {
    const svc = services.value.find(s => s.service === parsed.service_key)
    if (svc) {
      selectedService.value = svc
      draftRestored.value = true
    }
  }
  if (parsed?.notes) notes.value = parsed.notes
}

function _clearDraft() {
  if (!_draftKey.value) return
  try { localStorage.removeItem(_draftKey.value) } catch { /* silent */ }
  draftRestored.value = false
}

function discardDraft() {
  selectedService.value = null
  notes.value = ''
  _clearDraft()
}

const open = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

async function loadServices() {
  loading.value = true
  try {
    const data = await api.get('/api/mobile/quotes/services')
    services.value = data.services || []
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not load services', detail: e.message, life: 4000 })
  } finally {
    loading.value = false
  }
}

watch(() => props.visible, async (v) => {
  if (v) {
    selectedService.value = null
    notes.value = ''
    draftRestored.value = false
    if (services.value.length === 0) await loadServices()
    // Restore AFTER services loaded — otherwise we can't resolve service_key.
    _restoreDraft()
  }
})

watch([selectedService, notes], () => { _writeDraftSoon() })

onMounted(async () => {
  if (props.visible) {
    await loadServices()
    _restoreDraft()
  }
})

function pickService(svc) {
  selectedService.value = svc
}

function backToList() {
  selectedService.value = null
}

function tierTotal(tier) {
  return (tier.line_items || []).reduce(
    (s, li) => s + (Number(li.unit_price) || 0) * (Number(li.quantity) || 0),
    0,
  )
}

function fmtMoney(n) {
  return `$${(Number(n) || 0).toFixed(2)}`
}

function tierBadgeSeverity(id) {
  if (id === 'good') return 'secondary'
  if (id === 'better') return 'info'
  if (id === 'best') return 'success'
  return 'secondary'
}

async function buildAndPresent() {
  if (!selectedService.value || !props.job?.id) return
  building.value = true
  try {
    const payload = {
      service: selectedService.value.service,
      label: selectedService.value.label,
      notes: notes.value || null,
    }
    const quote = await api.post(`/api/mobile/jobs/${props.job.id}/quote`, payload)
    toast.add({ severity: 'success', summary: 'Quote built', detail: `${quote.tiers?.length || 0} tiers ready`, life: 2500 })
    _clearDraft()
    emit('saved', quote)
    emit('present', quote)
    open.value = false
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not build quote', detail: e.message, life: 5000 })
  } finally {
    building.value = false
  }
}
</script>

<template>
  <Dialog
    v-model:visible="open"
    header="Build a quote"
    modal
    :style="{ width: '94vw', maxWidth: '520px' }"
  >
    <div v-if="loading" class="qb-loading">
      <i class="pi pi-spin pi-spinner" /> Loading services…
    </div>

    <!-- LIST: pick a service -->
    <div v-else-if="!selectedService" class="qb-list">
      <div class="qb-list-hint">Pick a service to quote.</div>
      <ul class="svc-list">
        <li
          v-for="svc in services"
          :key="svc.service"
          class="svc-item"
          @click="pickService(svc)"
        >
          <div class="svc-item-main">
            <strong>{{ svc.label }}</strong>
            <span class="muted">{{ svc.description }}</span>
          </div>
          <i class="pi pi-chevron-right" />
        </li>
      </ul>
    </div>

    <!-- DETAIL: show GBB tiers, build button -->
    <div v-else class="qb-detail">
      <div v-if="draftRestored" class="qb-draft-hint" data-testid="qb-draft-restored">
        <i class="pi pi-history" /> Restored from earlier
        <Button label="Discard" text size="small" @click="discardDraft" />
      </div>
      <div class="qb-detail-header">
        <Button icon="pi pi-arrow-left" text size="small" @click="backToList" label="Back" />
        <strong>{{ selectedService.label }}</strong>
      </div>
      <p class="muted qb-svc-desc">{{ selectedService.description }}</p>

      <div class="tier-grid">
        <div
          v-for="tier in selectedService.tiers"
          :key="tier.id"
          class="tier-card"
          :class="`tier-${tier.id}`"
        >
          <div class="tier-head">
            <Tag :value="tier.id.toUpperCase()" :severity="tierBadgeSeverity(tier.id)" />
            <strong>{{ tier.label }}</strong>
          </div>
          <p class="tier-desc">{{ tier.description }}</p>
          <ul class="tier-lines">
            <li v-for="(li, idx) in tier.line_items" :key="idx">
              <span>{{ li.description }}</span>
              <span class="muted">×{{ li.quantity }}</span>
              <span>{{ fmtMoney(Number(li.unit_price) * Number(li.quantity)) }}</span>
            </li>
          </ul>
          <div class="tier-total">
            <span>Total</span>
            <strong>{{ fmtMoney(tierTotal(tier)) }}</strong>
          </div>
          <div v-if="tier.warranty_months" class="tier-warranty muted">
            <i class="pi pi-shield" />
            {{ tier.warranty_months >= 999 ? 'Lifetime warranty' : `${tier.warranty_months}-month warranty` }}
          </div>
        </div>
      </div>

      <div class="form-field qb-notes">
        <label>Notes for the customer (optional)</label>
        <Textarea v-model="notes" rows="2" autoResize placeholder="Anything specific…" />
      </div>
    </div>

    <template #footer>
      <Button label="Cancel" text @click="open = false" />
      <Button
        v-if="selectedService"
        label="Hand to customer"
        icon="pi pi-user"
        severity="success"
        :loading="building"
        @click="buildAndPresent"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.qb-loading { padding: 1rem; text-align: center; color: var(--p-text-muted-color); }

.qb-draft-hint {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.4rem 0.6rem;
  margin-bottom: 0.6rem;
  background: var(--p-blue-50);
  color: var(--p-blue-700);
  border-radius: 0.5rem;
  font-size: 0.8rem;
}

.qb-list-hint { font-size: 0.85rem; color: var(--p-text-muted-color); margin-bottom: 0.6rem; }

.svc-list { list-style: none; padding: 0; margin: 0; }
.svc-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.85rem 1rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem;
  margin-bottom: 0.55rem;
  cursor: pointer;
  background: var(--p-content-background, white);
  transition: background 0.15s ease;
}
.svc-item:hover, .svc-item:active { background: var(--p-highlight-background, #f3f4f6); }
.svc-item-main { display: flex; flex-direction: column; gap: 0.15rem; }
.svc-item .pi-chevron-right { color: var(--p-text-muted-color); }

.qb-detail-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.4rem;
}
.qb-svc-desc { margin: 0 0 0.85rem 0; font-size: 0.85rem; }

.tier-grid {
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
  margin-bottom: 0.85rem;
}
.tier-card {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.65rem;
  padding: 0.75rem 0.85rem;
  background: var(--p-content-background, white);
}
.tier-card.tier-best { border-color: #15803d; box-shadow: 0 0 0 1px #15803d40 inset; }
.tier-card.tier-better { border-color: #2563eb; box-shadow: 0 0 0 1px #2563eb40 inset; }

.tier-head { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }
.tier-desc { margin: 0 0 0.5rem 0; font-size: 0.8rem; color: var(--p-text-muted-color); }
.tier-lines { list-style: none; padding: 0; margin: 0 0 0.5rem 0; font-size: 0.8rem; }
.tier-lines li {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 0.4rem;
  padding: 0.18rem 0;
}
.tier-total { display: flex; justify-content: space-between; padding-top: 0.4rem; border-top: 1px dashed var(--p-content-border-color); }
.tier-total strong { font-size: 1rem; }
.tier-warranty { display: flex; align-items: center; gap: 0.3rem; margin-top: 0.4rem; font-size: 0.75rem; }

.form-field { display: flex; flex-direction: column; gap: 0.3rem; }
.form-field label { font-size: 0.85rem; color: var(--p-text-muted-color); }
.qb-notes { margin-top: 0.6rem; }
.muted { color: var(--p-text-muted-color, #6b7280); font-size: 0.8rem; }
</style>

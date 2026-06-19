<script setup>
// Mobile-shaped Job-closeout sheet — Phase 2 / C3.
//
// Doug 2026-05-10: Phase 1 routed dispatch's "Complete" through the gated
// /complete endpoint so the silent-disappearing-job class was closed.
// Phase 2 promotes completion from a status flip to a closeout transaction:
// parts used + hours + signature + notes get captured in one POST and
// written to the new JobCloseout snapshot table for audit + billing.
//
// Submit: POST /api/jobs/{id}/closeout — single transaction (the backend
// inserts JobPart rows for inventory-tracked parts, attaches the calling
// tech's open time_entry to this job, writes a JobCloseout snapshot, flips
// lifecycle to 'completed', writes an audit row). 422 with `missing[]` if
// the tenant gates require parts/hours/signature and the form is short.
//
// Sections:
//  1. Parts (SKU autocomplete via /api/parts-needed/sku-suggest)
//  2. Hours (defaults to open work time-entry duration if visible)
//  3. Signature canvas
//  4. Notes
//
// Caller wires v-model:visible + @closed-out to a parent (MobileTodayView
// job cards or DispatchView Status="Complete" handler).

import { ref, reactive, computed, watch, nextTick } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

const props = defineProps({
  visible: { type: Boolean, default: false },
  jobId: { type: String, default: null },
  jobTitle: { type: String, default: '' },
  customerName: { type: String, default: '' },
})
const emit = defineEmits(['update:visible', 'closed-out'])

const api = useApi()
const toast = useToast()

const open = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

// ─── Parts state ─────────────────────────────────────────────────────
const parts = ref([])
function addPartRow() {
  parts.value.push({
    name: '',
    sku: null,
    part_id: null,
    qty: 1,
    unit_cost: 0,
    suggestions: [],
    suggestionsLoading: false,
    _searchTimer: null,
    _searchSeq: 0,
  })
}
function removePartRow(idx) {
  parts.value.splice(idx, 1)
}
function onPartNameInput(row) {
  if (row._searchTimer) clearTimeout(row._searchTimer)
  // Tech edits the name; selection from the suggestions list overwrites
  // sku + part_id below.
  const q = (row.name || '').trim()
  if (q.length < 2) {
    row.suggestions = []
    return
  }
  row._searchTimer = setTimeout(async () => {
    const seq = ++row._searchSeq
    row.suggestionsLoading = true
    try {
      const r = await api.get(
        `/api/parts-needed/sku-suggest?q=${encodeURIComponent(q)}&limit=8`,
        { suppressErrorToast: true },
      )
      if (seq !== row._searchSeq) return
      row.suggestions = Array.isArray(r) ? r : []
    } catch {
      if (seq !== row._searchSeq) return
      row.suggestions = []
    } finally {
      if (seq === row._searchSeq) row.suggestionsLoading = false
    }
  }, 250)
}
function pickSuggestion(row, s) {
  row.name = s.name || s.sku || row.name
  row.sku = s.sku || null
  // Only inventory-source suggestions carry a real part_id (the door_catalog
  // and custom_door sources don't have a parts.id). The backend handles
  // non-inventory rows by snapshotting only — see C2.
  row.part_id = s.source === 'parts' ? (s.part_id || null) : null
  row.suggestions = []
}

// ─── Hours / signature / notes ───────────────────────────────────────
const hours = ref(0)
const notes = ref('')
const signedBy = ref('')
const sigCanvas = ref(null)
const sigDrawn = ref(false)
const canvasSize = { w: 320, h: 140 }
let drawing = false
let lastPt = null

function sigStart(e) {
  if (!sigCanvas.value) return
  drawing = true
  const c = sigCanvas.value
  const rect = c.getBoundingClientRect()
  lastPt = {
    x: (e.clientX - rect.left) * (c.width / rect.width),
    y: (e.clientY - rect.top) * (c.height / rect.height),
  }
  e.preventDefault()
}
function sigMove(e) {
  if (!drawing || !sigCanvas.value) return
  const c = sigCanvas.value
  const rect = c.getBoundingClientRect()
  const x = (e.clientX - rect.left) * (c.width / rect.width)
  const y = (e.clientY - rect.top) * (c.height / rect.height)
  const ctx = c.getContext('2d')
  ctx.beginPath()
  ctx.moveTo(lastPt.x, lastPt.y)
  ctx.lineTo(x, y)
  ctx.stroke()
  lastPt = { x, y }
  sigDrawn.value = true
  e.preventDefault()
}
function sigEnd() {
  drawing = false
  lastPt = null
}
function clearCanvas() {
  const c = sigCanvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  ctx.fillStyle = '#fff'
  ctx.fillRect(0, 0, c.width, c.height)
  sigDrawn.value = false
}

// ─── Submit ──────────────────────────────────────────────────────────
const saving = ref(false)

const canSubmit = computed(() => {
  // The backend tenant-gate applies the OFFICIAL rules; this is just
  // local validation. Any non-empty intent submits; backend 422s with
  // `missing[]` if the tenant requires parts/hours/signature.
  if (!props.jobId) return false
  // Ensure at least one of the four sections has content, otherwise the
  // submit is effectively a bare /complete and the user should use the
  // status dropdown instead.
  const hasParts = parts.value.length > 0 && parts.value.every((p) => p.name.trim())
  const hasHours = (Number(hours.value) || 0) > 0
  const hasSig = sigDrawn.value
  const hasNotes = (notes.value || '').trim().length > 0
  if (!(hasParts || hasHours || hasSig || hasNotes)) return false
  // Required: every parts row needs a name + qty >= 1.
  for (const p of parts.value) {
    if (!p.name.trim()) return false
    if (!(Number(p.qty) >= 1)) return false
  }
  return true
})

async function submit() {
  if (!canSubmit.value || saving.value) return
  saving.value = true

  let signature_data = null
  if (sigDrawn.value && sigCanvas.value) {
    signature_data = sigCanvas.value.toDataURL('image/png')
  }

  const payload = {
    parts: parts.value.map((p) => ({
      part_id: p.part_id || null,
      sku: p.sku || null,
      name: p.name.trim(),
      qty: Number(p.qty) || 1,
      unit_cost: Number(p.unit_cost) || 0,
    })),
    hours: Number(hours.value) || 0,
    signature_data,
    signed_by: signedBy.value.trim() || null,
    notes: notes.value.trim() || null,
  }

  try {
    const created = await api.post(`/api/jobs/${props.jobId}/closeout`, payload)
    toast.add({
      severity: 'success',
      summary: 'Job closed out',
      detail: 'Moved to Ready for Billing — review and invoice it from /billing.',
      life: 5000,
    })
    emit('closed-out', created)
    _resetForm()
    open.value = false
  } catch (err) {
    const missing = err?.body?.missing || []
    if (missing.length) {
      const labels = {
        parts: 'parts logged',
        hours: 'labor hours',
        signature: 'customer signature',
      }
      toast.add({
        severity: 'warn',
        summary: 'Cannot close out yet',
        detail: 'Add: ' + missing.map((m) => labels[m] || m).join(', '),
        life: 6000,
      })
    } else {
      toast.add({
        severity: 'error',
        summary: 'Could not close out',
        detail: err?.message || 'Try again.',
        life: 5000,
      })
    }
  } finally {
    saving.value = false
  }
}

function _resetForm() {
  parts.value = []
  hours.value = 0
  notes.value = ''
  // Pre-fill the signer with the customer's name when known — saves the
  // tech a tap on every closeout. They can edit if a different person
  // (spouse, manager, on-site contact) is actually signing.
  signedBy.value = props.customerName || ''
  sigDrawn.value = false
  drawing = false
  lastPt = null
}

watch(open, async (v) => {
  if (v) {
    _resetForm()
    await nextTick()
    clearCanvas()
  }
})
</script>

<template>
  <Dialog
    v-model:visible="open"
    :header="`Close out — ${jobTitle || 'Job'}`"
    modal
    :style="{ width: '95vw', maxWidth: '560px' }"
    :breakpoints="{ '768px': '100vw' }"
    data-testid="mobile-job-closeout-dialog"
  >
    <p v-if="customerName" class="muted hint">{{ customerName }}</p>

    <form class="form-stack" @submit.prevent="submit">
      <!-- Parts -->
      <section class="section">
        <header class="section-head">
          <h3>Parts used</h3>
          <Button
            type="button"
            label="Add part"
            icon="pi pi-plus"
            size="small"
            severity="secondary"
            text
            data-testid="mjco-add-part"
            @click="addPartRow"
          />
        </header>
        <ul v-if="parts.length" class="parts-list" data-testid="mjco-parts-list">
          <li v-for="(p, idx) in parts" :key="idx" class="part-row">
            <div class="part-row-main">
              <InputText
                v-model="p.name"
                placeholder="Part name or SKU"
                class="w-full"
                :data-testid="`mjco-part-name-${idx}`"
                autocomplete="off"
                @input="onPartNameInput(p)"
              />
              <ul v-if="p.suggestions.length" class="suggest-list">
                <li
                  v-for="s in p.suggestions"
                  :key="`${s.source}-${s.sku}`"
                  class="suggest-item"
                  :data-testid="`mjco-part-suggestion-${idx}`"
                  @click="pickSuggestion(p, s)"
                >
                  <strong>{{ s.sku }}</strong>
                  <span class="muted"> · {{ s.name }}</span>
                  <span v-if="s.qty_on_hand != null" class="qty-pill">{{ s.qty_on_hand }} on hand</span>
                </li>
              </ul>
            </div>
            <input
              v-model.number="p.qty"
              type="number"
              min="1"
              max="999"
              class="qty-input"
              :data-testid="`mjco-part-qty-${idx}`"
              aria-label="Quantity"
            />
            <Button
              icon="pi pi-times"
              text
              severity="danger"
              size="small"
              :data-testid="`mjco-part-remove-${idx}`"
              @click="removePartRow(idx)"
            />
          </li>
        </ul>
        <p v-else class="muted hint">No parts yet — tap "Add part" for each one you installed.</p>
      </section>

      <!-- Hours -->
      <section class="section">
        <header class="section-head"><h3>Labor</h3></header>
        <div class="form-field">
          <label for="mjco-hours">Hours worked</label>
          <input
            id="mjco-hours"
            v-model.number="hours"
            type="number"
            min="0"
            max="99"
            step="0.25"
            class="hours-input"
            data-testid="mjco-hours"
            inputmode="decimal"
          />
        </div>
      </section>

      <!-- Signature -->
      <section class="section">
        <header class="section-head"><h3>Customer signature</h3></header>
        <div class="form-field">
          <label for="mjco-signed-by">Signed by</label>
          <InputText
            id="mjco-signed-by"
            v-model="signedBy"
            placeholder="Customer name"
            class="w-full"
            data-testid="mjco-signed-by"
          />
        </div>
        <div class="sig-canvas-wrap">
          <canvas
            ref="sigCanvas"
            class="sig-canvas"
            :width="canvasSize.w"
            :height="canvasSize.h"
            data-testid="mjco-sig-canvas"
            @pointerdown="sigStart"
            @pointermove="sigMove"
            @pointerup="sigEnd"
            @pointerleave="sigEnd"
          />
          <button type="button" class="sig-clear" @click="clearCanvas" data-testid="mjco-sig-clear">
            Clear
          </button>
        </div>
      </section>

      <!-- Notes -->
      <section class="section">
        <header class="section-head"><h3>Notes <span class="muted">(optional)</span></h3></header>
        <Textarea
          v-model="notes"
          rows="2"
          autoResize
          class="w-full"
          placeholder="Anything dispatch should know before invoicing"
          data-testid="mjco-notes"
        />
      </section>
    </form>

    <template #footer>
      <Button label="Cancel" text severity="secondary" @click="open = false" />
      <Button
        label="Close out"
        icon="pi pi-check"
        :disabled="!canSubmit"
        :loading="saving"
        data-testid="mjco-submit"
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
  color: var(--p-text-muted-color);
  font-weight: 500;
}
.section {
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.65rem;
  padding: 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  background: var(--p-content-background);
  color: var(--p-text-color);
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}
.section-head h3 {
  margin: 0;
  font-size: 0.95rem;
  font-weight: 700;
}
.muted {
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
  font-weight: 400;
}
.hint { margin: 0; }

.parts-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.part-row {
  display: grid;
  grid-template-columns: 1fr 4.5rem auto;
  gap: 0.4rem;
  align-items: start;
}
.part-row-main { display: flex; flex-direction: column; }

.suggest-list {
  list-style: none;
  margin: 0.25rem 0 0 0;
  padding: 0;
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.5rem;
  background: var(--p-content-background);
  max-height: 220px;
  overflow-y: auto;
}
.suggest-item {
  padding: 0.65rem 0.75rem;
  cursor: pointer;
  border-bottom: 1px solid var(--p-content-border-color);
  min-height: 44px;
  display: flex;
  align-items: center;
  gap: 0.35rem;
}
.suggest-item:last-child { border-bottom: 0; }
.suggest-item:hover {
  background: var(--p-content-hover-background);
  color: var(--p-content-hover-color);
}

.qty-input,
.hours-input {
  padding: 0.6rem 0.5rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.5rem;
  text-align: center;
  font: inherit;
  background: var(--p-content-background);
  color: var(--p-text-color);
  min-height: 44px;
  width: 100%;
}
.qty-pill {
  margin-left: auto;
  background: var(--p-highlight-background);
  color: var(--p-highlight-color);
  border-radius: 999px;
  padding: 0.05rem 0.45rem;
  font-size: 0.7rem;
  font-weight: 600;
}

.sig-canvas-wrap {
  position: relative;
  border: 1px dashed var(--p-content-border-color);
  border-radius: 0.5rem;
  background: #fff; /* canvas itself stays white so signatures are visible regardless of theme */
  height: 140px;
}
.sig-canvas {
  width: 100%;
  height: 100%;
  display: block;
  border-radius: 0.5rem;
  touch-action: none;
}
.sig-clear {
  position: absolute;
  right: 0.4rem;
  top: 0.4rem;
  font-size: 0.75rem;
  background: var(--p-content-background);
  color: var(--p-text-color);
  border: 1px solid var(--p-content-border-color);
  border-radius: 999px;
  padding: 0.2rem 0.55rem;
  cursor: pointer;
}
</style>

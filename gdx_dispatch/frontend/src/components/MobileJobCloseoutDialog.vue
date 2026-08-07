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
//  1. Parts used (SKU autocomplete via /api/parts-needed/sku-suggest)
//  2. Parts to order (free-text; lands in the office Parts-to-Order queue)
//  3. Return visit (toggle + required why; backend spawns the child job)
//  4. Hours (defaults to open work time-entry duration if visible)
//  5. Signature canvas
//  6. Notes
//
// Caller wires v-model:visible + @closed-out to a parent (MobileTodayView
// job cards or DispatchView Status="Complete" handler).

import { ref, reactive, computed, watch, nextTick } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Select from 'primevue/select'
import { isInstallLane as _isInstallLane } from '../constants/jobTypes'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

const props = defineProps({
  visible: { type: Boolean, default: false },
  jobId: { type: String, default: null },
  jobTitle: { type: String, default: '' },
  jobType: { type: String, default: '' },
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
// PR5 (Doug 2026-07-07): explicit attestation — satisfies the tenant's
// require-parts gate without a parts list; bare silence still 422s.
const noPartsUsed = ref(false)
function addPartRow() {
  noPartsUsed.value = false
  parts.value.push({
    name: '',
    sku: null,
    part_id: null,
    qty: 1,
    unit_cost: 0,
    note: '',
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
  // A picked catalog identity must never outlive the exact name it was
  // picked for: the office orders by sku and inventory decrements by
  // part_id, so a renamed row carrying the old pick would order/decrement
  // the WRONG part. Any manual edit clears both; re-picking re-sets them.
  row.sku = null
  row.part_id = null
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

// ─── Parts already requested on this job ─────────────────────────────
// Read-only context (2026-08-06 incident): a part requested minutes
// earlier via the job's Parts card was invisible here, so the tech
// re-typed it into the closeout NOTES — where nothing orders or bills
// it. Showing the open request rows ends the double-entry. Best-effort:
// a failed read never blocks a closeout.
const existingRequests = ref([])
async function _loadExistingRequests() {
  if (!props.jobId) return
  try {
    const rows = await api.get(
      `/api/jobs/${props.jobId}/parts-needed?unbilled=true`,
      { suppressErrorToast: true },
    )
    existingRequests.value = (Array.isArray(rows) ? rows : []).filter(
      (r) => r.source === 'request' && r.status !== 'cancelled',
    )
  } catch {
    existingRequests.value = []
  }
}

// ─── Return visit + parts to order (Doug 2026-08-04) ─────────────────
// "Does this need a return visit and why?" asked at the moment the tech
// knows the answer. The why is required — dispatch schedules from it.
const returnVisitNeeded = ref(false)
const returnVisitReason = ref('')
// Parts the job still NEEDS — typed free-text first; the catalog suggest
// is an upgrade, never a requirement. These land in the office
// Parts-to-Order queue exactly like the job-screen Parts card's requests.
const orderParts = ref([])
function addOrderRow() {
  orderParts.value.push({
    name: '',
    sku: null,
    part_id: null, // set by pickSuggestion, ignored by the payload — needed rows never touch inventory
    qty: 1,
    urgent: false,
    suggestions: [],
    suggestionsLoading: false,
    _searchTimer: null,
    _searchSeq: 0,
  })
}
function removeOrderRow(idx) {
  orderParts.value.splice(idx, 1)
}

// ─── Hours / signature / notes ───────────────────────────────────────
const hours = ref(0)
// Plan §11 (Doug): "ask how many techs on site". Billing input only — billed
// man-hours = hours × techs under §8; never payroll for the other techs.
const techsOnSite = ref(1)
// The confirm step: submit first shows the man-hours consequence IN the
// dialog (not an overlay — overlay-on-dialog is a z-index gamble, and this
// control must fail CLOSED). "Is this how many hours you meant?"
const confirmStep = ref(false)
const confirmArmedAt = ref(0)
// Plan §8/§14 gap 1: the install flat-price picker. Only relevant for the
// install lane; service jobs price hourly and never see it.
// Canonical lane, not a raw string (audit round 2): an install from the quote
// flow arrives as 'installation'/'Install' and must still show the picker.
const isInstallLane = computed(() => _isInstallLane(props.jobType))
const matrixItems = ref([])
const matrixItemId = ref(null)
async function loadMatrix() {
  if (!isInstallLane.value || matrixItems.value.length) return
  try {
    const r = await api.get('/api/labor-pricing/items?active=true', { suppressErrorToast: true })
    matrixItems.value = (Array.isArray(r) ? r : r?.items || []).map((i) => ({
      label: `${i.description} — $${Number(i.flat_price).toFixed(2)}`,
      value: String(i.id),
    }))
  } catch {
    matrixItems.value = []
  }
}
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
  const hasOrders = orderParts.value.length > 0
  if (!(hasParts || hasHours || hasSig || hasNotes || noPartsUsed.value || hasOrders || returnVisitNeeded.value)) return false
  // Required: every parts row needs a name + qty >= 1.
  for (const p of parts.value) {
    if (!p.name.trim()) return false
    if (!(Number(p.qty) >= 1)) return false
  }
  for (const p of orderParts.value) {
    if (!p.name.trim()) return false
    if (!(Number(p.qty) >= 1)) return false
  }
  // Return visit demands its why — same rule the backend enforces.
  if (returnVisitNeeded.value && !returnVisitReason.value.trim()) return false
  return true
})

async function submit() {
  if (!canSubmit.value || saving.value) return
  // Plan §11: first tap shows the review strip with the consequence
  // (hours × techs = billed man-hours); only an explicit second confirm
  // POSTs. A bare "are you sure?" gets tapped through — the NUMBER is what
  // makes a typed-from-memory figure trustworthy enough to bill.
  if (!confirmStep.value) {
    confirmStep.value = true
    // Dwell (audit round 2): the strip appears exactly where the button was,
    // so an impatient double-tap would confirm unread. Refuse the second tap
    // for a beat.
    confirmArmedAt.value = Date.now()
    return
  }
  if (Date.now() - confirmArmedAt.value < 1200) return
  confirmStep.value = false
  saving.value = true

  let signature_data = null
  if (sigDrawn.value && sigCanvas.value) {
    signature_data = sigCanvas.value.toDataURL('image/png')
  }

  const payload = {
    parts: noPartsUsed.value ? [] : parts.value.map((p) => ({
      part_id: p.part_id || null,
      sku: p.sku || null,
      name: p.name.trim(),
      qty: Number(p.qty) || 1,
      unit_cost: Number(p.unit_cost) || 0,
      note: (p.note || '').trim() || null,
    })),
    no_parts_used: noPartsUsed.value,
    hours: Number(hours.value) || 0,
    techs_on_site: Math.max(1, Math.min(10, Number(techsOnSite.value) || 1)),
    labor_matrix_item_id: isInstallLane.value ? (matrixItemId.value || null) : null,
    signature_data,
    signed_by: signedBy.value.trim() || null,
    notes: notes.value.trim() || null,
    needs_return_visit: returnVisitNeeded.value,
    return_visit_reason: returnVisitNeeded.value ? returnVisitReason.value.trim() : null,
    parts_to_order: orderParts.value.map((p) => ({
      name: p.name.trim(),
      sku: p.sku || null,
      // Same clamp rationale as the job-screen Parts card: `:max` only
      // clamps on blur, so bound the value on the path that sends it.
      qty: Math.min(99, Math.max(1, Math.trunc(Number(p.qty) || 1))),
      urgency: p.urgent ? 'urgent' : 'normal',
    })),
  }

  try {
    // 2026-07-01 UX audit: closeout goes through the offline queue. In a
    // dead zone the whole payload (signature PNG included) persists to
    // IndexedDB and replays with an Idempotency-Key on reconnect — the
    // tech's 5 minutes of data entry survives the signal drop.
    const created = await api.postQueued(`/api/jobs/${props.jobId}/closeout`, payload, {
      actionType: 'job.closeout', resourceId: String(props.jobId),
    })
    if (created?.queued) {
      toast.add({
        severity: 'warn',
        summary: 'Saved offline',
        detail: 'No signal — the closeout is stored on this phone and will submit automatically when you reconnect.',
        life: 6000,
      })
    } else {
      toast.add({
        severity: 'success',
        summary: 'Job closed out',
        // Autodraft (2026-08-07): when the closeout minted a draft
        // invoice, say so by number — "look at that invoice" starts here.
        detail: created?.autodraft_invoice_number
          ? `Invoice ${created.autodraft_invoice_number} drafted from your parts + hours — the office reviews it on the Billing screen.`
          : 'Moved to Ready for Billing — review and invoice it from /billing.',
        life: 6000,
      })
      if (created?.return_visit_job_id) {
        toast.add({
          severity: 'info',
          summary: 'Return visit created',
          detail: 'Dispatch will schedule it — your reason is on the new job.',
          life: 5000,
        })
      }
    }
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
        return_visit_reason: 'why the return visit is needed',
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

// 2026-07-01 UX audit: with real work in the form (parts rows, hours, a
// drawn signature, notes), tapping the header X or Escape used to discard
// everything silently. Dirty → the X and Escape are disabled; Cancel asks.
const isDirty = computed(() =>
  parts.value.length > 0 ||
  Number(hours.value) > 0 ||
  notes.value.trim() !== '' ||
  sigDrawn.value ||
  orderParts.value.length > 0 ||
  returnVisitNeeded.value ||
  returnVisitReason.value.trim() !== ''
)

function requestCancel() {
  if (isDirty.value && !window.confirm('Discard this closeout? Parts, hours, and the signature will be lost.')) {
    return
  }
  open.value = false
}

function _resetForm() {
  parts.value = []
  hours.value = 0
  techsOnSite.value = 1
  matrixItemId.value = null
  confirmStep.value = false
  notes.value = ''
  returnVisitNeeded.value = false
  returnVisitReason.value = ''
  orderParts.value = []
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
    existingRequests.value = []
    _loadExistingRequests()
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
    :closable="!isDirty"
    :close-on-escape="!isDirty"
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
            <InputText
              v-if="!p.part_id"
              v-model="p.note"
              placeholder="Not in system? Explain it for the office"
              class="w-full part-note"
              :data-testid="`mjco-part-note-${idx}`"
            />
            <Button
              icon="pi pi-times"
              v-tooltip="'Remove part'"
              aria-label="Remove part"
              text
              severity="danger"
              size="small"
              :data-testid="`mjco-part-remove-${idx}`"
              @click="removePartRow(idx)"
            />
          </li>
        </ul>
        <p v-else class="muted hint">No parts yet — tap "Add part" for each one you installed.</p>
        <!-- PR5: deliberate attestation. With the tenant's require-parts
             gate on, this is the only way to complete with an empty list. -->
        <label v-if="!parts.length" class="no-parts-attest" data-testid="mjco-no-parts-used">
          <input type="checkbox" v-model="noPartsUsed" />
          <span>No parts were used on this job</span>
        </label>
      </section>

      <!-- Parts to order — free-text first; catalog match never required.
           Lands in the office Parts-to-Order queue as a normal request. -->
      <section class="section">
        <header class="section-head">
          <h3>Parts to order <span class="muted">(optional)</span></h3>
          <Button
            type="button"
            label="Add part"
            icon="pi pi-plus"
            size="small"
            severity="secondary"
            text
            data-testid="mjco-add-order-part"
            @click="addOrderRow"
          />
        </header>
        <!-- Open requests already on this job (Parts card or an earlier
             closeout) — read-only so nothing gets re-typed or re-ordered. -->
        <ul v-if="existingRequests.length" class="parts-list" data-testid="mjco-existing-requests">
          <li v-for="r in existingRequests" :key="r.id" class="part-row existing-request">
            <span class="muted">
              <i class="pi pi-check-circle" style="font-size: 0.8rem" />
              {{ r.part_name }} ×{{ r.quantity }} — already requested
            </span>
            <span class="qty-pill">{{ r.status }}</span>
          </li>
        </ul>
        <ul v-if="orderParts.length" class="parts-list" data-testid="mjco-order-list">
          <li v-for="(p, idx) in orderParts" :key="idx" class="part-row">
            <div class="part-row-main">
              <InputText
                v-model="p.name"
                maxlength="200"
                placeholder="Type the part — no catalog match needed"
                class="w-full"
                :data-testid="`mjco-order-name-${idx}`"
                autocomplete="off"
                @input="onPartNameInput(p)"
              />
              <ul v-if="p.suggestions.length" class="suggest-list">
                <li
                  v-for="s in p.suggestions"
                  :key="`${s.source}-${s.sku}`"
                  class="suggest-item"
                  :data-testid="`mjco-order-suggestion-${idx}`"
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
              max="99"
              class="qty-input"
              :data-testid="`mjco-order-qty-${idx}`"
              aria-label="Quantity"
            />
            <Button
              icon="pi pi-times"
              v-tooltip="'Remove part'"
              aria-label="Remove part"
              text
              severity="danger"
              size="small"
              :data-testid="`mjco-order-remove-${idx}`"
              @click="removeOrderRow(idx)"
            />
            <label class="order-urgent">
              <input type="checkbox" v-model="p.urgent" :data-testid="`mjco-order-urgent-${idx}`" />
              <span>Urgent</span>
            </label>
          </li>
        </ul>
        <p v-else class="muted hint">Need something for a return trip? Add it here — the office orders it.</p>
      </section>

      <!-- Return visit -->
      <section class="section">
        <header class="section-head"><h3>Return visit</h3></header>
        <label class="no-parts-attest" data-testid="mjco-return-visit">
          <input type="checkbox" v-model="returnVisitNeeded" />
          <span>This job needs a return visit</span>
        </label>
        <div v-if="returnVisitNeeded" class="form-field">
          <label for="mjco-return-reason">Why? <span class="muted">(required)</span></label>
          <Textarea
            id="mjco-return-reason"
            v-model="returnVisitReason"
            rows="2"
            auto-resize
            maxlength="1000"
            class="w-full"
            placeholder="Waiting on parts, warranty, second tech needed…"
            data-testid="mjco-return-reason"
          />
          <small class="muted">Creates an unscheduled job for dispatch with this reason on it.</small>
        </div>
      </section>

      <!-- Hours -->
      <section class="section">
        <header class="section-head"><h3>Labor</h3></header>
        <div v-if="isInstallLane" class="form-field" data-testid="mjco-install-picker">
          <label for="mjco-matrix">Install price (from the matrix)</label>
          <Select
            id="mjco-matrix"
            v-model="matrixItemId"
            :options="matrixItems"
            option-label="label"
            option-value="value"
            placeholder="Pick the install line…"
            class="w-full"
            data-testid="mjco-matrix-select"
            @before-show="loadMatrix"
          />
          <small class="muted">Installs bill this flat price; hours below are for records only.</small>
        </div>
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
        <div class="form-field">
          <label for="mjco-techs">Techs on site</label>
          <input
            id="mjco-techs"
            v-model.number="techsOnSite"
            type="number"
            min="1"
            max="10"
            step="1"
            class="hours-input"
            data-testid="mjco-techs-on-site"
            inputmode="numeric"
          />
          <small class="muted">Counts toward the bill, not anyone's paycheck.</small>
        </div>
        <!-- §11 review strip — rendered IN the dialog so it cannot silently
             fail open the way an overlay confirm can (issue #215's lesson). -->
        <div v-if="confirmStep" class="confirm-strip" data-testid="mjco-confirm-strip">
          <p>
            You entered <strong>{{ (Number(hours) || 0).toFixed(2) }} h</strong>
            with <strong>{{ techsOnSite }} tech{{ techsOnSite === 1 ? '' : 's' }}</strong>
            on site — that bills
            <strong>{{ ((Number(hours) || 0) * techsOnSite).toFixed(2) }} man-hours</strong>.
            Is that what you meant?
          </p>
          <small class="muted">Tap "Close out job" again to confirm, or change the numbers above.</small>
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
      <Button label="Cancel" text severity="secondary" data-testid="mjco-cancel" @click="requestCancel" />
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
/* min-width: 0 — grid items default to min-width:auto, and the InputText's
   intrinsic width (~233px) is wider than 1fr's share on a 390px phone, so
   the whole grid blew out and pushed the remove ✕ off-screen (caught in the
   2026-08-04 browser walk; affected parts-used rows too). */
.part-row-main { display: flex; flex-direction: column; min-width: 0; }

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

/* Urgent flag on a parts-to-order row — spans under the 3-column grid row
   so the tap target isn't squeezed beside the qty box. */
.order-urgent {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
  min-height: 32px;
}

/* dark-safe: signature paper — white is deliberate in both themes, the ink is dark */
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

/* §11 review strip — high-contrast in both themes via theme vars. */
.confirm-strip {
  border: 1px solid var(--p-amber-400, #fbbf24);
  background: color-mix(in srgb, var(--p-amber-400, #fbbf24) 12%, transparent);
  border-radius: 8px;
  padding: 0.6rem 0.75rem;
  margin-top: 0.5rem;
}
.confirm-strip p { margin: 0 0 0.25rem; }
</style>

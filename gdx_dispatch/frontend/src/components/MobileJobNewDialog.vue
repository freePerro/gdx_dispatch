<script setup>
// Mobile-shaped Job-create flow. Built 2026-05-10 in response to Doug:
// "tech need to be able to make a new job, and add a new customer and
// parts while doing it." Replaces the previous workaround (mobile users
// routed to /jobs?new=1 which opened the desktop dialog on a phone-sized
// viewport — clunky, and unreachable for techs because the router guard
// redirects /jobs → /mobile/jobs for them).
//
// API contract (researched against gdx/routers/jobs.py + customers.py +
// parts_needed.py at HEAD 3e51e8a0):
//   POST /api/customers              { name*, phone?, email?, address? }
//   POST /api/jobs                   { title*, customer_id?, scheduled_at?, ... }
//   POST /api/jobs/{id}/parts-needed { part_name*, quantity, sku?, urgency?, notes? }
//   GET  /api/customers/search?q=    -> [{ id, name, phone, ... }]
//   GET  /api/parts-needed/sku-suggest?q= -> [{ source, sku, name, ... }]
//
// Submit chain is intentionally independent — if customer-create succeeds
// but job-create fails, the customer persists (toast tells the user; they
// can re-try with the now-existing customer). Same for parts: a job is
// created even if a part add fails (parts can be appended later from job
// detail). This matches what desktop JobsView already does.
import { ref, reactive, computed, watch, nextTick } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import ToggleSwitch from 'primevue/toggleswitch'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { usePermission } from '../composables/usePermission'
import { useDirtyDialog } from '../composables/useDirtyDialog'
import FormField from './FormField.vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
})
const emit = defineEmits(['update:visible', 'created'])

const api = useApi()
const toast = useToast()
const { hasPermission } = usePermission()

const open = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

// Gate the Parts section on the same permission that gates the backend
// `POST /api/jobs/{id}/parts-needed` route (`inventory.write`). Otherwise
// a custom-role user with `jobs.write` but no inventory perms would see
// a section that 403s on submit. Builtin roles all line up — this guard
// is for tenant-customized roles.
const canAddParts = computed(() => hasPermission('inventory.write'))

// ─── Customer state ──────────────────────────────────────────────────
const newCustomer = ref(false)
const customerSearch = ref('')
const customerOptions = ref([])
const selectedCustomer = ref(null)
const customerSearching = ref(false)
let _searchTimer = null
// Monotonic request token — only the response from the most recent
// in-flight search is allowed to overwrite customerOptions. Without it,
// fast typing can let an older `q="aa"` response land after the newer
// `q="aab"` response, leaving a stale option list under a fresh query.
let _customerSearchSeq = 0
const newCust = reactive({ name: '', phone: '', email: '', address: '' })

watch(customerSearch, (q) => {
  if (newCustomer.value) return
  if (_searchTimer) clearTimeout(_searchTimer)
  if (!q || q.trim().length < 2) {
    customerOptions.value = []
    return
  }
  _searchTimer = setTimeout(async () => {
    const seq = ++_customerSearchSeq
    customerSearching.value = true
    try {
      const r = await api.get(`/api/customers/search?q=${encodeURIComponent(q.trim())}`)
      if (seq !== _customerSearchSeq) return  // a newer query has been issued
      customerOptions.value = Array.isArray(r) ? r : (r?.items || [])
    } catch {
      if (seq !== _customerSearchSeq) return
      customerOptions.value = []
    } finally {
      if (seq === _customerSearchSeq) customerSearching.value = false
    }
  }, 250)
})

function pickCustomer(c) {
  selectedCustomer.value = c
  customerSearch.value = c?.name || ''
  customerOptions.value = []
}

function clearCustomer() {
  selectedCustomer.value = null
  customerSearch.value = ''
  customerOptions.value = []
}

// ─── Job state ───────────────────────────────────────────────────────
//
// Intentionally NO scheduled_at field. A scheduled job with no tech
// triggers `require_tech_for_scheduled_job` (HTTPException 422) on tenants
// that have `dispatch_block_save_no_tech` on, AND we have no clean way to
// auto-assign the calling tech here (technicians.id ≠ users.id; we'd need
// an extra lookup against /api/technicians). Tech-created jobs default
// to lifecycle "service_call"; dispatch schedules + assigns from desktop later.
// Doug 2026-05-10 / 2026-05-13 confirmed this matches the GDX workflow.
const job = reactive({
  title: '',
  description: '',
  // Sprint dispatch-capacity (2026-05-20) — scheduler's expected hours
  // (decimal, e.g. 1.5). Optional; dispatch falls back to the estimate
  // calc, then to "?h" if nothing's known.
  scheduled_duration_hours: null,
  // Sprint customer-multi-location (2026-05-21) — null = use the
  // customer's primary location (JobDetailView fallback).
  location_id: null,
})

// Sprint customer-multi-location — locations for the picked customer.
// Re-fetched whenever selectedCustomer changes. Picker hidden at ≤1.
const customerLocations = ref([])
watch(selectedCustomer, async (c) => {
  job.location_id = null
  if (!c?.id) {
    customerLocations.value = []
    return
  }
  try {
    const r = await api.get(`/api/customers/${c.id}/locations`)
    customerLocations.value = Array.isArray(r) ? r : []
  } catch (err) {
    // Surface auth/5xx failures in devtools — silent empty hides the
    // picker and lets the tech submit without ever knowing the API died.
    console.warn('MobileJobNewDialog locations fetch failed', err)
    customerLocations.value = []
  }
})

// ─── Parts state ─────────────────────────────────────────────────────
const parts = ref([])
function addPartRow() {
  parts.value.push({
    part_name: '',
    sku: null,
    quantity: 1,
    urgency: 'normal',
    notes: '',
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
  const q = (row.part_name || '').trim()
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
      )
      if (seq !== row._searchSeq) return  // newer keystroke superseded this
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
  row.part_name = s.name || s.sku || row.part_name
  row.sku = s.sku || null
  row.suggestions = []
}

// ─── Submit ──────────────────────────────────────────────────────────
const saving = ref(false)
const titleInput = ref(null)

const canSubmit = computed(() => {
  if (!job.title.trim()) return false
  if (newCustomer.value) {
    if (!newCust.name.trim()) return false
  }
  // Existing-customer path is allowed to be empty (a tech might want a
  // bare job with no customer attached — backend permits customer_id=null).
  for (const p of parts.value) {
    if (!p.part_name.trim()) return false
  }
  return true
})

async function submit() {
  if (!canSubmit.value || saving.value) return
  saving.value = true
  try {
    let customerId = selectedCustomer.value?.id || null

    // Step 1 — create customer if requested.
    if (newCustomer.value) {
      const payload = {
        name: newCust.name.trim(),
        phone: newCust.phone.trim() || null,
        email: newCust.email.trim() || null,
        address: newCust.address.trim() || null,
      }
      try {
        const created = await api.post('/api/customers', payload)
        customerId = created?.id || created?.customer?.id || null
        if (!customerId) throw new Error('Customer creation returned no id')
      } catch (e) {
        toast.add({
          severity: 'error',
          summary: 'Could not create customer',
          detail: e?.message || 'Try again or pick an existing customer.',
          life: 5000,
        })
        return
      }
    }

    // Step 2 — create job. No scheduled_at — see comment on `job` state.
    let createdJob = null
    try {
      const jobPayload = {
        title: job.title.trim(),
        description: job.description.trim() || '',
        customer_id: customerId,
        scheduled_duration_hours:
          job.scheduled_duration_hours != null && job.scheduled_duration_hours !== ''
            ? Number(job.scheduled_duration_hours)
            : null,
        location_id: job.location_id || null,
      }
      createdJob = await api.post('/api/jobs', jobPayload)
    } catch (e) {
      toast.add({
        severity: 'error',
        summary: 'Could not create job',
        detail: e?.message || 'Try again.',
        life: 5000,
      })
      return
    }

    const jobId = createdJob?.id || createdJob?.job?.id
    if (!jobId) {
      toast.add({
        severity: 'warn',
        summary: 'Job created but id missing',
        detail: 'Refresh the list to see it.',
        life: 4000,
      })
      emit('created', null)
      _resetForm()
      open.value = false
      return
    }

    // Step 3 — attach parts (best-effort; per-part failure isn't fatal).
    // Defense-in-depth: even though `parts.value` only mutates via the gated
    // `addPartRow` button, re-check the permission here so a future code
    // path that pushes rows from somewhere else can't bypass the backend
    // gate by submitting parts on behalf of an inventory-disallowed user.
    const partsToSubmit = canAddParts.value ? parts.value : []
    let partsFailed = 0
    for (const p of partsToSubmit) {
      try {
        await api.post(`/api/jobs/${jobId}/parts-needed`, {
          part_name: p.part_name.trim(),
          quantity: Number(p.quantity) || 1,
          sku: p.sku || null,
          urgency: p.urgency || 'normal',
          notes: p.notes?.trim() || '',
        })
      } catch {
        partsFailed += 1
      }
    }

    if (partsFailed > 0) {
      toast.add({
        severity: 'warn',
        summary: `Job created, ${partsFailed} part${partsFailed === 1 ? '' : 's'} failed`,
        detail: 'Open the job to retry.',
        life: 5000,
      })
    } else {
      toast.add({
        severity: 'success',
        summary: 'Job created',
        detail: partsToSubmit.length
          ? `Added ${partsToSubmit.length} part${partsToSubmit.length === 1 ? '' : 's'}.`
          : 'Open the list to see it.',
        life: 3000,
      })
    }

    emit('created', { id: jobId, ...createdJob })
    _resetForm()
    open.value = false
  } finally {
    saving.value = false
  }
}

function _resetForm() {
  newCustomer.value = false
  customerSearch.value = ''
  customerOptions.value = []
  selectedCustomer.value = null
  newCust.name = ''
  newCust.phone = ''
  newCust.email = ''
  newCust.address = ''
  job.title = ''
  job.description = ''
  job.scheduled_duration_hours = null
  job.location_id = null
  customerLocations.value = []
  parts.value = []
}

// Unsaved-changes guard — Esc / the header X are disabled while dirty, and
// Cancel prompts before discarding typed-in work (2026-07-01 UX audit).
// Getter mirrors every user-editable field; part rows are mapped to their
// plain fields so the internal _searchTimer/_searchSeq scratch never leaks
// into the JSON snapshot comparison.
const { snapshot, isDirty, confirmDiscard } = useDirtyDialog(
  () => ({
    newCustomer: newCustomer.value,
    customerSearch: customerSearch.value,
    selectedCustomerId: selectedCustomer.value?.id ?? null,
    newCust: { ...newCust },
    job: { ...job },
    parts: parts.value.map((p) => ({
      part_name: p.part_name,
      sku: p.sku,
      quantity: p.quantity,
      urgency: p.urgency,
      notes: p.notes,
    })),
  }),
  { message: 'Discard this new job?' }
)

function requestCancel() {
  if (!confirmDiscard()) return
  open.value = false
}

// immediate: the dialog can be mounted already-visible; the pristine
// snapshot must exist before the first user keystroke either way.
watch(open, async (v) => {
  if (v) {
    _resetForm()
    snapshot()
    await nextTick()
    titleInput.value?.$el?.focus?.()
  }
}, { immediate: true })
</script>

<template>
  <Dialog
    v-model:visible="open"
    header="New job"
    modal
    :closable="!isDirty"
    :close-on-escape="!isDirty"
    :style="{ width: '95vw', maxWidth: '560px' }"
    :breakpoints="{ '768px': '100vw' }"
    data-testid="mobile-job-new-dialog"
  >
    <form class="form-stack" @submit.prevent="submit">
      <!-- Section: Customer -->
      <section class="section">
        <header class="section-head">
          <h3>Customer</h3>
          <label class="toggle-row">
            <ToggleSwitch v-model="newCustomer" data-testid="mjn-new-customer-toggle" />
            <span>Create new</span>
          </label>
        </header>

        <div v-if="!newCustomer" class="customer-search">
          <InputText
            v-model="customerSearch"
            placeholder="Search by name or phone…"
            class="w-full"
            data-testid="mjn-customer-search"
            autocomplete="off"
          />
          <ul v-if="customerOptions.length" class="suggest-list">
            <li
              v-for="c in customerOptions"
              :key="c.id"
              class="suggest-item"
              data-testid="mjn-customer-option"
              @click="pickCustomer(c)"
            >
              <strong>{{ c.name }}</strong>
              <span v-if="c.phone" class="muted"> · {{ c.phone }}</span>
            </li>
          </ul>
          <div
            v-if="selectedCustomer"
            class="picked"
            data-testid="mjn-customer-picked"
          >
            <i class="pi pi-check-circle" />
            <span>{{ selectedCustomer.name }}</span>
            <Button
              icon="pi pi-times"
              text
              size="small"
              aria-label="Clear customer"
              v-tooltip="'Clear customer'"
              @click="clearCustomer"
            />
          </div>
          <p v-else-if="!customerSearch" class="muted hint">
            Leave blank to create a job with no customer attached.
          </p>
        </div>

        <div v-else class="form-stack">
          <FormField
            v-model="newCust.name"
            label="Name"
            required
            autocomplete="off"
            data-testid="mjn-newcust-name"
          />
          <div class="form-row">
            <!-- Phone stays raw: FormField has no inputmode pass-through and
                 losing inputmode="tel" would cost techs the phone keypad. -->
            <div class="form-field">
              <label>Phone</label>
              <InputText
                v-model="newCust.phone"
                class="w-full"
                inputmode="tel"
                data-testid="mjn-newcust-phone"
              />
            </div>
            <FormField
              v-model="newCust.email"
              label="Email"
              type="email"
              data-testid="mjn-newcust-email"
            />
          </div>
          <FormField
            v-model="newCust.address"
            label="Address"
            data-testid="mjn-newcust-address"
          />
        </div>
        <!--
          Sprint customer-multi-location (2026-05-21) — only render at
          2+ sites. Defaults to primary; tech can switch by tapping a row.
        -->
        <div
          v-if="!newCustomer && customerLocations.length > 1"
          class="loc-picker"
          data-testid="mjn-location-picker"
        >
          <div class="loc-picker-head">Which site?</div>
          <ul class="loc-list">
            <li
              v-for="loc in customerLocations"
              :key="loc.id"
              class="loc-item"
              :class="{ active: String(job.location_id) === String(loc.id) || (!job.location_id && loc.is_primary) }"
              data-testid="mjn-location-option"
              @click="job.location_id = String(loc.id)"
            >
              <strong>{{ loc.label || '(unlabeled)' }}</strong>
              <span v-if="loc.address" class="muted"> · {{ loc.address }}</span>
              <span v-if="loc.is_primary" class="badge-primary">primary</span>
            </li>
          </ul>
        </div>
      </section>

      <!-- Section: Job basics -->
      <section class="section">
        <header class="section-head"><h3>Job</h3></header>
        <div class="form-field">
          <label>Title *</label>
          <InputText
            ref="titleInput"
            v-model="job.title"
            class="w-full"
            placeholder="e.g. Replace broken springs"
            data-testid="mjn-job-title"
            autocomplete="off"
          />
        </div>
        <div class="form-field">
          <label>Description</label>
          <Textarea
            v-model="job.description"
            rows="2"
            autoResize
            class="w-full"
            data-testid="mjn-job-description"
          />
        </div>
        <div class="form-field">
          <label>Estimated time (hours)</label>
          <InputText
            v-model="job.scheduled_duration_hours"
            class="w-full"
            type="number"
            step="0.25"
            min="0"
            placeholder="e.g. 1.5"
            data-testid="mjn-job-duration-hours"
          />
          <small class="muted">Optional. Helps dispatch plan the day. Leave blank if unsure.</small>
        </div>
        <p class="muted hint">
          Saved as a Service Call. Dispatch will schedule and assign it.
        </p>
      </section>

      <!-- Section: Parts (only for users who can write inventory). -->
      <section v-if="canAddParts" class="section">
        <header class="section-head">
          <h3>Parts <span class="muted">(optional)</span></h3>
          <Button
            type="button"
            label="Add part"
            icon="pi pi-plus"
            size="small"
            severity="secondary"
            text
            data-testid="mjn-add-part"
            @click="addPartRow"
          />
        </header>
        <ul v-if="parts.length" class="parts-list" data-testid="mjn-parts-list">
          <li v-for="(p, idx) in parts" :key="idx" class="part-row">
            <div class="part-row-main">
              <InputText
                v-model="p.part_name"
                placeholder="Part name or SKU"
                class="w-full"
                :data-testid="`mjn-part-name-${idx}`"
                autocomplete="off"
                @input="onPartNameInput(p)"
              />
              <ul v-if="p.suggestions.length" class="suggest-list inline">
                <li
                  v-for="s in p.suggestions"
                  :key="`${s.source}-${s.sku}`"
                  class="suggest-item"
                  :data-testid="`mjn-part-suggestion-${idx}`"
                  @click="pickSuggestion(p, s)"
                >
                  <strong>{{ s.sku }}</strong>
                  <span class="muted"> · {{ s.name }}</span>
                  <span v-if="s.qty_on_hand != null" class="qty-pill">
                    {{ s.qty_on_hand }} on hand
                  </span>
                </li>
              </ul>
            </div>
            <input
              v-model.number="p.quantity"
              type="number"
              min="1"
              max="999"
              class="qty-input"
              :data-testid="`mjn-part-qty-${idx}`"
              aria-label="Quantity"
            />
            <Button
              icon="pi pi-times"
              v-tooltip="'Remove part'"
              aria-label="Remove part"
              text
              severity="danger"
              size="small"
              :data-testid="`mjn-part-remove-${idx}`"
              @click="removePartRow(idx)"
            />
          </li>
        </ul>
        <p v-else class="muted hint">No parts yet. Add what you need.</p>
      </section>
    </form>

    <template #footer>
      <Button label="Cancel" text severity="secondary" data-testid="mjn-cancel" @click="requestCancel" />
      <Button
        label="Create job"
        icon="pi pi-check"
        :disabled="!canSubmit"
        :loading="saving"
        data-testid="mjn-submit"
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
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 500;
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.6rem;
}
@media (max-width: 480px) {
  .form-row {
    grid-template-columns: 1fr;
  }
}

.section {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.65rem;
  padding: 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  background: var(--p-content-background, #fff);
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

.toggle-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  cursor: pointer;
  font-size: 0.85rem;
}

.muted {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.85rem;
  font-weight: 400;
}
.hint { margin: 0; }

.customer-search {
  position: relative;
}
.suggest-list {
  list-style: none;
  margin: 0.35rem 0 0 0;
  padding: 0;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  background: var(--p-content-background, #fff);
  max-height: 220px;
  overflow-y: auto;
}
.suggest-list.inline {
  margin-top: 0.25rem;
}
.suggest-item {
  padding: 0.65rem 0.75rem;
  cursor: pointer;
  border-bottom: 1px solid var(--p-content-border-color, #f3f4f6);
  min-height: 44px;
  display: flex;
  align-items: center;
  gap: 0.35rem;
}
.suggest-item:last-child { border-bottom: 0; }
.suggest-item:hover, .suggest-item:active {
  background: var(--p-highlight-background, #f3f4f6);
}

.picked {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.5rem 0.65rem;
  margin-top: 0.4rem;
  background: var(--p-green-50);
  color: var(--p-green-700);
  border-radius: 0.45rem;
  font-size: 0.9rem;
}
.picked > span { flex: 1; }

.loc-picker {
  margin-top: 0.6rem;
  padding: 0.5rem 0;
  border-top: 1px dashed var(--p-content-border-color, #e5e7eb);
}
.loc-picker-head {
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
  margin-bottom: 0.4rem;
}
.loc-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.loc-item {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.55rem 0.65rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.45rem;
  font-size: 0.9rem;
  min-height: 44px;
  cursor: pointer;
}
.loc-item.active {
  background: var(--p-primary-50, #eef2ff);
  border-color: var(--p-primary-300, #a5b4fc);
}
.loc-item .badge-primary {
  margin-left: auto;
  font-size: 0.7rem;
  padding: 0.15rem 0.4rem;
  background: var(--p-green-100, #dcfce7);
  color: var(--p-green-800, #166534);
  border-radius: 0.3rem;
}

.datetime-input {
  width: 100%;
  padding: 0.65rem 0.75rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  font: inherit;
  background: var(--p-content-background, #fff);
  color: inherit;
  min-height: 44px;
}

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
.part-row-main {
  display: flex;
  flex-direction: column;
}
.qty-input {
  width: 100%;
  padding: 0.6rem 0.5rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  text-align: center;
  font: inherit;
  background: var(--p-content-background, #fff);
  color: inherit;
  min-height: 44px;
}
.qty-pill {
  margin-left: auto;
  background: var(--p-highlight-background, #eef2ff);
  color: var(--p-primary-color, #4338ca);
  border-radius: 999px;
  padding: 0.05rem 0.45rem;
  font-size: 0.7rem;
  font-weight: 600;
}
</style>

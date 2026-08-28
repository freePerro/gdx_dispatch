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
import { DEFAULT_JOB_TYPE, JOB_TYPE_OPTIONS } from '../constants/jobTypes'
import { ref, reactive, computed, watch, nextTick } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'
import ToggleSwitch from 'primevue/toggleswitch'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { useAuthStore } from '../stores/auth'
import { isTechnician } from '../constants/roles'
import { usePermission } from '../composables/usePermission'
import { useDirtyDialog } from '../composables/useDirtyDialog'
import { formatPhone } from '../composables/useFormatters'
import FormField from './FormField.vue'
import PhoneInput from './PhoneInput.vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  // Optional pre-picked customer (e.g. "New job" from MobileCustomerDetailView
  // — the tech is already looking at the customer; making them re-search the
  // same person was the "pain to search customers" complaint). Applied in the
  // open watcher BETWEEN _resetForm() and snapshot() so the dialog opens
  // pristine (see the audit note there).
  customer: { type: Object, default: null },
})
const emit = defineEmits(['update:visible', 'created'])

const api = useApi()
const toast = useToast()
const auth = useAuthStore()
const { hasPermission } = usePermission()

// A job created from the truck belongs to the tech who created it. The
// 2026-08-17 version made that a toggle (default ON, "log it for dispatch"
// one tap away); it was switched off twice and each time the tech was
// locked out of his own job. Decided 2026-08-28: no toggle — the server
// assigns technician-role creators on its own. isTech only picks the hint.
const isTech = computed(() => isTechnician(auth.role))

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
// Flipping between "existing" and "new" customer invalidates the jobsite
// ask: a picked location belongs to the OLD customer (the backend would 400
// "does not belong"), and a drafted address was typed for someone else
// (post-code audit PR 2 §3).
watch(newCustomer, () => {
  siteChoice.value = null
  newSite.address = ''
  newSite.label = ''
})
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
// True once a search round-trip has completed for the CURRENT query text —
// gates the "no matches → add as new customer" row so it can't flash during
// the debounce window before the first fetch has answered.
const searchDone = ref(false)

watch(customerSearch, (q) => {
  if (newCustomer.value) return
  // pickCustomer() writes the picked name back into the input; without this
  // guard that programmatic write re-triggered the search and the dropdown
  // popped back up over the picked chip 250ms after every pick.
  if (selectedCustomer.value && q === (selectedCustomer.value.name || '')) return
  if (_searchTimer) clearTimeout(_searchTimer)
  searchDone.value = false
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
      searchDone.value = true
    } catch {
      if (seq !== _customerSearchSeq) return
      customerOptions.value = []
      searchDone.value = true
    } finally {
      if (seq === _customerSearchSeq) customerSearching.value = false
    }
  }, 250)
})

// Zero-result dead end (2026-07-22): the old UI offered nothing when a
// search came up empty — the tech had to notice the small "Create new"
// toggle. This surfaces the escape hatch right where their eyes are.
const showNoResultsAdd = computed(() =>
  !newCustomer.value
  && !selectedCustomer.value
  && searchDone.value
  && !customerSearching.value
  && customerSearch.value.trim().length >= 2
  && customerOptions.value.length === 0
)

function startNewCustomerFromSearch() {
  const q = customerSearch.value.trim()
  newCustomer.value = true
  const digits = q.replace(/\D/g, '')
  // A digits-ish query is a phone number off caller ID, not a name.
  if (digits.length >= 7 && /^[\d\s().+-]+$/.test(q)) {
    newCust.phone = digits
  } else {
    newCust.name = q
  }
}

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
  // Plan §9 / §14 Gap 1: without a picker every phone-created job took the
  // backend default — so a tech creating an INSTALL from the truck had no way
  // to say so, and under the §8 two-lane pricing that install would bill
  // hourly instead of its flat price. Defaults to the canonical service
  // spelling (most field-created jobs are service calls).
  job_type: DEFAULT_JOB_TYPE,
  // Sprint dispatch-capacity (2026-05-20) — scheduler's expected hours
  // (decimal, e.g. 1.5). Optional; dispatch falls back to the estimate
  // calc, then to "?h" if nothing's known.
  scheduled_duration_hours: null,
})

// ─── Jobsite ask (PR 2, jobsite-address plan) ────────────────────────
// null = same as customer address (ships location_id: null — the existing
// "customer's primary" semantics); a location id string; or NEW_SITE for
// the inline different-address form.
const NEW_SITE = '__new__'
const siteChoice = ref(null)
const newSite = reactive({ address: '', label: '' })
// Retry-safety memos (pre-code audit §3): a job-POST failure leaves the
// dialog open; Save again must REUSE what already succeeded, never mint a
// duplicate customer or location row. Keyed on the exact user input so an
// edited form correctly creates fresh rows.
const createdCustomerMemo = ref(null) // { key, id }
const createdSiteMemo = ref(null)     // { key, id }
function _custKey() {
  return [newCust.name, newCust.phone, newCust.email, newCust.address]
    .map((v) => (v || '').trim().toLowerCase()).join('|')
}
function _siteKey(customerId) {
  return [customerId, (newSite.address || '').trim().toLowerCase(),
    (newSite.label || '').trim().toLowerCase()].join('|')
}

// Sprint customer-multi-location — locations for the picked customer.
// Re-fetched whenever selectedCustomer changes.
const customerLocations = ref([])
watch(selectedCustomer, async (c) => {
  siteChoice.value = null
  newSite.address = ''
  newSite.label = ''
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
  // "Different address…" without an address is a job nobody can find.
  if (siteChoice.value === NEW_SITE && !newSite.address.trim()) return false
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

    // Step 1 — create customer if requested. Memoized across attempts: a
    // later step failing leaves the dialog open, and Save again must reuse
    // the row that already exists, not mint a duplicate (audit §3).
    if (newCustomer.value) {
      if (createdCustomerMemo.value?.key === _custKey()) {
        customerId = createdCustomerMemo.value.id
      } else {
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
          createdCustomerMemo.value = { key: _custKey(), id: customerId }
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
    }

    // Step 1b — the jobsite. A different-address job binds a REAL
    // customer_locations row (the endpoint audit-logs the create;
    // invariant #1). Failure BLOCKS the job: creating it anyway would
    // point the tech at the customer's address — the exact error this
    // feature exists to prevent. is_primary is ALWAYS false: true would
    // retroactively re-address every null-location job for the customer
    // (audit §5). Memoized like the customer for retry-safety.
    let siteLocationId = siteChoice.value === NEW_SITE ? null : (siteChoice.value || null)
    if (siteChoice.value === NEW_SITE) {
      if (!customerId) {
        toast.add({
          severity: 'warn',
          summary: 'Pick a customer first',
          detail: 'A jobsite address needs a customer to belong to.',
          life: 4000,
        })
        return
      }
      if (createdSiteMemo.value?.key === _siteKey(customerId)) {
        siteLocationId = createdSiteMemo.value.id
      } else {
        try {
          const loc = await api.post(`/api/customers/${customerId}/locations`, {
            label: newSite.label.trim() || null,
            address: newSite.address.trim(),
            is_primary: false,
          })
          siteLocationId = loc?.id || null
          if (!siteLocationId) throw new Error('Location creation returned no id')
          createdSiteMemo.value = { key: _siteKey(customerId), id: siteLocationId }
        } catch (e) {
          toast.add({
            severity: 'error',
            summary: 'Could not save the jobsite address',
            detail: e?.message || 'The job was NOT created — fix the address and try again.',
            life: 5000,
          })
          return
        }
      }
    }

    // Step 2 — create job. No scheduled_at — see comment on `job` state.
    let createdJob = null
    try {
      const jobPayload = {
        title: job.title.trim(),
        description: job.description.trim() || '',
        customer_id: customerId,
        job_type: job.job_type || DEFAULT_JOB_TYPE,
        scheduled_duration_hours:
          job.scheduled_duration_hours != null && job.scheduled_duration_hours !== ''
            ? Number(job.scheduled_duration_hours)
            : null,
        location_id: siteLocationId,
        // The server assigns a technician-role creator on its own now; this
        // flag is kept for the wire contract (and any non-tech caller that
        // wants it) and is never the thing that decides.
        assign_to_me: isTech.value,
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
      // Word the toast from what the server actually did, not the toggle:
      // assign_to_me quietly no-ops for callers with no technician record,
      // and "you can start it now" on a job the tech can't touch is exactly
      // the lie this feature exists to kill.
      const mine = Boolean(createdJob?.assigned_to)
      const partsNote = partsToSubmit.length
        ? `Added ${partsToSubmit.length} part${partsToSubmit.length === 1 ? '' : 's'}. `
        : ''
      toast.add({
        severity: 'success',
        summary: 'Job created',
        detail: partsNote + (mine
          ? "It's assigned to you — open it from your Jobs list to get started."
          : 'It stays in your Jobs list until dispatch assigns it.'),
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
  searchDone.value = false
  selectedCustomer.value = null
  newCust.name = ''
  newCust.phone = ''
  newCust.email = ''
  newCust.address = ''
  job.title = ''
  job.description = ''
  job.job_type = DEFAULT_JOB_TYPE
  job.scheduled_duration_hours = null
  siteChoice.value = null
  newSite.address = ''
  newSite.label = ''
  createdCustomerMemo.value = null
  createdSiteMemo.value = null
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
    // Jobsite ask — user-editable, so part of the pristine snapshot; the
    // retry memos are machine state and deliberately excluded.
    siteChoice: siteChoice.value,
    newSite: { ...newSite },
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
// ORDER MATTERS: the customer preseed must land BETWEEN _resetForm() and
// snapshot(). Earlier → _resetForm wipes it; later (e.g. a prop watcher) →
// the dialog is born dirty, which disables X/Esc and throws a phantom
// "Discard this new job?" at a tech who typed nothing (/audit 2026-07-22
// predicted exactly this bug).
watch(open, async (v) => {
  if (v) {
    _resetForm()
    if (props.customer?.id) {
      selectedCustomer.value = { ...props.customer }
      customerSearch.value = props.customer.name || ''
    }
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
              <span v-if="c.phone" class="muted"> · {{ formatPhone(c.phone) }}</span>
            </li>
          </ul>
          <button
            v-if="showNoResultsAdd"
            type="button"
            class="no-results-add"
            data-testid="mjn-no-results-add"
            @click="startNewCustomerFromSearch"
          >
            <i class="pi pi-user-plus" />
            <span>No matches — add “{{ customerSearch.trim() }}” as a new customer</span>
          </button>
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
            <!-- PhoneInput auto-formats to (111)222-3333; inputmode="tel"
                 falls through so techs still get the numeric phone keypad. -->
            <div class="form-field">
              <label>Phone</label>
              <PhoneInput
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
          The jobsite ask (PR 2, jobsite-address plan) — ONE always-visible
          select once a customer is in play, replacing the old 2+-locations-
          only picker. "Same as customer address" (null) is the default; a
          tech standing at a different address can say so right here instead
          of the job silently pointing at the HQ.
        -->
        <div
          v-if="selectedCustomer || newCustomer"
          class="loc-picker"
          data-testid="mjn-site-section"
        >
          <div class="loc-picker-head">Where's the job?</div>
          <ul class="loc-list">
            <li
              class="loc-item"
              :class="{ active: siteChoice === null }"
              data-testid="mjn-site-same"
              @click="siteChoice = null"
            >
              <strong>Same as customer address</strong>
              <span v-if="selectedCustomer?.address || newCust.address" class="muted">
                · {{ selectedCustomer?.address || newCust.address }}
              </span>
            </li>
            <li
              v-for="loc in (newCustomer ? [] : customerLocations)"
              :key="loc.id"
              class="loc-item"
              :class="{ active: String(siteChoice) === String(loc.id) }"
              data-testid="mjn-location-option"
              @click="siteChoice = String(loc.id)"
            >
              <strong>{{ loc.label || '(unlabeled)' }}</strong>
              <span v-if="loc.address" class="muted"> · {{ loc.address }}</span>
              <span v-if="loc.is_primary" class="badge-primary">primary</span>
            </li>
            <li
              class="loc-item"
              :class="{ active: siteChoice === NEW_SITE }"
              data-testid="mjn-site-new"
              @click="siteChoice = NEW_SITE"
            >
              <strong>Different address…</strong>
            </li>
          </ul>
          <div v-if="siteChoice === NEW_SITE" class="form-field">
            <label>Jobsite address *</label>
            <InputText
              v-model="newSite.address"
              placeholder="Street, city"
              data-testid="mjn-newsite-address"
            />
            <label>Label (optional)</label>
            <InputText
              v-model="newSite.label"
              placeholder="e.g. Warehouse, North yard"
              data-testid="mjn-newsite-label"
            />
          </div>
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
          <label>Job type</label>
          <Select
            v-model="job.job_type"
            :options="[...JOB_TYPE_OPTIONS]"
            class="w-full"
            data-testid="mjn-job-type"
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
        <!-- No toggle. A job created in the field is the creator's (Doug,
             2026-08-28): the opt-out shipped on 08-17 was switched off twice
             in eleven days and each time left the tech locked out of his own
             job. The server enforces this on the role; the hint just says so. -->
        <p class="muted hint" data-testid="mjn-dispatch-hint">
          <template v-if="isTech">
            Saved as a Service Call assigned to you — you can start it right away. Dispatch will schedule it.
          </template>
          <template v-else>
            Saved as a Service Call. Dispatch will schedule and assign it.
          </template>
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

.no-results-add {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  width: 100%;
  margin-top: 0.4rem;
  padding: 0.65rem 0.75rem;
  min-height: 44px;
  border: 1px dashed var(--p-primary-color);
  border-radius: 0.5rem;
  background: var(--p-highlight-background);
  color: var(--p-primary-color);
  font: inherit;
  font-size: 0.9rem;
  text-align: left;
  cursor: pointer;
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
  background: var(--p-highlight-background);
  border-color: var(--p-primary-color);
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

<script setup>
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useGpsBreadcrumb } from '@/composables/useGpsBreadcrumb'
import MobileQuoteBuilderDialog from '../components/MobileQuoteBuilderDialog.vue'
import MobileCustomerQuoteDialog from '../components/MobileCustomerQuoteDialog.vue'
import MobileInvoiceDialog from '../components/MobileInvoiceDialog.vue'
import MobileChatDialog from '../components/MobileChatDialog.vue'
import MobileJobCloseoutDialog from '../components/MobileJobCloseoutDialog.vue'
import Tag from 'primevue/tag'
import Button from 'primevue/button'
import Message from 'primevue/message'
import SelectButton from 'primevue/selectbutton'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Textarea from 'primevue/textarea'
import AutoComplete from 'primevue/autocomplete'
import Select from 'primevue/select'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import {
  markJobSeen as _markJobSeen,
  countUnseenForJob,
} from '../composables/usePartsSeenCutoff'
import {
  isPushSupported,
  getCurrentPermission,
  subscribeToPush,
} from '../composables/usePushSubscription'
import { useOfflineSync } from '../composables/useOfflineSync'
import { useMobileTour } from '../composables/useMobileTour'

// Sprint tech_mobile S1-A1 + A3 + A4 + A7 — today's route screen.
// Consumes GET /api/mobile/today; per-card actions hit existing
// /api/mobile/jobs/{id}/en-route + maps deep links.

const api = useApi()
const toast = useToast()

// Phase 3 (S3-A5) — offline state surface for the banner.
const { isOnline, pendingCount, syncing, syncNow } = useOfflineSync()

// Phase 4.5 — first-login tech tour. Auto-runs on first visit; user can
// replay via the "?" button in the header.
const { start: startTour } = useMobileTour()

const loading = ref(true)
const error = ref(null)
const refreshing = ref(false)
const jobs = ref([])
const tech = ref(null)
const date = ref(null)
const advancing = ref({})
const reorderMode = ref(false)
const reorderSaving = ref(false)
let originalOrder = []

// S1-A5 — list/map toggle.
const VIEW_LIST = 'list'
const VIEW_MAP = 'map'
const VIEW_OPTIONS = [
  { label: 'List', value: VIEW_LIST, icon: 'pi pi-list' },
  { label: 'Map', value: VIEW_MAP, icon: 'pi pi-map' },
]
const view = ref(VIEW_LIST)
const mapsApiKey = ref('')
const mapContainer = ref(null)
let googleMap = null
let mapMarkers = []
const mapReady = ref(false)
const mappableJobs = computed(() =>
  jobs.value.filter((j) => j.location && j.location.lat != null && j.location.lng != null),
)

async function load(silent = false) {
  if (!silent) loading.value = true
  refreshing.value = silent
  error.value = null
  try {
    const data = await api.get('/api/mobile/today')
    jobs.value = data.jobs || []
    tech.value = data.tech_id
    date.value = data.date
  } catch (err) {
    error.value = err?.message || "Couldn't load today's route"
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

function formatTime(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  } catch (e) {
    return ''
  }
}

function formatDriveTime(seconds) {
  if (seconds === null || seconds === undefined) return null
  const total = Math.round(seconds / 60)
  if (total < 1) return '<1 min'
  if (total < 60) return `${total} min`
  const h = Math.floor(total / 60)
  const m = total % 60
  return m > 0 ? `${h}h ${m}m` : `${h}h`
}

function statusSeverity(status) {
  switch (status) {
    case 'en_route':
      return 'warn'
    case 'on_site':
      return 'info'
    case 'done':
      return 'success'
    default:
      return 'secondary'
  }
}

// Status pill icon + label (paired so the meaning survives sunlight glare
// and red/green colorblindness — never color alone, always icon + text).
function statusIcon(s) {
  return ({
    en_route: 'pi pi-send',
    on_site: 'pi pi-map-marker',
    done: 'pi pi-check',
    unassigned: 'pi pi-circle',
    assigned: 'pi pi-circle-fill',
  })[s] || 'pi pi-circle-fill'
}
function statusLabel(s) {
  return ({
    en_route: 'En route',
    on_site: 'On site',
    done: 'Done',
    unassigned: 'Unassigned',
    assigned: 'Assigned',
  })[s] || s || 'Assigned'
}

function prioritySeverity(priority) {
  const p = (priority || '').toLowerCase()
  if (p === 'urgent') return 'danger'
  if (p === 'high') return 'warn'
  return 'secondary'
}

// S1-A3 — open in user's preferred maps app.
function openMaps(job) {
  if (job.navigation_link) {
    window.open(job.navigation_link, '_blank', 'noopener')
  }
}

// S1-A6 — reorder helpers.
function enterReorderMode() {
  originalOrder = jobs.value.map((j) => j.appointment_id)
  reorderMode.value = true
}

function cancelReorder() {
  // Restore the order we captured on entry.
  if (originalOrder.length === jobs.value.length) {
    const byId = new Map(jobs.value.map((j) => [j.appointment_id, j]))
    jobs.value = originalOrder.map((id) => byId.get(id)).filter(Boolean)
  }
  reorderMode.value = false
}

function moveJob(idx, delta) {
  const next = idx + delta
  if (next < 0 || next >= jobs.value.length) return
  const list = [...jobs.value]
  ;[list[idx], list[next]] = [list[next], list[idx]]
  jobs.value = list
}

async function saveReorder() {
  const ids = jobs.value.map((j) => j.appointment_id).filter(Boolean)
  if (ids.length !== jobs.value.length) {
    toast.add({
      severity: 'warn',
      summary: 'Cannot reorder',
      detail: 'Some stops have no appointment record.',
      life: 4000,
    })
    return
  }
  reorderSaving.value = true
  try {
    const result = await api.post('/api/mobile/today/reorder', { appointment_ids: ids })
    if (result?.changed) {
      toast.add({ severity: 'success', summary: 'Route reordered', life: 2500 })
      await load(true)
    } else {
      toast.add({ severity: 'info', summary: 'No changes', life: 2000 })
    }
    reorderMode.value = false
  } catch (err) {
    const detail = err?.message || 'Could not save'
    toast.add({
      severity: 'error',
      summary: 'Reorder failed',
      detail,
      life: 5000,
    })
  } finally {
    reorderSaving.value = false
  }
}

// S1-A4 — "On my way" → flips dispatch_status to en_route.
async function onMyWay(job) {
  advancing.value = { ...advancing.value, [job.id]: true }
  try {
    await api.post(`/api/mobile/jobs/${job.id}/en-route`, {})
    job.dispatch_status = 'en_route'
    toast.add({
      severity: 'success',
      summary: 'On my way',
      detail: `Dispatch notified — ${job.customer?.name || 'job'}`,
      life: 2500,
    })
    // Refresh quietly to pick up any state dispatch changed.
    load(true)
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Could not update',
      detail: err?.message || 'Unknown error',
      life: 4000,
    })
  } finally {
    advancing.value = { ...advancing.value, [job.id]: false }
  }
}

// Phase 1.2 B1 — "I'm here" → flips dispatch_status to on_site,
// stamps Job + JobAssignment.arrived_at, auto-clocks-in the tech.
// Sends optional geo if the browser will give it (best-effort, never block).
async function imHere(job) {
  advancing.value = { ...advancing.value, [job.id]: true }
  let geo = {}
  try {
    if (navigator.geolocation) {
      geo = await new Promise((resolve) => {
        navigator.geolocation.getCurrentPosition(
          (pos) => resolve({
            lat: pos.coords.latitude,
            lng: pos.coords.longitude,
            accuracy: pos.coords.accuracy,
          }),
          () => resolve({}),
          { timeout: 3000, maximumAge: 60000 },
        )
      })
    }
  } catch { /* swallow */ }
  try {
    await api.post(`/api/mobile/jobs/${job.id}/arrived`, geo)
    job.dispatch_status = 'on_site'
    toast.add({
      severity: 'success',
      summary: "I'm here",
      detail: `Clocked in at ${job.customer?.name || 'job'}`,
      life: 2500,
    })
    load(true)
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Could not check in',
      detail: err?.message || 'Unknown error',
      life: 4000,
    })
  } finally {
    advancing.value = { ...advancing.value, [job.id]: false }
  }
}

// Phase 2 / C3.5 (Doug 2026-05-10) — replaced the inline sigDialog +
// /api/mobile/jobs/{id}/complete flow with the unified
// MobileJobCloseoutDialog. The new dialog collects parts + hours +
// signature + notes and POSTs /api/jobs/{id}/closeout — same path
// dispatchers use, single transaction, JobCloseout snapshot row
// written. The legacy /api/mobile/jobs/{id}/complete route stays alive
// for backwards compatibility but isn't reached from this view anymore.
//
// Why unify: techs were filling a thin sig-only form that didn't
// capture parts or labor; office had to chase those down later.
// One closeout path, one rule: capture-or-default at submit time.
const closeoutOpen = ref(false)
const closeoutJob = ref(null)

function openComplete(job) {
  closeoutJob.value = job
  closeoutOpen.value = true
}

function onCloseoutDone() {
  // Dialog already fired the success toast. Mark the job done locally
  // so the card shows "done" immediately, then refresh.
  if (closeoutJob.value) {
    closeoutJob.value.dispatch_status = 'done'
  }
  closeoutOpen.value = false
}

// Refetch when the dialog closes for any reason (submit OR cancel).
// On submit: ensures the local job list reflects the new lifecycle.
// On cancel: harmless re-render (no state changed server-side).
watch(closeoutOpen, async (v) => {
  if (!v) {
    closeoutJob.value = null
    load(true)
  }
})

const emptyState = computed(() => !loading.value && jobs.value.length === 0)

// Phase 1.4 D1+D2 — multi-tech card decoration. Resolve the calling
// tech's row from the assignments array (it's the row whose user_id
// matches the JWT sub) so we can list ONLY the OTHER assignees.
function _myTechId() {
  try {
    const u = JSON.parse(sessionStorage.getItem('gdx_user') || 'null')
    return u?.id || null
  } catch {
    return null
  }
}
function otherTechs(job) {
  const list = Array.isArray(job?.assignments) ? job.assignments : []
  if (list.length <= 1) return []
  const myId = _myTechId()
  // assignment.tech_id is technicians.id; sessionStorage user.id is users.id.
  // The two don't match directly, so we treat any tech whose state-machine
  // stamps differ from the page's most-recent activity as "the other tech."
  // Simpler rule: if there are >1 assignments, every row except the lead
  // (or the row we currently see actively progressing) is "other."
  // Practical approach: list everyone EXCEPT the assignment whose
  // completed_at OR arrived_at OR en_route_at is the most recent — that's
  // most likely "us". This is best-effort UX, not a security boundary.
  const ranked = [...list].sort((a, b) => {
    const ax = a.completed_at || a.arrived_at || a.en_route_at || ''
    const bx = b.completed_at || b.arrived_at || b.en_route_at || ''
    return bx.localeCompare(ax)
  })
  // If our row matched myId via user_id later, prefer that — for now,
  // assume the "self" row is the one with the most-recent stamp.
  const selfId = ranked[0]?.tech_id
  return list.filter((r) => r.tech_id !== selfId)
}
function _stateOf(a) {
  if (a.completed_at) return 'done'
  if (a.arrived_at) return 'on_site'
  if (a.en_route_at) return 'en_route'
  return 'assigned'
}
function multiTechLabel(job) {
  const others = otherTechs(job)
  if (others.length === 0) return ''
  const lead = others.find((o) => o.is_lead)
  const subj = others.length === 1 ? (others[0].tech_name || 'another tech') : `${others.length} others`
  if (lead && others.length === 1) return `Lead: ${lead.tech_name || 'lead tech'}`
  return `with ${subj}`
}
function multiTechTooltip(job) {
  const others = otherTechs(job)
  return others
    .map((o) => `${o.tech_name || 'tech'}${o.is_lead ? ' (lead)' : ''} — ${_stateOf(o)}`)
    .join('\n')
}

// ── Phase 1.3 parts (C1-C3, C6, C7) ──────────────────────────────────

const expandedJobId = ref(null)
const partsByJob = ref({})         // job_id -> array of parts
const partsLoading = ref({})        // job_id -> bool

// ── Install / equipment at the site (read-only) ──────────────────────
// Surfaces the customer's installed equipment (door + opener specs) on the
// job card so a tech on an install/service call sees the unit details. Reuses
// GET /api/customers/{id}/equipment (gated on the equipment_tracking module).
const equipExpandedJobId = ref(null)
const equipByCustomer = ref({})     // customer_id -> array of equipment
const equipLoading = ref({})        // customer_id -> bool
const partsModalOpen = ref(false)
const partsModalJobId = ref(null)
const partsModalEditingId = ref(null)  // when set, modal is in "edit" mode
const skuSuggestions = ref([])
const skuLoading = ref(false)
const URGENCY_OPTIONS = [
  { label: 'Normal', value: 'normal' },
  { label: 'Urgent', value: 'urgent' },
  { label: 'Critical', value: 'critical' },
]
const blankPartsForm = () => ({
  sku: null,
  part_name: '',
  quantity: 1,
  supplier: '',
  urgency: 'normal',
  notes: '',
  photo_url: null,
})
const partsForm = ref(blankPartsForm())

// ── Phase 2.1 + 2.2 — quoting & invoicing (S2-A* / S2-B*) ──────────
// Per-job quote state (loaded lazily when the job-card actions render).
//   quoteByJob[job.id] = { quotes: [...], lastLoaded: ts }
// has_accepted_quote / latest_quote derived from this.
const quoteByJob = ref({})

const quoteBuilderOpen = ref(false)
const quoteBuilderJob = ref(null)

const customerQuoteOpen = ref(false)
const customerQuote = ref(null)

const invoiceDialogOpen = ref(false)
const invoiceDialogJob = ref(null)

async function ensureJobQuotesLoaded(job) {
  if (!job?.id) return
  if (quoteByJob.value[job.id]) return
  try {
    const data = await api.get(`/api/mobile/jobs/${job.id}/quote`)
    quoteByJob.value[job.id] = { quotes: data.quotes || [] }
  } catch (e) {
    quoteByJob.value[job.id] = { quotes: [], error: e.message }
  }
}
function jobQuotes(job) {
  return quoteByJob.value[job?.id]?.quotes || []
}
function jobAcceptedQuote(job) {
  return jobQuotes(job).find(q => q.status === 'accepted') || null
}
function jobLatestActiveQuote(job) {
  // Active = not declined.
  return jobQuotes(job).find(q => q.status !== 'declined') || null
}

function openQuoteBuilder(job) {
  quoteBuilderJob.value = job
  quoteBuilderOpen.value = true
}
function onQuoteBuilt(quote) {
  if (quoteBuilderJob.value?.id) {
    const list = jobQuotes(quoteBuilderJob.value)
    list.unshift(quote)
    quoteByJob.value[quoteBuilderJob.value.id] = { quotes: list }
  }
}
function presentQuote(quote) {
  customerQuote.value = quote
  customerQuoteOpen.value = true
}
function onQuoteAccepted(updated) {
  // Patch the in-memory list with the new status.
  for (const jid of Object.keys(quoteByJob.value)) {
    const list = quoteByJob.value[jid].quotes || []
    const i = list.findIndex(q => q.id === updated.id)
    if (i >= 0) list[i] = { ...list[i], ...updated }
  }
  toast.add({ severity: 'success', summary: 'Customer accepted', life: 2500 })
}
function onQuoteDeclined(updated) {
  for (const jid of Object.keys(quoteByJob.value)) {
    const list = quoteByJob.value[jid].quotes || []
    const i = list.findIndex(q => q.id === updated.id)
    if (i >= 0) list[i] = { ...list[i], ...updated }
  }
}
function openInvoice(job) {
  invoiceDialogJob.value = job
  invoiceDialogOpen.value = true
}
function onInvoiced(_inv) {
  // No-op for now; the dialog re-loads its own summary.
}

// Phase 4.1 — per-job dispatch chat.
const chatDialogOpen = ref(false)
const chatDialogJob = ref(null)
function openChat(job) {
  chatDialogJob.value = job
  chatDialogOpen.value = true
}
const partsSubmitting = ref(false)

function summaryLabel(s) {
  if (!s || !s.total) return null
  const parts = []
  if (s.needed) parts.push(`${s.needed} needed`)
  if (s.ordered) parts.push(`${s.ordered} ordered`)
  if (s.received) parts.push(`${s.received} received`)
  return `${s.total} part${s.total === 1 ? '' : 's'} (${parts.join(', ')})`
}

// Phase 1.3 C4 (in-app fallback) — surface dispatch status changes
// (ordered/received) since the tech's last view. Push lands in Sprint 1.5;
// until then we badge the parts pill and toast a one-line summary on
// first load so the tech doesn't miss "your spring arrived."
const partsUnseenByJob = ref({})  // job_id -> count of newly-actioned parts

// Phase 1.5 E2 — "Enable notifications" CTA. Shown when:
//   * the browser supports push (no point asking otherwise), AND
//   * permission is still 'default' (we never re-ask after deny;
//     browser hides the prompt anyway after a deny click).
const pushCta = ref({ visible: false, working: false })
const pushHidden = ref(false)
// Dismiss survives reloads and navigation for 30 days. Avoids re-prompting
// every page transition on `/mobile`. Re-prompt allowed after TTL so a tech
// who changes their mind doesn't need devtools.
const PUSH_CTA_DISMISS_KEY = 'gdx.mobile.push_cta_dismissed_at'
const PUSH_CTA_DISMISS_TTL_MS = 30 * 24 * 60 * 60 * 1000
function _readPushDismissed() {
  try {
    const raw = window.localStorage.getItem(PUSH_CTA_DISMISS_KEY)
    if (!raw) return false
    const ts = Number(raw)
    if (!Number.isFinite(ts)) return false
    return Date.now() - ts < PUSH_CTA_DISMISS_TTL_MS
  } catch (e) {
    return false
  }
}
function _writePushDismissed() {
  try {
    window.localStorage.setItem(PUSH_CTA_DISMISS_KEY, String(Date.now()))
  } catch (e) {
    /* private mode / quota — fall back to in-memory only */
  }
}
function refreshPushCta() {
  if (pushHidden.value || _readPushDismissed()) {
    pushHidden.value = true
    pushCta.value = { visible: false, working: false }
    return
  }
  const supported = isPushSupported()
  const perm = getCurrentPermission()
  pushCta.value = {
    visible: supported && perm === 'default',
    working: false,
  }
}
async function enablePush() {
  pushCta.value.working = true
  try {
    const r = await subscribeToPush(api)
    if (r.ok) {
      toast.add({
        severity: 'success',
        summary: 'Notifications on',
        detail: 'Dispatch can now ping you about parts and urgent jobs.',
        life: 3000,
      })
      pushHidden.value = true
    } else if (r.reason === 'permission_denied') {
      toast.add({
        severity: 'info',
        summary: 'Notifications declined',
        detail: 'You can re-enable them in your browser settings.',
        life: 4000,
      })
      pushHidden.value = true
    } else {
      toast.add({
        severity: 'warn',
        summary: 'Could not enable',
        detail: `Push setup failed (${r.reason}).`,
        life: 4000,
      })
    }
  } finally {
    refreshPushCta()
  }
}
function dismissPushCta() {
  pushHidden.value = true
  _writePushDismissed()
  refreshPushCta()
}

function markJobSeen(jobId) {
  _markJobSeen(jobId)
  partsUnseenByJob.value = { ...partsUnseenByJob.value, [jobId]: 0 }
}

function recomputeUnseen(jobId, partsList) {
  return countUnseenForJob(jobId, partsList)
}

async function toggleParts(job) {
  if (expandedJobId.value === job.id) {
    expandedJobId.value = null
    return
  }
  expandedJobId.value = job.id
  if (!partsByJob.value[job.id]) {
    await loadParts(job.id)
  }
  markJobSeen(job.id)
}

async function loadParts(jobId) {
  partsLoading.value = { ...partsLoading.value, [jobId]: true }
  try {
    const r = await api.get(`/api/jobs/${jobId}/parts-needed`)
    const list = Array.isArray(r) ? r : []
    partsByJob.value = { ...partsByJob.value, [jobId]: list }
    partsUnseenByJob.value = {
      ...partsUnseenByJob.value,
      [jobId]: recomputeUnseen(jobId, list),
    }
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Could not load parts',
      detail: err?.message || 'Unknown error',
      life: 4000,
    })
  } finally {
    partsLoading.value = { ...partsLoading.value, [jobId]: false }
  }
}

const EQUIP_TYPE_LABELS = {
  garage_door: 'Garage door',
  opener: 'Opener',
  gate: 'Gate',
  other: 'Equipment',
}
function equipTypeLabel(t) {
  return EQUIP_TYPE_LABELS[t] || 'Equipment'
}
function equipTitle(e) {
  const parts = [e.manufacturer, e.model].filter(Boolean).join(' ')
  return parts || equipTypeLabel(e.equipment_type)
}

async function toggleEquipment(job) {
  const cid = job.customer?.id
  if (!cid) return
  if (equipExpandedJobId.value === job.id) {
    equipExpandedJobId.value = null
    return
  }
  equipExpandedJobId.value = job.id
  if (!equipByCustomer.value[cid]) {
    await loadEquipment(cid)
  }
}

async function loadEquipment(customerId) {
  equipLoading.value = { ...equipLoading.value, [customerId]: true }
  try {
    const r = await api.get(`/api/customers/${customerId}/equipment`)
    const list = Array.isArray(r) ? r : r?.items || r?.data || []
    equipByCustomer.value = { ...equipByCustomer.value, [customerId]: list }
  } catch {
    // Equipment tracking is an optional module — fail quietly (show "none").
    equipByCustomer.value = { ...equipByCustomer.value, [customerId]: [] }
  } finally {
    equipLoading.value = { ...equipLoading.value, [customerId]: false }
  }
}

async function refreshAllUnseenCounts() {
  // C4 fallback: when the today's-route loads, walk every job that has
  // recorded parts and recompute its unseen-update badge. Cheap because
  // parts_summary already tells us which jobs to bother fetching.
  const targets = jobs.value.filter((j) => (j.parts_summary?.total || 0) > 0)
  await Promise.all(targets.map((j) => loadParts(j.id)))
  let total = 0
  for (const j of targets) {
    total += partsUnseenByJob.value[j.id] || 0
  }
  if (total > 0) {
    toast.add({
      severity: 'info',
      summary: total === 1 ? 'Dispatch updated 1 part' : `Dispatch updated ${total} parts`,
      detail: 'Tap the parts row on a job to see what changed.',
      life: 5000,
    })
  }
}

function openRequestParts(job) {
  partsModalJobId.value = job.id
  partsModalEditingId.value = null
  partsForm.value = blankPartsForm()
  skuSuggestions.value = []
  partsModalOpen.value = true
}

function openEditPart(job, part) {
  partsModalJobId.value = job.id
  partsModalEditingId.value = part.id
  // Always feed the AutoComplete a string so the prefill is visible —
  // the user can clear it and pick a catalog hit, in which case the
  // payload helper coerces back to {sku, name}. If we passed an object
  // here the AutoComplete would render the object's optionLabel only
  // when it matches a current suggestion, leaving the field looking
  // empty.
  partsForm.value = {
    sku: part.sku || '',
    part_name: part.part_name,
    quantity: part.quantity || 1,
    supplier: part.supplier || '',
    urgency: part.urgency || 'normal',
    notes: part.notes || '',
    photo_url: part.photo_url || null,
  }
  partsModalOpen.value = true
}

async function searchSku(event) {
  const q = (event?.query || '').trim()
  if (!q) {
    skuSuggestions.value = []
    return
  }
  skuLoading.value = true
  try {
    const r = await api.get(`/api/parts-needed/sku-suggest?q=${encodeURIComponent(q)}`)
    skuSuggestions.value = Array.isArray(r) ? r : []
  } catch {
    skuSuggestions.value = []
  } finally {
    skuLoading.value = false
  }
}

function pickSuggestion(s) {
  // Hydrate the rest of the form when the tech picks a catalog hit.
  if (!s || typeof s === 'string') return
  partsForm.value.part_name = s.name || partsForm.value.part_name
  if (!partsForm.value.supplier && s.vendor) {
    partsForm.value.supplier = s.vendor
  }
}

function partsFormPayload() {
  // SKU autocomplete returns an object when the tech picks a catalog
  // hit and a bare string when they typed something the catalog didn't
  // know about. Either way the typed value is meaningful — preserve it
  // as the literal SKU rather than throwing it away.
  const skuField = partsForm.value.sku
  let sku = null
  let typedName = ''
  if (typeof skuField === 'object' && skuField) {
    sku = skuField.sku || null
  } else if (typeof skuField === 'string') {
    const trimmed = skuField.trim()
    if (trimmed) {
      sku = trimmed
      typedName = trimmed
    }
  }
  const name = (partsForm.value.part_name || typedName).trim()
  return {
    sku: sku || null,
    part_name: name,
    quantity: Number(partsForm.value.quantity) || 1,
    supplier: partsForm.value.supplier || '',
    urgency: partsForm.value.urgency || 'normal',
    notes: partsForm.value.notes || '',
    photo_url: partsForm.value.photo_url || null,
  }
}

async function submitPartsForm() {
  const payload = partsFormPayload()
  if (!payload.part_name) {
    toast.add({ severity: 'warn', summary: 'Part name required', life: 3000 })
    return
  }
  partsSubmitting.value = true
  try {
    if (partsModalEditingId.value) {
      await api.patch(`/api/parts-needed/${partsModalEditingId.value}`, payload)
      toast.add({ severity: 'success', summary: 'Part updated', life: 2500 })
    } else {
      await api.post(`/api/jobs/${partsModalJobId.value}/parts-needed`, payload)
      toast.add({ severity: 'success', summary: 'Part requested', life: 2500 })
    }
    partsModalOpen.value = false
    await loadParts(partsModalJobId.value)
    await load(true)  // refresh parts_summary on the cards
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Could not save',
      detail: err?.message || 'Unknown error',
      life: 4000,
    })
  } finally {
    partsSubmitting.value = false
  }
}

function partStatusSeverity(s) {
  return s === 'received' ? 'success' : s === 'ordered' ? 'info' : 'warn'
}

function formatEta(iso) {
  if (!iso) return null
  try {
    return new Date(iso).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })
  } catch {
    return null
  }
}

// ── Map view (S1-A5) ─────────────────────────────────────────────────

function statusColor(status) {
  switch (status) {
    case 'en_route':
      return '#f59e0b'
    case 'on_site':
      return '#3b82f6'
    case 'done':
      return '#10b981'
    default:
      return '#6b7280'
  }
}

async function fetchMapsKey() {
  try {
    const r = await api.get('/api/settings/integrations/google-maps')
    mapsApiKey.value = r?.key || ''
  } catch (_err) {
    mapsApiKey.value = ''
  }
}

function ensureGoogleMapsScript() {
  return new Promise((resolve) => {
    if (window.google?.maps) {
      resolve(true)
      return
    }
    if (!mapsApiKey.value) {
      resolve(false)
      return
    }
    const existing = document.querySelector('script[data-gdx-gmaps]')
    if (existing) {
      existing.addEventListener('load', () => resolve(true), { once: true })
      return
    }
    const s = document.createElement('script')
    s.src = `https://maps.googleapis.com/maps/api/js?key=${mapsApiKey.value}`
    s.async = true
    s.dataset.tgdGmaps = '1'
    s.onload = () => resolve(true)
    s.onerror = () => resolve(false)
    document.head.appendChild(s)
  })
}

function clearMarkers() {
  mapMarkers.forEach((m) => m.setMap(null))
  mapMarkers = []
}

function renderMarkers() {
  if (!googleMap || !window.google?.maps) return
  clearMarkers()
  const bounds = new window.google.maps.LatLngBounds()
  mappableJobs.value.forEach((j, idx) => {
    const position = { lat: j.location.lat, lng: j.location.lng }
    const marker = new window.google.maps.Marker({
      position,
      map: googleMap,
      label: { text: String(idx + 1), color: '#fff', fontWeight: '600' },
      icon: {
        path: window.google.maps.SymbolPath.CIRCLE,
        scale: 14,
        fillColor: statusColor(j.dispatch_status),
        fillOpacity: 1,
        strokeColor: '#fff',
        strokeWeight: 2,
      },
      title: j.customer?.name || 'Stop',
    })
    mapMarkers.push(marker)
    bounds.extend(position)
  })
  if (mappableJobs.value.length > 0) {
    googleMap.fitBounds(bounds, 60)
    if (mappableJobs.value.length === 1) {
      googleMap.setZoom(14)
    }
  }
}

async function initMap() {
  if (!mapContainer.value || !window.google?.maps) return
  if (!googleMap) {
    googleMap = new window.google.maps.Map(mapContainer.value, {
      center: { lat: 39.8283, lng: -98.5795 }, // continental US center; bounds replace this immediately
      zoom: 4,
      mapTypeControl: false,
      streetViewControl: false,
      fullscreenControl: false,
    })
  }
  mapReady.value = true
  renderMarkers()
}

watch(view, async (next) => {
  if (next === VIEW_MAP) {
    await fetchMapsKey()
    if (!mapsApiKey.value) return
    const ok = await ensureGoogleMapsScript()
    if (!ok) return
    await nextTick()
    await initMap()
  }
})

watch(jobs, () => {
  if (view.value === VIEW_MAP && mapReady.value) renderMarkers()
})

// Sprint 5 / S5-C1 — start GPS breadcrumb sampling. Server enforces the
// "while clocked in" privacy boundary; sampler stops on 403.
const gps = useGpsBreadcrumb({ intervalMs: 30_000 })

onMounted(async () => {
  await load()
  // C4 fallback: surface dispatch updates the tech missed since last view.
  refreshAllUnseenCounts()
  // E2: gate the "Enable notifications" CTA on browser support + perm.
  refreshPushCta()
  // Phase 4.5 — fire the first-login tour after the page paints. nextTick
  // alone isn't enough because PrimeVue tags + buttons render lazily.
  setTimeout(() => {
    try { startTour('tech') } catch (e) { /* no DOM targets — skip */ }
  }, 400)
  try { gps.start() } catch (e) { /* geolocation perms denied is fine */ }
})

function replayTour() {
  startTour('tech', { force: true })
}
</script>

<template>
    <section class="today-route">
      <!-- Phase 3 (S3-A5) — offline banner. Sticky, dismissible only by
           reconnecting. Sub-text shows pending action count + sync state. -->
      <Transition name="slide-down">
        <div v-if="!isOnline || pendingCount > 0" class="offline-banner" :class="{ 'banner-online': isOnline }">
          <i :class="isOnline ? 'pi pi-cloud-upload' : 'pi pi-wifi'" />
          <div class="offline-banner-text">
            <strong v-if="!isOnline">You're offline</strong>
            <strong v-else-if="syncing">Syncing…</strong>
            <strong v-else>{{ pendingCount }} pending</strong>
            <div class="offline-sub">
              <template v-if="!isOnline && pendingCount > 0">
                {{ pendingCount }} action{{ pendingCount === 1 ? '' : 's' }} will sync when you're back.
              </template>
              <template v-else-if="!isOnline">
                Your work is being saved locally.
              </template>
              <template v-else>
                Sending queued changes to the server.
              </template>
            </div>
          </div>
          <Button
            v-if="isOnline && pendingCount > 0 && !syncing"
            label="Sync now"
            icon="pi pi-arrow-up"
            text
            size="small"
            @click="syncNow"
          />
          <i v-else-if="syncing" class="pi pi-spin pi-spinner" />
        </div>
      </Transition>

      <div v-if="pushCta.visible" class="push-cta">
        <i class="pi pi-bell" />
        <div class="push-cta-text">
          <strong>Get push alerts</strong>
          <div>Hear the chime when dispatch ships a part or flags a critical job.</div>
        </div>
        <Button
          label="Enable"
          size="small"
          :loading="pushCta.working"
          @click="enablePush"
        />
        <Button
          icon="pi pi-times"
          text
          rounded
          size="small"
          aria-label="Dismiss"
          @click="dismissPushCta"
        />
      </div>

      <header class="today-header">
        <div>
          <h1 class="today-heading">Today's Route</h1>
          <div v-if="date" class="today-sub">{{ date }} · {{ jobs.length }} stops</div>
        </div>
        <div class="today-actions">
          <template v-if="reorderMode">
            <Button
              label="Cancel"
              severity="secondary"
              text
              :disabled="reorderSaving"
              @click="cancelReorder"
            />
            <Button
              label="Save order"
              icon="pi pi-check"
              :loading="reorderSaving"
              @click="saveReorder"
            />
          </template>
          <template v-else>
            <SelectButton
              v-model="view"
              :options="VIEW_OPTIONS"
              optionLabel="label"
              optionValue="value"
              aria-label="View"
              :allowEmpty="false"
            />
            <Button
              v-if="view === VIEW_LIST && jobs.length > 1"
              icon="pi pi-sort-alt"
              text
              aria-label="Reorder"
              @click="enterReorderMode"
            />
            <Button
              icon="pi pi-question-circle"
              text
              aria-label="Replay tour"
              @click="replayTour"
            />
            <Button
              icon="pi pi-refresh"
              text
              :loading="refreshing"
              aria-label="Refresh"
              @click="load(true)"
            />
          </template>
        </div>
      </header>

      <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>

      <div v-if="loading && !refreshing" class="loading">Loading…</div>

      <div v-else-if="emptyState" class="empty">
        <i class="pi pi-calendar-times empty-icon" />
        <div class="empty-title">Nothing scheduled today</div>
        <div class="empty-help">Pull to refresh, or check with dispatch.</div>
      </div>

      <div v-else-if="view === VIEW_MAP" class="map-wrap">
        <div ref="mapContainer" class="map-container" />
        <div v-if="!mapsApiKey" class="map-placeholder">
          <i class="pi pi-info-circle" />
          Google Maps key not configured. Ask an admin to set one in
          Settings → Integrations → Google Maps.
        </div>
        <div v-else-if="mappableJobs.length === 0" class="map-placeholder">
          <i class="pi pi-map" />
          None of today's stops have map coordinates yet. Stops are mappable
          once dispatch geocodes the appointment.
        </div>
      </div>

      <ol v-else class="job-list">
        <template v-for="(job, idx) in jobs" :key="job.id">
        <div
          v-if="idx > 0 && formatDriveTime(jobs[idx - 1]?.drive_time_to_next_seconds)"
          class="leg-eta"
        >
          <i class="pi pi-car" />
          {{ formatDriveTime(jobs[idx - 1].drive_time_to_next_seconds) }} drive
        </div>
        <li class="job-card">
          <div class="job-row job-row-top">
            <div class="job-time">{{ formatTime(job.time_window?.start) }}</div>
            <div class="job-pills">
              <Tag
                v-if="job.priority && job.priority !== 'Normal'"
                :value="job.priority"
                :severity="prioritySeverity(job.priority)"
              />
              <span :class="['status-pill', `status-${(job.dispatch_status || 'assigned').replace(' ','_')}`]">
                <i :class="statusIcon(job.dispatch_status)" />
                {{ statusLabel(job.dispatch_status) }}
              </span>
            </div>
            <div v-if="reorderMode" class="reorder-controls">
              <Button
                icon="pi pi-arrow-up"
                text
                rounded
                size="small"
                :disabled="idx === 0"
                aria-label="Move up"
                @click="moveJob(idx, -1)"
              />
              <Button
                icon="pi pi-arrow-down"
                text
                rounded
                size="small"
                :disabled="idx === jobs.length - 1"
                aria-label="Move down"
                @click="moveJob(idx, 1)"
              />
            </div>
            <div v-else class="job-stop-num">#{{ idx + 1 }}</div>
          </div>

          <div class="job-customer">{{ job.customer?.name || '—' }}</div>

          <div
            v-if="otherTechs(job).length > 0"
            class="job-multitech"
            :title="multiTechTooltip(job)"
          >
            <i class="pi pi-users" />
            <span>{{ multiTechLabel(job) }}</span>
          </div>
          <div
            class="job-address"
            :class="{ 'job-address-missing': !job.customer?.address }"
            @click="openMaps(job)"
          >
            <i class="pi pi-map-marker" />
            {{ job.customer?.address || 'No address — ask dispatch' }}
          </div>
          <div class="job-service">{{ job.service_type }} · {{ job.title }}</div>

          <div v-if="job.alerts?.length" class="job-alerts">
            <Tag
              v-for="alert in job.alerts"
              :key="alert"
              :value="alert.replace(/_/g, ' ')"
              severity="warn"
            />
          </div>

          <div v-if="job.customer?.notes" class="job-notes">
            <i class="pi pi-info-circle" />
            <span>{{ job.customer.notes }}</span>
          </div>

          <div
            v-if="summaryLabel(job.parts_summary) || true"
            class="job-parts-row"
            @click="toggleParts(job)"
            :data-testid="`parts-summary-${job.id}`"
          >
            <i class="pi pi-wrench" />
            <span>{{ summaryLabel(job.parts_summary) || 'No parts requested' }}</span>
            <span
              v-if="(partsUnseenByJob[job.id] || 0) > 0"
              class="parts-unseen-badge"
              :title="`${partsUnseenByJob[job.id]} update(s) from dispatch`"
            >
              {{ partsUnseenByJob[job.id] }}
            </span>
            <i :class="['pi', expandedJobId === job.id ? 'pi-chevron-up' : 'pi-chevron-down']" />
          </div>

          <div v-if="expandedJobId === job.id" class="job-parts-panel">
            <div class="job-parts-head">
              <Button
                label="Request part"
                icon="pi pi-plus"
                size="small"
                @click.stop="openRequestParts(job)"
              />
            </div>
            <div v-if="partsLoading[job.id]" class="muted">Loading…</div>
            <ul v-else-if="(partsByJob[job.id] || []).length" class="job-parts-list">
              <li
                v-for="p in partsByJob[job.id]"
                :key="p.id"
                class="job-parts-item"
              >
                <div class="job-parts-line">
                  <Tag :value="p.status" :severity="partStatusSeverity(p.status)" />
                  <Tag
                    v-if="p.urgency && p.urgency !== 'normal'"
                    :value="p.urgency"
                    :severity="p.urgency === 'critical' ? 'danger' : 'warn'"
                  />
                  <strong>{{ p.part_name }}</strong>
                  <span class="muted">×{{ p.quantity || 1 }}</span>
                </div>
                <div v-if="p.sku || p.eta_at" class="job-parts-meta">
                  <span v-if="p.sku">SKU {{ p.sku }}</span>
                  <span v-if="p.eta_at">ETA {{ formatEta(p.eta_at) }}</span>
                </div>
                <div v-if="p.status === 'needed'" class="job-parts-actions">
                  <Button
                    label="Edit"
                    icon="pi pi-pencil"
                    text
                    size="small"
                    @click.stop="openEditPart(job, p)"
                  />
                </div>
              </li>
            </ul>
            <div v-else class="muted">No parts requested for this job yet.</div>
          </div>

          <div
            class="job-parts-row"
            @click="toggleEquipment(job)"
            :data-testid="`equipment-summary-${job.id}`"
          >
            <i class="pi pi-box" />
            <span>Install &amp; equipment</span>
            <i :class="['pi', equipExpandedJobId === job.id ? 'pi-chevron-up' : 'pi-chevron-down']" />
          </div>

          <div
            v-if="equipExpandedJobId === job.id"
            class="job-parts-panel"
            :data-testid="`equipment-panel-${job.id}`"
          >
            <div v-if="equipLoading[job.customer?.id]" class="muted">Loading…</div>
            <ul v-else-if="(equipByCustomer[job.customer?.id] || []).length" class="job-parts-list">
              <li
                v-for="e in equipByCustomer[job.customer?.id]"
                :key="e.id"
                class="job-parts-item"
              >
                <div class="job-parts-line">
                  <Tag
                    :value="equipTypeLabel(e.equipment_type)"
                    :severity="e.equipment_type === 'garage_door' ? 'info' : 'secondary'"
                  />
                  <strong>{{ equipTitle(e) }}</strong>
                </div>
                <div
                  v-if="e.serial_number || e.installation_date || e.warranty_expires_on"
                  class="job-parts-meta"
                >
                  <span v-if="e.serial_number">S/N {{ e.serial_number }}</span>
                  <span v-if="e.installation_date">Installed {{ e.installation_date }}</span>
                  <span v-if="e.warranty_expires_on">Warranty → {{ e.warranty_expires_on }}</span>
                </div>
                <div v-if="e.notes" class="job-equip-notes">{{ e.notes }}</div>
              </li>
            </ul>
            <div v-else class="muted">No install/equipment on file for this site.</div>
          </div>

          <div class="job-actions">
            <Button
              v-if="job.dispatch_status === 'assigned' || job.dispatch_status === 'unassigned' || !job.dispatch_status"
              label="On my way"
              icon="pi pi-send"
              :loading="advancing[job.id]"
              @click="onMyWay(job)"
            />
            <Button
              v-else-if="job.dispatch_status === 'en_route'"
              label="I'm here"
              icon="pi pi-map-marker"
              :loading="advancing[job.id]"
              @click="imHere(job)"
            />
            <Button
              v-else-if="job.dispatch_status === 'on_site'"
              label="Complete"
              icon="pi pi-check"
              severity="success"
              :loading="advancing[job.id]"
              @click="openComplete(job)"
            />
            <Button
              v-if="job.dispatch_status !== 'done'"
              label="Navigate"
              icon="pi pi-directions"
              severity="secondary"
              outlined
              :disabled="!job.navigation_link"
              @click="openMaps(job)"
            />
            <!-- Phase 2.1 — Quote action. Visible while the job is active
                 (not assigned-only — wait until the tech is at least en
                 route so they're not building speculative quotes). -->
            <Button
              v-if="['en_route','on_site','done'].includes(job.dispatch_status)"
              :label="jobLatestActiveQuote(job) ? 'Show quote' : 'Build quote'"
              :icon="jobLatestActiveQuote(job) ? 'pi pi-file' : 'pi pi-pencil'"
              severity="secondary"
              outlined
              @click="ensureJobQuotesLoaded(job).then(() => {
                const q = jobLatestActiveQuote(job)
                if (q) presentQuote(q)
                else openQuoteBuilder(job)
              })"
            />
            <!-- Phase 2.2 — Invoice action. Visible after job is done OR
                 once we've got an accepted quote on the job. -->
            <Button
              v-if="job.dispatch_status === 'done' || jobAcceptedQuote(job)"
              label="Close out"
              icon="pi pi-receipt"
              severity="success"
              @click="ensureJobQuotesLoaded(job).then(() => openInvoice(job))"
            />
            <!-- Phase 4.1 — Per-job chat with dispatch. Available any time
                 the job has any active state (assigned through done). -->
            <Button
              label="Chat"
              icon="pi pi-comment"
              severity="secondary"
              outlined
              @click="openChat(job)"
            />
          </div>
        </li>
        </template>
      </ol>
    </section>

    <!-- Phase 2 / C3.5 — unified closeout sheet (Doug 2026-05-10).
         Replaces the prior thin sig-only complete dialog. See
         components/MobileJobCloseoutDialog.vue. -->
    <MobileJobCloseoutDialog
      v-model:visible="closeoutOpen"
      :job-id="closeoutJob?.id || ''"
      :job-title="closeoutJob?.title || closeoutJob?.customer?.name || ''"
      :customer-name="closeoutJob?.customer?.name || ''"
      @closed-out="onCloseoutDone"
    />

    <Dialog
      v-model:visible="partsModalOpen"
      :header="partsModalEditingId ? 'Edit part request' : 'Request part'"
      modal
      :style="{ width: '92vw', maxWidth: '480px' }"
    >
      <div class="parts-form">
        <div class="form-field">
          <label>Part / SKU</label>
          <AutoComplete
            v-model="partsForm.sku"
            :suggestions="skuSuggestions"
            optionLabel="sku"
            placeholder="Type SKU or part name"
            :loading="skuLoading"
            forceSelection="false"
            completeOnFocus
            @complete="searchSku"
            @item-select="(e) => pickSuggestion(e.value)"
          >
            <template #option="slotProps">
              <div class="sku-option">
                <strong>{{ slotProps.option.sku || slotProps.option.name }}</strong>
                <span class="muted">{{ slotProps.option.name }}</span>
                <span v-if="slotProps.option.qty_on_hand != null" class="muted">
                  · on hand: {{ slotProps.option.qty_on_hand }}
                </span>
                <span v-if="slotProps.option.source === 'door_catalog'" class="muted">
                  · door catalog
                </span>
              </div>
            </template>
          </AutoComplete>
        </div>
        <div class="form-field">
          <label>Description</label>
          <InputText v-model="partsForm.part_name" placeholder="What is this part?" />
        </div>
        <div class="form-row">
          <div class="form-field">
            <label>Qty</label>
            <InputNumber v-model="partsForm.quantity" :min="1" :max="999" showButtons />
          </div>
          <div class="form-field">
            <label>Urgency</label>
            <Select
              v-model="partsForm.urgency"
              :options="URGENCY_OPTIONS"
              optionLabel="label"
              optionValue="value"
            />
          </div>
        </div>
        <div class="form-field">
          <label>Supplier (optional)</label>
          <InputText v-model="partsForm.supplier" />
        </div>
        <div class="form-field">
          <label>Notes</label>
          <Textarea v-model="partsForm.notes" rows="2" autoResize />
        </div>
      </div>
      <template #footer>
        <Button label="Cancel" text @click="partsModalOpen = false" />
        <Button
          :label="partsModalEditingId ? 'Save' : 'Request'"
          icon="pi pi-check"
          :loading="partsSubmitting"
          @click="submitPartsForm"
        />
      </template>
    </Dialog>

    <!-- Phase 2.1 + 2.2 — Quote builder, customer-facing presentation,
         and close-out invoice dialog. All three are inert until opened
         via job-card actions. -->
    <MobileQuoteBuilderDialog
      v-model:visible="quoteBuilderOpen"
      :job="quoteBuilderJob"
      @saved="onQuoteBuilt"
      @present="presentQuote"
    />
    <MobileCustomerQuoteDialog
      v-model:visible="customerQuoteOpen"
      :quote="customerQuote"
      @accepted="onQuoteAccepted"
      @declined="onQuoteDeclined"
    />
    <MobileInvoiceDialog
      v-model:visible="invoiceDialogOpen"
      :job="invoiceDialogJob"
      @invoiced="onInvoiced"
    />
    <MobileChatDialog
      v-model:visible="chatDialogOpen"
      :job="chatDialogJob"
    />
</template>

<style scoped>
.today-route {
  padding: 0.75rem;
  max-width: 800px;
  margin: 0 auto;
}

/* Phase 3 (S3-A5) — offline banner */
.offline-banner {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.6rem 0.85rem;
  margin-bottom: 0.75rem;
  background: #fef2f2;
  border: 1px solid #fca5a5;
  border-left: 4px solid #dc2626;
  border-radius: 0.5rem;
  font-size: 0.85rem;
}
.offline-banner.banner-online {
  background: #fefce8;
  border-color: #fde68a;
  border-left-color: #ca8a04;
}
.offline-banner > .pi { color: #dc2626; font-size: 1.1rem; }
.offline-banner.banner-online > .pi { color: #ca8a04; }
.offline-banner-text { flex: 1; line-height: 1.3; }
.offline-banner-text strong { display: block; font-size: 0.95rem; color: #1f2937; }
.offline-sub { font-size: 0.75rem; color: var(--p-text-muted-color, #6b7280); margin-top: 0.1rem; }
.slide-down-enter-active, .slide-down-leave-active {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.slide-down-enter-from, .slide-down-leave-to {
  transform: translateY(-100%); opacity: 0;
}
.today-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
  flex-wrap: wrap;
}
@media (max-width: 480px) {
  .today-header {
    flex-direction: column;
    align-items: stretch;
  }
  .today-header > div:first-child {
    text-align: left;
  }
  .today-actions {
    justify-content: flex-end;
  }
}
.today-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}
.today-sub {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.85rem;
  margin-top: 0.15rem;
}
.loading,
.empty {
  text-align: center;
  padding: 2rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
}
.empty-icon {
  font-size: 2rem;
  display: block;
  margin: 0 auto 0.5rem;
  opacity: 0.5;
}
.empty-title {
  font-size: 1.05rem;
  font-weight: 600;
  margin-bottom: 0.25rem;
}
.empty-help {
  font-size: 0.85rem;
}
.job-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.job-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  padding: 0.75rem 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.job-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.job-row-top {
  justify-content: space-between;
}
.job-time {
  font-weight: 700;
  font-size: 1rem;
}
.job-pills {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
}
.job-stop-num {
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
}
.job-customer {
  font-weight: 600;
  font-size: 1.05rem;
}
.job-multitech {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  align-self: flex-start;
  padding: 0.15rem 0.5rem;
  background: var(--p-surface-100, #f3f4f6);
  border-radius: 999px;
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
}
.job-multitech i {
  font-size: 0.75rem;
}
/* Status pills — icon + text + sunlight-safe color. Never color alone:
   every pill carries an icon so colorblind techs and bright-cab readers
   parse the shape, not the hue. Colors are slate/amber/blue/green tuned
   to survive direct sun. */
.status-pill {
  display: inline-flex; align-items: center; gap: 0.3rem;
  padding: 0.2rem 0.55rem; border-radius: 999px;
  font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em;
}
.status-pill i { font-size: 0.7rem; }
.status-assigned   { background: #475569; color: #fff; }
.status-unassigned { background: #6b7280; color: #fff; }
.status-en_route   { background: #f59e0b; color: #1f2937; }
.status-on_site    { background: #2563eb; color: #fff; }
.status-done       { background: #15803d; color: #fff; }
/* Primary action — research says 64px, brand color, full-width-minus-gutter
   for the next-state button. The card already keeps it last in DOM order
   so it sits at the bottom; this just makes the tap target obvious. */
.job-actions {
  display: flex; gap: 0.5rem; margin-top: 0.25rem;
}
.job-actions .p-button {
  flex: 1; min-height: 56px; font-size: 1rem; font-weight: 600;
}
.job-actions .p-button:first-child {
  flex: 1.6;  /* primary gets more visual weight than secondary */
}
/* Address — single line, ellipsis on overflow, padded for finger reach. */
.job-address {
  min-height: 32px;
}
.job-address > span,
.job-address {
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.job-address {
  color: var(--p-primary-color, #2563eb);
  cursor: pointer;
  font-size: 0.95rem;
  display: flex;
  align-items: center;
  gap: 0.35rem;
}
.job-address.job-address-missing {
  color: var(--p-text-muted-color, #9ca3af);
  cursor: default;
  font-style: italic;
}
.job-address.job-address-missing i {
  color: var(--p-text-muted-color, #9ca3af);
}
.job-service {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.9rem;
}
.job-alerts {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
}
.job-notes {
  background: var(--p-yellow-50, #fef9c3);
  border-left: 3px solid var(--p-yellow-400, #facc15);
  border-radius: 0.25rem;
  padding: 0.5rem 0.65rem;
  font-size: 0.9rem;
  display: flex;
  gap: 0.5rem;
  align-items: flex-start;
}
.job-actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.4rem;
}
.job-actions :deep(.p-button) {
  flex: 1;
}
.leg-eta {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 0.4rem;
  padding: 0.25rem 0;
  margin: -0.15rem 0;
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
}
.leg-eta i {
  font-size: 0.8rem;
}
.today-actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.map-wrap {
  position: relative;
}
.map-container {
  width: 100%;
  height: 60vh;
  min-height: 360px;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  overflow: hidden;
  background: #f3f4f6;
}
.map-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  text-align: center;
  padding: 1rem;
  color: var(--p-text-muted-color, #6b7280);
  background: rgba(255, 255, 255, 0.85);
  border-radius: 0.5rem;
}
.reorder-controls {
  display: flex;
  gap: 0.15rem;
}
.job-parts-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.45rem 0.55rem;
  margin-top: 0.2rem;
  background: var(--p-surface-100, #f3f4f6);
  border-radius: 0.4rem;
  font-size: 0.85rem;
  cursor: pointer;
  user-select: none;
}
.job-parts-row .pi-chevron-up,
.job-parts-row .pi-chevron-down {
  margin-left: auto;
  font-size: 0.75rem;
  opacity: 0.6;
}
.parts-unseen-badge {
  margin-left: auto;
  background: #ef4444;
  color: #fff;
  font-size: 0.7rem;
  font-weight: 700;
  border-radius: 999px;
  min-width: 1.25rem;
  height: 1.25rem;
  padding: 0 0.4rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.parts-unseen-badge + .pi-chevron-up,
.parts-unseen-badge + .pi-chevron-down {
  margin-left: 0.4rem;
}
.job-parts-panel {
  margin-top: 0.4rem;
  padding: 0.5rem 0.6rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.4rem;
  background: var(--p-content-background, #fff);
}
.job-parts-head {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 0.4rem;
}
.job-parts-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.job-parts-item {
  border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
  padding-bottom: 0.4rem;
}
.job-parts-item:last-child {
  border-bottom: none;
}
.job-parts-line {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  flex-wrap: wrap;
  font-size: 0.9rem;
}
.job-parts-meta {
  display: flex;
  gap: 0.6rem;
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
  margin-top: 0.15rem;
}
.job-parts-actions {
  margin-top: 0.15rem;
}
.job-equip-notes {
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
  margin-top: 0.2rem;
  line-height: 1.4;
}
.muted {
  color: var(--p-text-muted-color, #6b7280);
}
.parts-form {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
.parts-form .form-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.parts-form .form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.6rem;
}
.parts-form label {
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 600;
}
.sku-option {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: baseline;
}
.push-cta {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  background: var(--p-primary-50, #eff6ff);
  border: 1px solid var(--p-primary-200, #bfdbfe);
  color: var(--text-primary, inherit);
  border-radius: 0.5rem;
  padding: 0.6rem 0.75rem;
  margin-bottom: 0.6rem;
  font-size: 0.85rem;
}
/* MH-3 (audit P1 #10): pre-fix the banner kept its pale-green Aura
   primary-50 background while the page went dark — heading + body text
   were near-invisible (light-gray on pale-green). Explicit dark-mode
   styling brings it to a dark-blue card with light text (≥7:1 contrast).
   Two blocks: the data-theme selector that the in-app toggle sets, AND
   a prefers-color-scheme fallback so the OS pref still flips the banner
   when the theme store boots after first paint. */
[data-theme="dark"] .push-cta {
  background: rgba(37, 99, 235, 0.12);
  border-color: rgba(37, 99, 235, 0.45);
  color: var(--color-text-100, #e6edf9);
}
[data-theme="dark"] .push-cta > .pi-bell {
  color: #93c5fd;
}
[data-theme="dark"] .push-cta-text strong {
  color: var(--color-text-100, #e6edf9);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .push-cta {
    background: rgba(37, 99, 235, 0.12);
    border-color: rgba(37, 99, 235, 0.45);
    color: var(--color-text-100, #e6edf9);
  }
  :root:not([data-theme="light"]) .push-cta > .pi-bell {
    color: #93c5fd;
  }
  :root:not([data-theme="light"]) .push-cta-text strong {
    color: var(--color-text-100, #e6edf9);
  }
}
.push-cta > .pi-bell {
  font-size: 1.2rem;
  color: var(--p-primary-600, #2563eb);
}
.push-cta-text {
  flex: 1;
  line-height: 1.25;
}
.push-cta-text strong {
  display: block;
  font-size: 0.95rem;
  margin-bottom: 0.1rem;
}
/* Phase 2 / C3.5 — removed .sig-form / .sig-canvas / .sig-clear; the
   inline complete-and-sign dialog they styled is gone, replaced by
   MobileJobCloseoutDialog which carries its own scoped styles. */
</style>

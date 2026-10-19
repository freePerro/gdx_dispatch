<script setup>
// Mobile dispatch surface — three tabs:
//   Board   — today's jobs grouped by tech + unassigned, with reassign dialog
//   Threads — dispatcher ↔ tech chat threads (original Phase 4.4 view)
//   Live    — latest tech locations (read-only, no map yet)
//
// Drag-and-drop board reassignment from desktop is replaced by a tap → tech
// picker dialog. That's the most usable shape on a phone — drag-targets
// don't survive small viewports.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import SelectButton from 'primevue/selectbutton'
import Select from 'primevue/select'
import InputText from 'primevue/inputtext'
import DatePicker from 'primevue/datepicker'
import Tag from 'primevue/tag'
import { useApi } from '../composables/useApi'
import { toShopWallClock, useTenantTimezone } from '../composables/useTenantTimezone'
import { useToast } from 'primevue/usetoast'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm'
import MobileChatDialog from '../components/MobileChatDialog.vue'
import CrewChangeDialog from '../components/CrewChangeDialog.vue'
import DayOverflowDialog from '../components/DayOverflowDialog.vue'
import { crewOf, crewPatch, useCrewChangePrompt } from '../composables/useCrewChange'
import { hoursLeftInDay, scheduleDropPatch, shiftHours, useDayOverflowPrompt } from '../composables/useScheduleDrop'

const api = useApi()
const toast = useToast()
const { confirmAsync } = useDestructiveConfirm()
const route = useRoute()
const router = useRouter()
// Office display timezone — the board buckets jobs into the selected day in
// THIS zone (same basis as the desktop board), not UTC/browser time.
const { tenantTimezone, zonedDateKey } = useTenantTimezone()

const TABS = [
  { label: 'Board', value: 'board' },
  { label: 'Threads', value: 'threads' },
  { label: 'Live', value: 'live' },
]
const activeTab = ref('board')

// ── Board ────────────────────────────────────────────────────────────
const selectedDate = ref(new Date())
const jobs = ref([])
const technicians = ref([])
const scheduledUnassigned = ref([])
const boardLoading = ref(false)
const optimizerLoading = ref(false)
const expandedTech = ref(null)

const assignDialogVisible = ref(false)
const assignDialogJob = ref(null)
const assignDialogTechId = ref(null)
const assignDialogSaving = ref(false)

// Quick-add job
const newJobOpen = ref(false)
const newJobSaving = ref(false)
const newJobForm = ref(emptyJobForm())
const customerOptions = ref([])
const customersLoading = ref(false)

function emptyJobForm() {
  return { customer_id: null, title: '', scheduled_at: new Date(), assigned_tech_id: null, hours: '' }
}

const selectedDateStr = computed(() => {
  const d = selectedDate.value
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
})

const isToday = computed(() => {
  const t = new Date()
  return t.toDateString() === selectedDate.value.toDateString()
})

const dateLabel = computed(() => {
  if (isToday.value) return 'Today'
  return selectedDate.value.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
})

// Jobs on the selected day. The server honors ?date= as a ±1-day UTC window
// (wide on purpose — it can't reproduce the tenant-zone day cut), so this
// client-side filter remains the PRECISE cut and must stay. A dated job
// matches when its OFFICE-local day equals the selected day; undated jobs
// (leads with no time yet) surface on today's board only, so they don't
// duplicate across every day or vanish entirely. A finished job that was
// never scheduled is not a lead: it sits on the day it was completed, or on
// no day (2026-10-15 — it used to fill today's tech columns every day).
const dayJobs = computed(() => {
  const key = selectedDateStr.value
  return jobs.value.filter((j) => {
    if (j.scheduled_at) return zonedDateKey(j.scheduled_at) === key
    if (isTerminal(j)) return Boolean(j.completed_at) && zonedDateKey(j.completed_at) === key
    return isToday.value
  })
})

const unassignedJobs = computed(() =>
  // MH-6: exclude terminal-state jobs (Complete / Cancelled / Paid /
  // Failed). Audit found the Unassigned queue was ~90% completed QB-
  // imported historical records under a green "Assign tech" CTA — not
  // actionable. Terminal jobs that legitimately have no tech (closed-
  // out historical imports) belong in reports, not the live queue.
  dayJobs.value.filter((j) => !j.technician_id && !j.assigned_to && !isTerminal(j)),
)

const techColumns = computed(() => {
  const byTech = new Map()
  for (const tech of technicians.value) {
    byTech.set(String(tech.id), { id: String(tech.id), name: tech.name, jobs: [] })
  }
  for (const job of dayJobs.value) {
    const tid = String(job.technician_id || job.assigned_to || '')
    if (!tid) continue
    if (!byTech.has(tid)) {
      byTech.set(tid, { id: tid, name: 'Unknown tech', jobs: [] })
    }
    byTech.get(tid).jobs.push(job)
  }
  return Array.from(byTech.values()).sort((a, b) => a.name.localeCompare(b.name))
})

function shiftDay(delta) {
  const d = new Date(selectedDate.value)
  d.setDate(d.getDate() + delta)
  selectedDate.value = d
}

function goToday() {
  selectedDate.value = new Date()
}

// MH-6 (audit P1 #6): canonical job-status enum the badge is allowed
// to render. Pre-fix `job.status` sometimes carried non-status values
// like "Service Call" (job type) or "QB Import" (import source) which
// the badge rendered as-if-they-were-status, so a dispatcher couldn't
// trust the badge. Whitelist gate: only canonical values pass through;
// anything else triggers the neutral "pending" rendering.
const CANONICAL_JOB_STATUSES = new Set([
  'new', 'pending', 'scheduled',
  'en_route', 'en route', 'on site', 'on_site',
  'in_progress', 'on_hold', 'hold',
  'done', 'complete', 'completed', 'paid',
  'cancelled', 'canceled', 'failed',
])

function canonicalStatus(status) {
  const s = String(status || '').toLowerCase().trim()
  return CANONICAL_JOB_STATUSES.has(s) ? s : ''
}

function statusBadgeValue(status) {
  // MH-6: only render the raw status string if it's a known enum value;
  // otherwise show "Pending" (the neutral default). Falls through to
  // the existing severity logic.
  const c = canonicalStatus(status)
  return c ? status : 'pending'
}

function statusSeverity(status) {
  const s = canonicalStatus(status) || 'pending'
  if (['done', 'complete', 'completed', 'paid'].includes(s)) return 'success'
  if (['en_route', 'en route', 'on site', 'on_site', 'in_progress'].includes(s)) return 'info'
  if (['pending', 'scheduled', 'new'].includes(s)) return 'warn'
  if (['canceled', 'cancelled', 'failed'].includes(s)) return 'danger'
  if (['on_hold', 'hold'].includes(s)) return 'secondary'
  return 'secondary'
}

// MH-6 (audit P1 #7): the Unassigned bucket on /mobile/dispatch was 33-
// of-38 Completed/QB-imported historical jobs. A dispatcher's actionable
// queue must exclude terminal-state jobs by default — they're nothing
// to assign. Status enum values that count as terminal:
const TERMINAL_STATUSES = new Set(['done', 'complete', 'completed', 'paid', 'canceled', 'cancelled', 'failed'])

function isTerminal(job) {
  if (!job) return false
  const stage = String(job.lifecycle_stage || '').toLowerCase()
  if (stage === 'completed' || stage === 'cancelled') return true
  return TERMINAL_STATUSES.has(String(job.status || '').toLowerCase().trim())
}

function displayCustomer(job) {
  return job.customer_name || job.customer?.name || job.title || 'Job'
}

// Title renders only when it adds information — displayCustomer already
// falls back to the title for customer-less jobs, which would show it twice.
function displayTitle(job) {
  const t = (job?.title || '').trim()
  return t && t !== displayCustomer(job) ? t : ''
}

function timeWindow(job) {
  if (job.time_window) return job.time_window
  if (job.scheduled_at) {
    try {
      return new Date(job.scheduled_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    } catch { return '' }
  }
  return ''
}

async function fetchJobs() {
  try {
    // The server scopes ?date= to a ±1-day window and lifts the page cap to
    // 500 for date-scoped requests, so a busy day can't fall off page 1
    // anymore; `dayJobs` still applies the exact tenant-zone day cut.
    const data = await api.get(`/api/jobs?date=${selectedDateStr.value}`)
    const list = Array.isArray(data) ? data : data?.items || data?.jobs || []
    jobs.value = list
  } catch {
    jobs.value = []
  }
}

async function fetchTechnicians() {
  try {
    const data = await api.get('/api/technicians')
    const rows = Array.isArray(data) ? data : data?.items || data?.data || []
    technicians.value = rows
      .filter((r) => r.active !== false)
      .map((r) => ({
        id: r.id,
        name: r.name || r.full_name || r.username || r.email || 'Tech',
        // The shift a booking is measured against (fitInTechDay).
        effective_shift_start: r.effective_shift_start || null,
        effective_shift_end: r.effective_shift_end || null,
        effective_workdays: Number.isInteger(r.effective_workdays) ? r.effective_workdays : 31,
      }))
  } catch {
    technicians.value = []
  }
}

async function fetchScheduledUnassigned() {
  try {
    const data = await api.get('/api/dispatch/scheduled-unassigned')
    scheduledUnassigned.value = Array.isArray(data) ? data : data?.items || []
  } catch {
    scheduledUnassigned.value = []
  }
}

async function refreshBoard() {
  boardLoading.value = true
  try {
    await Promise.all([fetchJobs(), fetchTechnicians(), fetchScheduledUnassigned()])
  } finally {
    boardLoading.value = false
  }
}

function openAssignDialog(job) {
  assignDialogJob.value = job
  assignDialogTechId.value = job.technician_id || job.assigned_to || null
  assignDialogVisible.value = true
}

const { prompt: crewPrompt, ask: askCrew, answer: answerCrewPrompt } = useCrewChangePrompt(
  (id) => (technicians.value.find((t) => String(t.id) === String(id)) || {}).name || 'Tech',
)

const { prompt: overflowPrompt, ask: askOverflow, answer: answerOverflowPrompt } = useDayOverflowPrompt()

// A booking longer than what is left of the tech's shift asks first, with
// the desktop board's DayOverflowDialog (Doug, 2026-10-18; GDXA-1686): add a
// tech, book the rest on the tech's next workdays, or keep it on the day.
// Resolves { choice, shift } — choice null when it fits or there is nothing
// to measure (no hours, or a tech with no shift) — or null on Cancel.
// `techIds` is the crew after this booking, lead first.
async function fitInTechDay({ label, hours, techId, techIds, scheduledAt }) {
  const tech = technicians.value.find((t) => String(t.id) === String(techId))
  const shift = tech ? shiftHours(tech.effective_shift_start, tech.effective_shift_end) : null
  const wall = toShopWallClock(scheduledAt, tenantTimezone?.value)
  const left = wall ? hoursLeftInDay(wall.getHours() + wall.getMinutes() / 60, shift) : null
  const need = Number(hours)
  if (!(need > 0) || left == null || need <= left) return { choice: null, shift }
  const pad = (n) => String(n).padStart(2, '0')
  const choice = await askOverflow({
    jobLabel: label,
    techName: tech.name,
    day: `${wall.getFullYear()}-${pad(wall.getMonth() + 1)}-${pad(wall.getDate())}`,
    shift,
    mask: tech.effective_workdays,
    hours: need,
    hoursLeft: left,
    techIds: techIds.map(String),
    leadId: String(techIds[0]),
    droppedId: String(techId),
  })
  if (!choice) return null
  // The hours go out only when a bigger crew changed them.
  return { choice: { ...choice, hours: choice.hours !== need ? choice.hours : null }, shift }
}

// The body for a booking the overflow prompt answered: date, crew, lead and
// changed hours through the same builder the desktop board uses; a split
// books only the first day's hours as this visit.
function bookingPatch({ choice }, scheduledAt) {
  const patch = scheduleDropPatch(choice, scheduledAt)
  if (choice.days) patch.visit_duration_minutes = Math.round(choice.days[0].hours * 60)
  return patch
}

// The days after the first of a split booking, through the job's Visits
// route, from shift start for the whole crew; days of one length in one
// request. A refusal leaves the first day booked and says where the rest go.
async function bookLaterDays(jobId, label, { choice, shift }) {
  const byLength = new Map()
  for (const d of choice.days.slice(1)) {
    byLength.set(d.hours, [...(byLength.get(d.hours) || []), d.day])
  }
  const startMin = Math.round(shift.start * 60)
  const startTime = `${String(Math.floor(startMin / 60)).padStart(2, '0')}:${String(startMin % 60).padStart(2, '0')}`
  try {
    for (const [hours, days] of byLength) {
      await api.post(`/api/jobs/${jobId}/visits`, {
        days,
        start_time: startTime,
        duration_minutes: Math.round(hours * 60),
        tech_ids: choice.tech_ids,
      }, { suppressErrorToast: true })
    }
  } catch (e) {
    toast.add({
      severity: 'warn',
      summary: `Only part of ${label} was booked`,
      detail: `${e?.message || 'The other days could not be booked.'} Add them from the Visits card on the job page.`,
      life: 9000,
    })
  }
}

function closeAssignDialog() {
  assignDialogVisible.value = false
  assignDialogJob.value = null
  assignDialogTechId.value = null
}

async function confirmAssign() {
  const job = assignDialogJob.value
  if (!job || !assignDialogTechId.value) {
    assignDialogVisible.value = false
    return
  }
  const techId = String(assignDialogTechId.value)
  // A single assigned_tech_id replaces the whole crew. On a crew job the pick
  // keeps everyone: the lead is a no-op, another member becomes lead, anyone
  // else is a swap or an add, asked here; Cancel changes nothing.
  const crew = crewOf(job)
  let patch = { assigned_tech_id: techId, assigned_to: techId, technician_id: techId }
  let crewAfter = [techId] // lead first, as the overflow prompt wants it
  if (crew.length >= 2) {
    if (techId === crew[0]) {
      closeAssignDialog()
      return
    }
    if (crew.includes(techId)) {
      patch = { assigned_tech_ids: crew, lead_tech_id: techId }
      crewAfter = [techId, ...crew.filter((id) => id !== techId)]
    } else {
      assignDialogVisible.value = false
      const choice = await askCrew({ crew, techId, jobLabel: displayCustomer(job) })
      if (!choice) {
        closeAssignDialog()
        return
      }
      patch = crewPatch(choice)
      crewAfter = choice.tech_ids.map(String)
    }
  }
  // An undated job is being booked into the tech's day, at 09:00 on the
  // board day: one longer than the tech has left asks first, with the
  // desktop drop's overflow dialog (GDXA-1686); Cancel writes nothing. Unlike
  // the desktop drop it does not ask for hours, so a job with none books
  // unmeasured. A dated job keeps its time and is not measured, on either
  // board; a job under way keeps its crew and lane (GDXA-1370/1377).
  let booking = { choice: null }
  // The list sends the label ("In Progress"); normalized as the desktop's jobStage.
  const underWay = String(job.lifecycle_stage || '').trim().toLowerCase().replace(/[\s-]+/g, '_') === 'in_progress'
  if (!job.scheduled_at) {
    const d = new Date(selectedDate.value)
    d.setHours(9, 0, 0, 0)
    patch.scheduled_at = d.toISOString()
  }
  if (!job.scheduled_at && !underWay) {
    booking = await fitInTechDay({
      label: displayCustomer(job),
      hours: job.scheduled_duration_hours,
      techId,
      techIds: crewAfter,
      scheduledAt: patch.scheduled_at,
    })
    if (!booking) {
      closeAssignDialog()
      return
    }
    if (booking.choice) patch = bookingPatch(booking, patch.scheduled_at)
    // Booked, it leaves the lane it waited in: a lane lists by
    // holding_area_id alone, so a stale one draws the job twice (GDXA-1378).
    if (job.holding_area_id) patch.holding_area_id = null
  }
  assignDialogSaving.value = true
  try {
    await api.patch(`/api/jobs/${job.id}`, patch)
    if (booking.choice?.days) await bookLaterDays(job.id, displayCustomer(job), booking)
    const techName = (technicians.value.find((t) => String(t.id) === String(techId)) || {}).name || 'tech'
    toast.add({
      severity: 'success',
      summary: 'Assigned',
      detail: `${displayCustomer(job)} → ${techName}`,
      life: 3000,
    })
    closeAssignDialog()
    await refreshBoard()
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Assignment failed', detail: e.message, life: 4000 })
  } finally {
    assignDialogSaving.value = false
  }
}

// One tap takes every tech off the job, crew and all, so it asks first.
async function unassignJob(job) {
  const crew = crewOf(job)
  if (crew.length) {
    const ok = await confirmAsync({
      header: crew.length > 1 ? 'Remove every technician?' : 'Remove the technician?',
      message: `${crew.map(techNameFor).join(', ')} will be taken off this job, and it goes back to unassigned.`,
      acceptLabel: 'Remove',
    })
    if (!ok) return
  }
  try {
    await api.patch(`/api/jobs/${job.id}`, {
      assigned_tech_id: null,
      assigned_to: null,
      technician_id: null,
    })
    toast.add({ severity: 'success', summary: 'Unassigned', life: 2500 })
    await refreshBoard()
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Unassign failed', detail: e.message, life: 4000 })
  }
}

async function loadCustomerOptions() {
  if (customerOptions.value.length) return
  customersLoading.value = true
  try {
    const r = await api.get('/api/customers?per_page=500')
    const list = Array.isArray(r) ? r : r?.items || r?.data || []
    customerOptions.value = list.map((c) => ({ label: c.name || c.email || c.id, value: c.id }))
  } catch {
    customerOptions.value = []
  } finally {
    customersLoading.value = false
  }
}

async function openNewJob() {
  newJobForm.value = emptyJobForm()
  newJobOpen.value = true
  await loadCustomerOptions()
}

async function submitNewJob() {
  const f = newJobForm.value
  if (!f.customer_id || !f.title.trim()) return
  newJobSaving.value = true
  try {
    const scheduled = f.scheduled_at instanceof Date ? f.scheduled_at : new Date(f.scheduled_at)
    if (!isNaN(scheduled.getTime()) && scheduled.getHours() === 0) {
      scheduled.setHours(9, 0, 0, 0)
    }
    const payload = {
      customer_id: f.customer_id,
      title: f.title.trim(),
      scheduled_at: scheduled.toISOString(),
    }
    const hours = Number(f.hours)
    if (hours > 0) payload.scheduled_duration_hours = hours
    const label = customerOptions.value.find((c) => c.value === f.customer_id)?.label || payload.title
    let booking = { choice: null }
    let booked = true
    if (f.assigned_tech_id) {
      payload.assigned_tech_id = f.assigned_tech_id
      payload.assigned_to = f.assigned_tech_id
      payload.technician_id = f.assigned_tech_id
      // Booked into the tech's day: one longer than they have left asks
      // first (GDXA-1686). Cancel leaves the form open and writes nothing.
      booking = await fitInTechDay({
        label,
        hours,
        techId: f.assigned_tech_id,
        techIds: [String(f.assigned_tech_id)],
        scheduledAt: payload.scheduled_at,
      })
      if (!booking) return
    }
    if (booking.choice) {
      // The crew it picked replaces the one-tech assignment.
      for (const k of ['assigned_tech_id', 'assigned_to', 'technician_id']) delete payload[k]
      payload.assigned_tech_ids = booking.choice.tech_ids
      payload.lead_tech_id = booking.choice.lead
      if (booking.choice.hours != null) payload.scheduled_duration_hours = booking.choice.hours
    }
    if (booking.choice?.days) {
      // A create books its one visit at the job's whole hours, so a split is
      // created undated and then booked as the board books one (bookingPatch),
      // leaving the lane create routes an undated job into.
      const { scheduled_at, ...undated } = payload
      const created = await api.post('/api/jobs', undated)
      booked = false
      try {
        await api.patch(`/api/jobs/${created.id}`,
          { ...bookingPatch(booking, scheduled_at), holding_area_id: null }, { suppressErrorToast: true })
        booked = true
      } catch (e) {
        toast.add({
          severity: 'warn',
          summary: `${label} was created but not booked`,
          detail: `${e?.message || 'The booking was refused.'} It is waiting on the board to be assigned a day.`,
          life: 9000,
        })
      }
      if (booked) await bookLaterDays(created.id, label, booking)
    } else {
      await api.post('/api/jobs', payload)
    }
    // Created but not booked has already said so; this would contradict it.
    if (booked) toast.add({ severity: 'success', summary: 'Job created', life: 2500 })
    newJobOpen.value = false
    newJobForm.value = emptyJobForm()
    await refreshBoard()
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Create failed', detail: e.message, life: 4000 })
  } finally {
    newJobSaving.value = false
  }
}

async function runOptimizer() {
  if (optimizerLoading.value) return
  optimizerLoading.value = true
  try {
    await api.post(`/api/dispatch/optimize`, { date: selectedDateStr.value })
    toast.add({ severity: 'success', summary: 'Optimizer queued', life: 3000 })
    await refreshBoard()
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Optimize failed', detail: e.message, life: 4000 })
  } finally {
    optimizerLoading.value = false
  }
}

function toggleTech(techId) {
  expandedTech.value = expandedTech.value === techId ? null : techId
}

// ── Threads (original) ───────────────────────────────────────────────
const threads = ref([])
const threadsLoading = ref(false)
const dispatcherOnly = ref(false)
const chatOpen = ref(false)
const chatJob = ref(null)
let pollTimer = null

async function loadThreads() {
  threadsLoading.value = true
  try {
    const data = await api.get('/api/mobile/dispatch/threads')
    threads.value = data.threads || []
    dispatcherOnly.value = false
  } catch (e) {
    if (e.status === 403 || /dispatcher-only/i.test(e.message || '')) {
      dispatcherOnly.value = true
      threads.value = []
    } else {
      toast.add({ severity: 'error', summary: 'Could not load threads', detail: e.message, life: 4000 })
    }
  } finally {
    threadsLoading.value = false
  }
}

function openThread(t) {
  chatJob.value = { id: t.job_id, title: t.job_title || 'Job' }
  chatOpen.value = true
}

// A tech's chat message pushes the dispatcher /mobile/dispatch?job=<id>
// (#657). Open that thread here — mounted with mark-read, so following the
// notification is what clears its unread badge.
async function openThreadFromLink() {
  const jobId = typeof route.query.job === 'string' ? route.query.job : ''
  if (!jobId) return
  // Consume the link so Back or a refresh does not reopen the thread and
  // stamp it read again. Through the router, not history.replaceState: the
  // router keeps its own copy of the URL and writes it back on the next
  // navigation, so a hand-stripped query returns on Back.
  const { job, ...rest } = route.query
  router.replace({ query: rest })?.catch?.(() => {})
  activeTab.value = 'threads'
  await loadThreads()
  // Older than the 7-day thread window: still open it, just without a title.
  openThread(threads.value.find((t) => String(t.job_id) === jobId) || { job_id: jobId })
}

function fmtAgo(iso) {
  if (!iso) return ''
  const d = typeof iso === 'string' ? new Date(iso) : iso
  const mins = Math.floor((Date.now() - d.getTime()) / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  return d.toLocaleDateString()
}

// ── Live locations ───────────────────────────────────────────────────
const liveLocations = ref([])
const liveLoading = ref(false)

async function loadLive() {
  liveLoading.value = true
  try {
    const data = await api.get('/api/gps/technicians/live')
    if (Array.isArray(data)) liveLocations.value = data
    else if (Array.isArray(data?.locations)) liveLocations.value = data.locations
    else if (Array.isArray(data?.tech_locations)) liveLocations.value = data.tech_locations
    else liveLocations.value = []
  } catch {
    liveLocations.value = []
  } finally {
    liveLoading.value = false
  }
}

function techNameFor(techId) {
  const hit = technicians.value.find((t) => String(t.id) === String(techId))
  return hit ? hit.name : 'Unknown'
}

function fmtCoords(lat, lng) {
  if (lat == null || lng == null) return ''
  return `${Number(lat).toFixed(4)}, ${Number(lng).toFixed(4)}`
}

// ── Lifecycle ────────────────────────────────────────────────────────
watch(selectedDate, () => {
  if (activeTab.value === 'board') refreshBoard()
})

watch(activeTab, (tab) => {
  if (tab === 'board' && jobs.value.length === 0) refreshBoard()
  if (tab === 'threads' && threads.value.length === 0 && !threadsLoading.value) loadThreads()
  if (tab === 'live') loadLive()
})

onMounted(() => {
  refreshBoard()
  openThreadFromLink()
  // Threads tab keeps the original 15s polling cadence when active.
  pollTimer = setInterval(() => {
    if (activeTab.value === 'threads') loadThreads()
  }, 15000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
    <section class="mobile-dispatch">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>Dispatch</h1>
          <div v-if="activeTab === 'board'" class="head-actions">
            <Button
              label="New"
              icon="pi pi-plus"
              size="small"
              @click="openNewJob"
              data-test="md-new-job"
            />
            <Button
              label="Optimize"
              icon="pi pi-play"
              size="small"
              severity="secondary"
              :loading="optimizerLoading"
              @click="runOptimizer"
              data-test="md-optimize"
            />
          </div>
          <Button
            v-else-if="activeTab === 'threads'"
            v-tooltip="'Refresh'"
            icon="pi pi-refresh"
            aria-label="Refresh"
            text
            size="small"
            :loading="threadsLoading"
            @click="loadThreads"
            data-test="md-threads-refresh"
          />
          <Button
            v-else-if="activeTab === 'live'"
            v-tooltip="'Refresh'"
            icon="pi pi-refresh"
            aria-label="Refresh"
            text
            size="small"
            :loading="liveLoading"
            @click="loadLive"
            data-test="md-live-refresh"
          />
        </div>
        <SelectButton
          v-model="activeTab"
          :options="TABS"
          optionLabel="label"
          optionValue="value"
          :allowEmpty="false"
          aria-label="Dispatch section"
          class="tab-switch"
        />
      </header>

      <!-- BOARD ────────────────────────────────────────────────── -->
      <template v-if="activeTab === 'board'">
        <div class="date-pill" data-test="md-date-pill">
          <button type="button" class="date-btn" v-tooltip="'Previous day'" @click="shiftDay(-1)" aria-label="Previous day">
            <i class="pi pi-chevron-left" />
          </button>
          <button type="button" class="date-current" :class="{ 'is-today': isToday }" @click="goToday">
            {{ dateLabel }}
            <span v-if="!isToday" class="date-iso">{{ selectedDateStr }}</span>
          </button>
          <button type="button" class="date-btn" v-tooltip="'Next day'" @click="shiftDay(1)" aria-label="Next day">
            <i class="pi pi-chevron-right" />
          </button>
        </div>

        <div v-if="boardLoading && jobs.length === 0 && technicians.length === 0" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
          <span>Loading board…</span>
        </div>

        <template v-else>
          <!-- Unassigned -->
          <section class="bd-section" data-test="md-unassigned">
            <h2 class="bd-section-title">
              <i class="pi pi-exclamation-triangle" />
              Unassigned
              <span v-if="unassignedJobs.length" class="bd-count">{{ unassignedJobs.length }}</span>
            </h2>
            <p v-if="!unassignedJobs.length" class="bd-empty">All jobs assigned.</p>
            <ol v-else class="bd-list">
              <li v-for="job in unassignedJobs" :key="job.id" class="job-card unassigned-card">
                <div class="job-row">
                  <span class="job-customer"><i v-if="job.is_return_visit" class="pi pi-replay return-visit-icon" v-tooltip="'Return visit'" /> {{ displayCustomer(job) }}</span>
                  <Tag :value="statusBadgeValue(job.status)" :severity="statusSeverity(job.status)" />
                </div>
                <p v-if="displayTitle(job)" class="job-title-line">{{ displayTitle(job) }}</p>
                <div class="job-meta">
                  <span v-if="job.job_type" class="meta-item"><i class="pi pi-briefcase" /> {{ job.job_type }}</span>
                  <span v-if="timeWindow(job)" class="meta-item"><i class="pi pi-clock" /> {{ timeWindow(job) }}</span>
                  <!-- site_address is the /api/jobs effective jobsite; the old
                       job.address key was never in that payload (always blank). -->
                  <span v-if="job.site_full_address || job.site_address" class="meta-item"><i class="pi pi-map-marker" /> {{ job.site_full_address || job.site_address }}</span>
                </div>
                <Button
                  label="Assign tech"
                  icon="pi pi-user-plus"
                  size="small"
                  @click="openAssignDialog(job)"
                  data-test="md-assign-btn"
                />
              </li>
            </ol>
          </section>

          <!-- Tech sections -->
          <section v-for="tech in techColumns" :key="tech.id" class="bd-section">
            <button
              type="button"
              class="tech-header"
              :aria-expanded="expandedTech === tech.id"
              @click="toggleTech(tech.id)"
            >
              <span class="tech-avatar">{{ (tech.name || '?').charAt(0).toUpperCase() }}</span>
              <span class="tech-name">{{ tech.name }}</span>
              <span class="bd-count">{{ tech.jobs.length }}</span>
              <i :class="['pi', expandedTech === tech.id ? 'pi-chevron-up' : 'pi-chevron-down']" />
            </button>
            <ol v-if="expandedTech === tech.id" class="bd-list">
              <li v-if="!tech.jobs.length" class="bd-empty">No jobs.</li>
              <li v-for="job in tech.jobs" :key="job.id" class="job-card tech-job-card">
                <div class="job-row">
                  <span class="job-customer"><i v-if="job.is_return_visit" class="pi pi-replay return-visit-icon" v-tooltip="'Return visit'" /> {{ displayCustomer(job) }}</span>
                  <Tag :value="canonicalStatus(job.status) ? job.status : 'assigned'" :severity="statusSeverity(job.status)" />
                </div>
                <p v-if="displayTitle(job)" class="job-title-line">{{ displayTitle(job) }}</p>
                <div class="job-meta">
                  <span v-if="job.job_type" class="meta-item"><i class="pi pi-briefcase" /> {{ job.job_type }}</span>
                  <span v-if="timeWindow(job)" class="meta-item"><i class="pi pi-clock" /> {{ timeWindow(job) }}</span>
                </div>
                <div class="job-actions">
                  <Button label="Reassign" icon="pi pi-exchange" size="small" severity="secondary" @click="openAssignDialog(job)" data-test="md-reassign-btn" />
                  <Button label="Unassign" icon="pi pi-times" size="small" severity="danger" text @click="unassignJob(job)" data-test="md-unassign-btn" />
                </div>
              </li>
            </ol>
          </section>

          <p v-if="!technicians.length" class="state-msg">
            <i class="pi pi-users" />
            <span>No technicians configured.</span>
          </p>
        </template>
      </template>

      <!-- THREADS ─────────────────────────────────────────────── -->
      <template v-else-if="activeTab === 'threads'">
        <div v-if="threadsLoading && !threads.length" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
          <span>Loading threads…</span>
        </div>
        <div v-else-if="dispatcherOnly" class="state-msg">
          <i class="pi pi-lock empty-icon" />
          <strong>Dispatcher view</strong>
          <p class="muted">Ask your admin for dispatcher permissions.</p>
        </div>
        <div v-else-if="!threads.length" class="state-msg">
          <i class="pi pi-inbox empty-icon" />
          <strong>No active threads</strong>
          <p class="muted">Techs haven't messaged dispatch in the last 7 days.</p>
        </div>
        <ul v-else class="thread-list">
          <li
            v-for="t in threads"
            :key="t.job_id"
            class="thread-item"
            :class="{ 'has-unread': t.unread_count > 0 }"
            @click="openThread(t)"
            data-test="md-thread-row"
          >
            <div class="thread-main">
              <div class="thread-head">
                <strong>{{ t.job_title || 'Job ' + (t.job_id || '').slice(0, 8) }}</strong>
                <Tag v-if="t.unread_count > 0" :value="`${t.unread_count} new`" severity="danger" />
              </div>
              <div class="muted thread-cust">{{ t.customer_name || '' }}{{ t.customer_address ? ' · ' + t.customer_address : '' }}</div>
              <div class="muted thread-time">{{ fmtAgo(t.last_message_at) }}</div>
              <!-- Read state is shared by the office (#656): one dispatcher
                   opening a thread clears it for all. Say who, so a cleared
                   badge is never mistaken for one nobody acted on. -->
              <div
                v-if="t.unread_count === 0 && t.last_read_at"
                class="muted thread-seen"
                data-test="md-thread-seen"
              >
                <i class="pi pi-check" aria-hidden="true" />
                Seen by {{ t.last_read_by_name || 'someone' }} · {{ fmtAgo(t.last_read_at) }}
              </div>
            </div>
            <i class="pi pi-chevron-right" />
          </li>
        </ul>
      </template>

      <!-- LIVE ────────────────────────────────────────────────── -->
      <template v-else-if="activeTab === 'live'">
        <div v-if="liveLoading && !liveLocations.length" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
          <span>Loading locations…</span>
        </div>
        <div v-else-if="!liveLocations.length" class="state-msg">
          <i class="pi pi-map empty-icon" />
          <strong>No location pings</strong>
          <p class="muted">Techs haven't reported a position recently.</p>
        </div>
        <ul v-else class="live-list">
          <li
            v-for="pin in liveLocations"
            :key="pin.tech_id || pin.user_id || pin.id"
            class="live-item"
            data-test="md-live-row"
          >
            <div class="live-main">
              <strong>{{ techNameFor(pin.tech_id || pin.user_id || pin.id) }}</strong>
              <div class="muted live-meta">
                {{ fmtCoords(pin.lat, pin.lng) }}
                <span v-if="pin.recorded_at"> · {{ fmtAgo(pin.recorded_at) }}</span>
              </div>
            </div>
            <i class="pi pi-map-marker" />
          </li>
        </ul>
      </template>

      <!-- New job (quick-add) -->
      <Dialog
        v-model:visible="newJobOpen"
        header="New Job"
        modal
        :style="{ width: '95vw', maxWidth: '460px' }"
        :breakpoints="{ '768px': '95vw' }"
      >
        <div class="form-stack">
          <div>
            <label>Customer *</label>
            <Select
              v-model="newJobForm.customer_id"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              :loading="customersLoading"
              placeholder="Pick a customer"
              filter
              :showClear="true"
              class="w-full"
              data-test="md-newjob-customer"
            />
          </div>
          <div>
            <label>Title *</label>
            <InputText v-model="newJobForm.title" placeholder="What needs doing?" class="w-full" data-test="md-newjob-title" />
          </div>
          <div>
            <label>Scheduled</label>
            <DatePicker
              v-model="newJobForm.scheduled_at"
              dateFormat="yy-mm-dd"
              showTime
              hourFormat="12"
              :showIcon="true"
              class="w-full"
              data-test="md-newjob-when"
            />
          </div>
          <div>
            <label for="md-newjob-hours">Hours (optional)</label>
            <InputText
              id="md-newjob-hours"
              v-model="newJobForm.hours"
              type="number"
              inputmode="decimal"
              step="0.25"
              min="0"
              placeholder="e.g. 1.5"
              class="w-full"
              data-test="md-newjob-hours"
            />
          </div>
          <div>
            <label>Assign tech (optional)</label>
            <Select
              v-model="newJobForm.assigned_tech_id"
              :options="technicians"
              optionLabel="name"
              optionValue="id"
              placeholder="Unassigned"
              :showClear="true"
              filter
              class="w-full"
              data-test="md-newjob-tech"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="newJobOpen = false" />
          <Button
            label="Create"
            :loading="newJobSaving"
            :disabled="!newJobForm.customer_id || !newJobForm.title.trim()"
            @click="submitNewJob"
            data-test="md-newjob-submit"
          />
        </template>
      </Dialog>

      <!-- Assign dialog -->
      <Dialog
        v-model:visible="assignDialogVisible"
        :header="assignDialogJob ? `Assign ${displayCustomer(assignDialogJob)}` : 'Assign'"
        modal
        :style="{ width: '95vw', maxWidth: '420px' }"
        :breakpoints="{ '768px': '95vw' }"
      >
        <div v-if="assignDialogJob" class="form-stack">
          <Select
            v-model="assignDialogTechId"
            :options="technicians"
            optionLabel="name"
            optionValue="id"
            placeholder="Pick a technician"
            :showClear="true"
            filter
            class="w-full"
            data-test="md-assign-select"
          />
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="assignDialogVisible = false" />
          <Button
            label="Assign"
            :loading="assignDialogSaving"
            :disabled="!assignDialogTechId"
            @click="confirmAssign"
            data-test="md-assign-confirm"
          />
        </template>
      </Dialog>
      <CrewChangeDialog :prompt="crewPrompt" @answer="answerCrewPrompt" />
      <DayOverflowDialog :prompt="overflowPrompt" :technicians="technicians" :all-technicians="technicians" @answer="answerOverflowPrompt" />
    </section>
    <MobileChatDialog v-model:visible="chatOpen" :job="chatJob" mark-read @read="loadThreads" />
</template>

<style scoped>
.mobile-dispatch {
  padding: 0.75rem;
  max-width: 800px;
  margin: 0 auto;
}

.mobile-page-head {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  margin-bottom: 0.75rem;
}

.head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.head-actions {
  display: flex;
  gap: 0.4rem;
  align-items: center;
}

.mobile-page-head h1 {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}

.tab-switch :deep(.p-selectbutton) {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  width: 100%;
}

.tab-switch :deep(.p-selectbutton .p-button) {
  padding-block: 0.5rem;
}

.date-pill {
  display: flex;
  align-items: stretch;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  margin-bottom: 0.75rem;
  background: var(--p-content-background, #fff);
  overflow: hidden;
}

.date-btn {
  border: 0;
  background: transparent;
  padding: 0.55rem 0.85rem;
  cursor: pointer;
  color: inherit;
}

.date-btn:active {
  background: var(--p-content-hover-background, #f3f4f6);
}

.date-current {
  flex: 1;
  border: 0;
  background: transparent;
  padding: 0.5rem 0.6rem;
  cursor: pointer;
  text-align: center;
  color: inherit;
  font-weight: 600;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.1rem;
}

.date-current.is-today {
  color: var(--p-primary-color);
}

.date-iso {
  font-size: 0.7rem;
  font-weight: 400;
  color: var(--p-text-muted-color, #6b7280);
}

.bd-section {
  margin-bottom: 1rem;
}

.bd-section-title {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin: 0 0 0.5rem;
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--p-text-muted-color, #6b7280);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.bd-count {
  display: inline-flex;
  align-items: center;
  padding: 0.1rem 0.45rem;
  border-radius: 999px;
  background: var(--p-primary-color);
  color: var(--text-on-p-primary);
  font-size: 0.7rem;
  font-weight: 700;
}

.bd-empty {
  margin: 0.25rem 0 0;
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
  font-style: italic;
}

.bd-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.tech-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.7rem 0.85rem;
  cursor: pointer;
  font-weight: 600;
  text-align: left;
  color: inherit;
  font: inherit;
}

.tech-avatar {
  width: 1.85rem;
  height: 1.85rem;
  border-radius: 50%;
  background: var(--p-primary-color);
  color: var(--text-on-p-primary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
}

.tech-name {
  flex: 1;
}

.job-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.unassigned-card {
  border-left: 3px solid #f59e0b;
}

.tech-job-card {
  margin-left: 0.5rem;
}

.job-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.job-customer {
  font-weight: 700;
  font-size: 1rem;
}

/* What the job IS — the discriminator when one customer has several jobs. */
.job-title-line {
  margin: 0;
  font-size: 0.85rem;
  font-weight: 500;
}

.job-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #6b7280);
}

.job-actions {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}

/* Threads */
.thread-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.thread-item {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.85rem 1rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.55rem;
  background: var(--p-content-background, #fff);
  cursor: pointer;
}

.thread-item:active {
  background: var(--p-content-hover-background, #f3f4f6);
}

.thread-item.has-unread {
  border-left: 4px solid #dc2626;
}

.thread-main {
  flex: 1;
}

.thread-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.4rem;
}

.thread-seen {
  font-size: 0.75rem;
  margin-top: 0.15rem;
  display: flex;
  align-items: center;
  gap: 0.3rem;
}

.thread-seen .pi {
  font-size: 0.7rem;
}

.thread-cust {
  font-size: 0.8rem;
  margin-top: 0.1rem;
}

.thread-time {
  font-size: 0.7rem;
  margin-top: 0.1rem;
}

/* Live */
.live-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.live-item {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.7rem 1rem;
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
}

.live-main {
  flex: 1;
}

.live-meta {
  font-size: 0.78rem;
  margin-top: 0.15rem;
}

/* States */
.state-msg {
  text-align: center;
  padding: 2.5rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.4rem;
}

.empty-icon {
  font-size: 2rem;
  opacity: 0.5;
}

.muted {
  color: var(--p-text-muted-color, #6b7280);
}

.w-full {
  width: 100%;
}

.form-stack {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.form-stack label {
  display: block;
  font-size: 0.85rem;
  font-weight: 500;
  margin-bottom: 0.2rem;
}
.return-visit-icon { color: var(--color-warning-500, #d97706); font-size: 0.8em; margin-right: 0.2rem; }
</style>
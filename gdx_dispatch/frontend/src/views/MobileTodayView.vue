<script setup>
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useGpsBreadcrumb } from '@/composables/useGpsBreadcrumb'
import MobileJobCard from '../components/MobileJobCard.vue'
import MobileReceiptCapture from '../components/MobileReceiptCapture.vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import SelectButton from 'primevue/selectbutton'
import Select from 'primevue/select'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import {
  countUnseenForJob,
} from '../composables/usePartsSeenCutoff'
import {
  isPushSupported,
  getCurrentPermission,
  subscribeToPush,
  ensureSubscribed,
  fetchVapidPublicKey,
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
const areaJobs = ref([])
const tech = ref(null)
const date = ref(null)
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

// 2026-07-16 — "today" is the tech's LOCAL calendar day. Without these
// params the backend fell back to the UTC date, so from 7pm local (UTC-5)
// the list silently rolled over to tomorrow and evening jobs vanished.
function localDayParams() {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const params = new URLSearchParams({
    date: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
  })
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
    if (tz) params.set('tz', tz)
  } catch { /* very old WebView — backend falls back to UTC */ }
  return params
}

// The route survives a cold remount with no signal.
//
// This screen held the day's route in memory only. That was survivable while a
// tech never left it — but every tap-through to a job and back is a remount,
// and with no signal `load()` threw, `jobs` stayed [], and the screen said
// "Nothing scheduled today". The tech loses the route mid-day, in the exact
// dead zone the offline queue exists for.
//
// Cached per tech+day so a stale route can never be shown as if it were
// another tech's or another day's. Writes are best-effort: a full or blocked
// localStorage must never break the load path.
// True while the screen is showing a cached route because the live fetch
// failed. Drives the "no signal" banner.
const fromCache = ref(false)

const ROUTE_CACHE_KEY = 'gdx_today_route_cache'

function cacheKeyFor(dayParams) {
  return `${ROUTE_CACHE_KEY}:${dayParams}`
}

// A cached route is a stand-in for signal, not an archive. Past this it is
// more likely to mislead than help — dispatch has had a working day to move
// things — so it expires rather than resurfacing days later as if current.
const ROUTE_CACHE_TTL_MS = 12 * 60 * 60 * 1000

function readRouteCache(key) {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed || !Array.isArray(parsed.jobs)) return null
    const at = Date.parse(parsed.cached_at || '')
    if (!Number.isNaN(at) && Date.now() - at > ROUTE_CACHE_TTL_MS) {
      localStorage.removeItem(key)
      return null
    }
    return parsed
  } catch {
    return null
  }
}

function writeRouteCache(key, data) {
  try {
    localStorage.setItem(key, JSON.stringify({
      jobs: data.jobs || [],
      area_jobs: data.area_jobs || [],
      tech_id: data.tech_id,
      date: data.date,
      cached_at: new Date().toISOString(),
    }))
  } catch {
    // Quota or private mode. A cache miss is a worse day, not a broken one.
  }
}

async function load(silent = false) {
  if (!silent) loading.value = true
  refreshing.value = silent
  error.value = null
  const params = localDayParams().toString()
  const key = cacheKeyFor(params)
  try {
    const data = await api.get(`/api/mobile/today?${params}`)
    jobs.value = data.jobs || []
    areaJobs.value = data.area_jobs || []
    tech.value = data.tech_id
    date.value = data.date
    fromCache.value = false
    writeRouteCache(key, data)
  } catch (err) {
    const cached = readRouteCache(key)
    if (cached) {
      // Show the route we last saw rather than an empty day. Labelled, because
      // a tech acting on a stale route must know it is stale.
      jobs.value = cached.jobs || []
      areaJobs.value = cached.area_jobs || []
      tech.value = cached.tech_id
      date.value = cached.date
      fromCache.value = true
      error.value = null
    } else {
      fromCache.value = false
      error.value = err?.message || "Couldn't load today's route"
    }
  } finally {
    loading.value = false
    refreshing.value = false
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
    const result = await api.postQueued('/api/mobile/today/reorder', { appointment_ids: ids }, {
      actionType: 'today.reorder',
    })
    if (result?.queued) {
      toast.add({ severity: 'warn', summary: 'Saved offline', detail: 'Reorder will sync when you reconnect.', life: 4000 })
    } else if (result?.changed) {
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



// Closeout is not on this screen any more. PR B moved every job action to the
// job detail screen; this view is the ROUTE — which stops, in what order, and
// how far apart. The closeout history that used to be recorded here now lives
// with the code that owns it, in MobileJobDetailView, and the guard that the
// legacy /api/mobile/jobs/{id}/complete endpoint stays unreachable moved with
// it (see MobileCloseoutOwnership.spec.js).
// One closeout path, one rule: capture-or-default at submit time.
const closeoutOpen = ref(false)
const closeoutJob = ref(null)



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

function _stateOf(a) {
  if (a.completed_at) return 'done'
  if (a.arrived_at) return 'on_site'
  if (a.en_route_at) return 'en_route'
  return 'assigned'
}

// ── Phase 1.3 parts (C1-C3, C6, C7) ──────────────────────────────────

const partsByJob = ref({})         // job_id -> array of parts
const partsLoading = ref({})        // job_id -> bool

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


function recomputeUnseen(jobId, partsList) {
  return countUnseenForJob(jobId, partsList)
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

// C4 fallback: on load, recompute the "dispatch answered your parts request"
// badge for every stop that has parts. The signal used to end in a toast saying
// "tap the parts row" — PR B removed that row, so the count now rides on the
// card itself and the toast points at the job instead. Losing the signal
// entirely was the alternative, and a tech who never learns dispatch ordered
// the spring is the reason it exists.
async function refreshAllUnseenCounts() {
  const targets = jobs.value.filter((j) => (j.parts_summary?.total || 0) > 0)
  await Promise.all(targets.map((j) => loadParts(j.id)))
  const total = targets.reduce((n, j) => n + (partsUnseenByJob.value[j.id] || 0), 0)
  if (total > 0) {
    toast.add({
      severity: 'info',
      summary: total === 1 ? 'Dispatch updated 1 part' : `Dispatch updated ${total} parts`,
      detail: 'Open the job to see what changed.',
      life: 5000,
    })
  }
}

onMounted(async () => {
  await load()
  refreshAllUnseenCounts()
  // E2: gate the "Enable notifications" CTA on browser support + perm.
  // 2026-08-04: two hard-won additions —
  //   * permission already granted: silently heal a missing subscription
  //     (VAPID keys landed on prod AFTER Doug's phone granted; the CTA
  //     never re-shows once perm != 'default', so without this the device
  //     is granted-but-unsubscribed forever).
  //   * permission still 'default': only render the CTA when the backend
  //     actually serves a VAPID key — otherwise the tap dead-ends in a
  //     "Push setup failed (no_vapid_key)" toast.
  if (isPushSupported() && getCurrentPermission() === 'granted') {
    ensureSubscribed(api).then((r) => {
      if (r.healed) console.info('[push] healed missing subscription')
    }).catch(() => { /* background heal — never surface */ })
  } else if (isPushSupported() && getCurrentPermission() === 'default') {
    fetchVapidPublicKey(api).then((key) => {
      if (key) refreshPushCta()
    }).catch(() => { /* no CTA when the key can't be fetched */ })
  }
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
          v-tooltip="'Dismiss'"
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
            <MobileReceiptCapture button-label="" />
            <SelectButton
              v-model="view"
              :options="VIEW_OPTIONS"
              optionLabel="label"
              optionValue="value"
              aria-label="View"
              :allowEmpty="false"
            />
            <Button
              v-tooltip="'Reorder'"
              v-if="view === VIEW_LIST && jobs.length > 1"
              icon="pi pi-sort-alt"
              text
              aria-label="Reorder"
              @click="enterReorderMode"
            />
            <Button
              v-tooltip="'Replay tour'"
              icon="pi pi-question-circle"
              text
              aria-label="Replay tour"
              @click="replayTour"
            />
            <Button
              v-tooltip="'Refresh'"
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

      <!-- No signal, showing the last route we saw. Says so plainly: a tech
           acting on a stale route must know it is stale. Without this the
           screen would be indistinguishable from a live one. -->
      <div v-if="fromCache" class="cached-banner" data-testid="mt-cached-route">
        <i class="pi pi-wifi" />
        <span>No signal — showing your last saved route. Pull refresh when you're back in range.</span>
      </div>

      <div v-if="loading && !refreshing" class="loading">Loading…</div>

      <div v-else-if="emptyState" class="empty">
        <i class="pi pi-calendar-times empty-icon" />
        <div class="empty-title">Nothing scheduled today</div>
        <div class="empty-help">Tap refresh above, or check with dispatch.</div>
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
        <li>
          <MobileJobCard
            :job="job"
            testid="mobile-route-job"
            :unseen-parts="partsUnseenByJob[job.id] || 0"
            @navigate="openMaps"
          >
            <!-- Route chrome stays with the route: the Jobs list has no stop
                 #4, and reorder is a property of the day, not of the job. -->
            <template #lead>
              <span v-if="!reorderMode" class="stop-num">{{ idx + 1 }}</span>
            </template>
          </MobileJobCard>
          <div v-if="reorderMode" class="reorder-controls">
            <Button
              v-tooltip="'Move up'"
              icon="pi pi-arrow-up"
              text rounded size="small"
              :disabled="idx === 0"
              aria-label="Move up"
              @click="moveJob(idx, -1)"
            />
            <Button
              v-tooltip="'Move down'"
              icon="pi pi-arrow-down"
              text rounded size="small"
              :disabled="idx === jobs.length - 1"
              aria-label="Move down"
              @click="moveJob(idx, 1)"
            />
          </div>
        </li>
        </template>
      </ol>

      <!-- 2026-07-16 — "do it when you're in the area" jobs: assigned,
           deliberately undated, still open. Dispatch leaves these without a
           scheduled date on purpose; they were invisible on mobile before. -->
      <div
        v-if="!loading && view === VIEW_LIST && areaJobs.length"
        class="area-section"
        data-testid="mobile-area-jobs"
      >
        <h2 class="area-heading">
          <i class="pi pi-map" />
          When you're in the area
          <span class="area-count">{{ areaJobs.length }}</span>
        </h2>
        <ol class="job-list">
          <li v-for="job in areaJobs" :key="job.id">
            <MobileJobCard :job="job" testid="mobile-area-job" @navigate="openMaps" />
          </li>
        </ol>
      </div>
    </section>
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
  background: var(--color-danger-bg);
  border: 1px solid var(--color-danger-border);
  border-left: 4px solid var(--color-danger-500);
  border-radius: 0.5rem;
  font-size: 0.85rem;
}
.offline-banner.banner-online {
  background: var(--color-warning-bg);
  border-color: var(--color-warning-border);
  border-left-color: var(--color-warning-500);
}
.offline-banner > .pi { color: var(--color-danger-500); font-size: 1.1rem; }
.offline-banner.banner-online > .pi { color: var(--color-warning-500); }
.offline-banner-text { flex: 1; line-height: 1.3; }
.offline-banner-text strong { display: block; font-size: 0.95rem; color: var(--p-text-color); }
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
/* The route's stop badge, rendered into the card's `lead` slot. Named
   .stop-num when the card was extracted; the old .job-stop-num rule below
   belonged to the deleted markup and matched nothing, so every stop number
   rendered unstyled. */
.stop-num {
  flex: 0 0 auto;
  min-width: 1.35rem; text-align: center;
  font-size: 0.8rem; font-weight: 700;
  color: var(--p-text-muted-color, #6b7280);
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
  background: var(--p-content-hover-background);
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
.job-site-label {
  font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.02em; border: 1px solid currentColor;
  border-radius: 4px; padding: 0.05rem 0.3rem; flex-shrink: 0;
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
  background: color-mix(in srgb, #eab308 15%, var(--p-content-background));
  border-left: 3px solid #eab308;
  color: var(--p-text-color);
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
  background: var(--p-content-hover-background);
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
  color: var(--p-text-muted-color);
  background: color-mix(in srgb, var(--p-content-background) 85%, transparent);
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
  background: var(--p-content-hover-background);
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
/* The inline complete-and-sign styles (.sig-form / .sig-canvas / .sig-clear)
   went with the dialog they styled; the whole action surface now lives on the
   job detail screen. */

/* 2026-07-16 — "when you're in the area" section (undated assigned jobs). */
.area-section { margin-top: 1.25rem; }
.area-heading {
  display: flex; align-items: center; gap: 0.4rem;
  margin: 0 0 0.6rem; font-size: 0.95rem; font-weight: 700;
  color: var(--p-text-muted-color, #6b7280);
}
.area-count {
  background: var(--p-content-border-color, #e5e7eb);
  color: var(--p-text-color, #1f2937);
  border-radius: 999px; padding: 0 0.5rem;
  font-size: 0.75rem; font-weight: 600; line-height: 1.4;
}
.area-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem; padding: 0.75rem 1rem;
  display: flex; flex-direction: column; gap: 0.35rem;
  color: inherit; text-decoration: none;
}
.area-card:active { background: var(--p-content-hover-background, #f3f4f6); }
.area-customer { font-size: 1rem; font-weight: 700; }
.area-chevron { color: var(--p-text-muted-color, #9ca3af); font-size: 0.8rem; }
.area-address {
  display: flex; align-items: center; gap: 0.35rem;
  font-size: 0.9rem; color: var(--p-primary-color, #2563eb);
}
.area-address span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.area-title { color: var(--p-text-muted-color, #6b7280); font-size: 0.85rem; }
.return-visit-tag { margin-left: 0.4rem; font-size: 0.65rem; vertical-align: middle; }
.cached-banner {
  display: flex; align-items: flex-start; gap: 0.5rem;
  padding: 0.55rem 0.75rem; border-radius: 0.5rem;
  font-size: 0.85rem; line-height: 1.35;
  color: var(--p-text-color, #111827);
  background: var(--p-content-hover-background, #f3f4f6);
  border-left: 3px solid var(--p-orange-500, #f97316);
}
</style>
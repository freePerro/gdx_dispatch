<template>
    <section class="mobile-timeclock">
      <header class="mobile-page-head">
        <h1>Time Clock</h1>
      </header>

      <!-- MH-7 (audit P1 #9): max-shift guard banners. The 8-16h
           prompt nudges a tech who forgot to clock out; the >=16h
           banner emphasizes a likely-stale session that needs manual
           review. Both render ABOVE the hero card so they're the
           first thing the tech sees if they open the app mid-shift. -->
      <div
        v-if="shouldShowMaxShiftBanner"
        class="banner banner-warning"
        data-test="mt-max-shift-banner"
        role="alert"
      >
        <i class="pi pi-exclamation-triangle" />
        <div>
          <strong>You've been clocked in {{ Math.floor(elapsedHours) }}+ hours.</strong>
          This likely needs manual review. Tap <em>Clock Out</em> below if you're done, or
          contact dispatch.
        </div>
      </div>
      <div
        v-else-if="shouldShowStillWorkingPrompt"
        class="banner banner-info"
        data-test="mt-still-working-prompt"
        role="status"
      >
        <i class="pi pi-clock" />
        <div class="banner-body">
          <strong>Still working?</strong> You've been clocked in
          {{ Math.floor(elapsedHours) }} hours.
        </div>
        <Button
          label="Yes, still working"
          size="small"
          data-test="mt-still-working-confirm"
          @click="dismissStillWorking"
        />
      </div>

      <!-- Status + clock-in/out hero card -->
      <div class="status-card" data-test="mt-status-card">
        <div class="status-line">
          <span class="status-label">Status</span>
          <Tag
            :value="statusLabel"
            :severity="statusSeverity"
            data-test="mt-status-tag"
          />
        </div>

        <div v-if="clockedIn" class="elapsed" data-test="mt-elapsed">{{ elapsedFormatted }}</div>
        <div v-if="clockedIn && clockInTime" class="since muted">
          Since {{ formatTime(clockInTime) }}
        </div>
        <div v-if="todayTotalHours != null" class="today-total" data-test="mt-today-total">
          Today: <strong>{{ todayTotalHours.toFixed(2) }}h</strong>
        </div>

        <!-- Job link selector — optional -->
        <div class="job-row">
          <label for="mt-job">Link to job (optional)</label>
          <Select
            id="mt-job"
            v-model="selectedJobId"
            :options="availableJobs"
            optionLabel="label"
            optionValue="value"
            placeholder="No job"
            :showClear="true"
            filter
            class="w-full"
            data-test="mt-job-select"
          />
        </div>

        <div class="gps-row" data-test="mt-gps">
          <i :class="gpsAvailable ? 'pi pi-map-marker' : 'pi pi-times-circle'" :style="{ color: gpsAvailable ? '#22c55e' : '#9ca3af' }" />
          <span class="muted">{{ gpsStatusText }}</span>
        </div>

        <!-- Big tap targets — Apple HIG / Material 48dp at minimum -->
        <div class="actions">
          <Button
            v-if="!clockedIn"
            label="Clock In"
            icon="pi pi-sign-in"
            severity="success"
            :loading="actionLoading"
            class="big-btn"
            data-test="mt-clock-in"
            @click="clockIn"
          />
          <template v-else>
            <Button
              label="Clock Out"
              icon="pi pi-sign-out"
              severity="danger"
              :loading="actionLoading"
              class="big-btn"
              data-test="mt-clock-out"
              @click="clockOut"
            />
            <Button
              v-if="!onBreak"
              label="Start Break"
              icon="pi pi-pause"
              severity="warn"
              :loading="breakLoading"
              class="big-btn"
              data-test="mt-break-start"
              @click="startBreak"
            />
            <Button
              v-else
              label="End Break"
              icon="pi pi-play"
              severity="info"
              :loading="breakLoading"
              class="big-btn"
              data-test="mt-break-end"
              @click="endBreak"
            />
          </template>
        </div>
      </div>

      <!-- Today's entries — card list (no DataTable on mobile) -->
      <section class="entries-section">
        <h2 class="section-title">Today's Entries</h2>
        <div v-if="entriesLoading" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
        </div>
        <ol v-else-if="todayEntries.length" class="card-list" data-test="mt-entries-list">
          <li
            v-for="e in todayEntries"
            :key="e.id || `${e.clock_in}-${e.entry_type}`"
            class="entry-card"
            data-test="mt-entry-row"
          >
            <div class="entry-top">
              <Tag
                :value="(e.entry_type || 'work').toUpperCase()"
                :severity="(e.entry_type || 'work') === 'break' ? 'warning' : 'info'"
              />
              <span class="entry-hours" v-if="e.hours != null">{{ Number(e.hours).toFixed(2) }}h</span>
            </div>
            <div class="entry-meta muted">
              <span v-if="e.clock_in">{{ formatTime(e.clock_in) }}</span>
              <span v-if="e.clock_out">→ {{ formatTime(e.clock_out) }}</span>
              <span v-if="!e.clock_out && e.entry_type !== 'break'">→ now</span>
            </div>
          </li>
        </ol>
        <div v-else class="state-msg muted">No entries yet today.</div>
      </section>
    </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

import Button from 'primevue/button'
import Select from 'primevue/select'
import Tag from 'primevue/tag'

const api = useApi()
const toast = useToast()

const clockedIn = ref(false)
const onBreak = ref(false)
const clockInTime = ref(null)
const todayTotalHours = ref(null)
const elapsedSeconds = ref(0)
let elapsedTimer = null

// MH-7 (audit P1 #9): max-shift guard metadata from the status response.
// Defaults match the backend so the warning still fires if the response
// is missing the fields (older deploys + jsdom test mounts).
const warningAfterHours = ref(8)
const maxShiftHours = ref(16)
const stillWorkingDismissed = ref(false)

const entries = ref([])
const entriesLoading = ref(false)
const availableJobs = ref([])
const selectedJobId = ref(null)

const actionLoading = ref(false)
const breakLoading = ref(false)

const gpsCoords = ref(null)
const gpsAvailable = computed(() => gpsCoords.value != null)
const gpsStatusText = computed(() => (gpsAvailable.value ? 'GPS available' : 'GPS unavailable'))

const statusLabel = computed(() => {
  if (!clockedIn.value) return 'Clocked Out'
  if (onBreak.value) return 'On Break'
  return 'Clocked In'
})

const statusSeverity = computed(() => {
  if (!clockedIn.value) return 'secondary'
  if (onBreak.value) return 'warning'
  return 'success'
})

const elapsedFormatted = computed(() => {
  const total = elapsedSeconds.value
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
})

function startElapsedTimer() {
  if (elapsedTimer) return
  if (clockInTime.value) {
    elapsedSeconds.value = Math.max(0, Math.floor((Date.now() - new Date(clockInTime.value).getTime()) / 1000))
  } else {
    elapsedSeconds.value = 0
  }
  elapsedTimer = setInterval(() => {
    elapsedSeconds.value += 1
  }, 1000)
}

function stopElapsedTimer() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer)
    elapsedTimer = null
  }
  elapsedSeconds.value = 0
}

// MH-7 prompts. elapsedHours derives from the live ticker so the
// prompt appears as the boundary is crossed without a refresh.
const elapsedHours = computed(() => elapsedSeconds.value / 3600)
const shouldShowStillWorkingPrompt = computed(() =>
  clockedIn.value
  && !stillWorkingDismissed.value
  && elapsedHours.value >= warningAfterHours.value
  && elapsedHours.value < maxShiftHours.value,
)
const shouldShowMaxShiftBanner = computed(() =>
  clockedIn.value && elapsedHours.value >= maxShiftHours.value,
)
function dismissStillWorking() {
  stillWorkingDismissed.value = true
  // Re-arm at the next hour boundary so a tech who confirms at 8h gets
  // re-prompted around 9h+, not silently 16-hour-warning-only.
  setTimeout(() => { stillWorkingDismissed.value = false }, 60 * 60 * 1000)
}

const todayEntries = computed(() => {
  const today = new Date().toISOString().slice(0, 10)
  return entries.value.filter((e) => {
    const ts = String(e.clock_in || e.clock_in_at || '')
    return ts.slice(0, 10) === today
  })
})

function formatTime(t) {
  if (!t) return ''
  try {
    return new Date(t).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  } catch {
    return String(t)
  }
}

function initGps() {
  if (!('geolocation' in navigator)) return
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      gpsCoords.value = { latitude: pos.coords.latitude, longitude: pos.coords.longitude }
    },
    () => {
      gpsCoords.value = null
    },
    { timeout: 2000, maximumAge: 60_000 },
  )
}

async function refreshGps() {
  return new Promise((resolve) => {
    if (!('geolocation' in navigator)) return resolve()
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        gpsCoords.value = { latitude: pos.coords.latitude, longitude: pos.coords.longitude }
        resolve()
      },
      () => resolve(),
      { timeout: 2000, maximumAge: 30_000 },
    )
  })
}

async function gpsWithTimeout(ms = 2000) {
  await Promise.race([refreshGps(), new Promise((r) => setTimeout(r, ms))])
}

function buildGpsBody() {
  const body = {}
  if (gpsCoords.value) {
    body.latitude = gpsCoords.value.latitude
    body.longitude = gpsCoords.value.longitude
  }
  return body
}

async function fetchStatus() {
  try {
    const data = await api.get('/api/timeclock/status')
    const status = data?.data || data
    const entry = status?.active_entry || null
    clockedIn.value = !!status?.clocked_in
    onBreak.value = !!status?.on_break || entry?.entry_type === 'break'
    clockInTime.value = entry?.clock_in_at || status?.clock_in_time || null
    todayTotalHours.value = status?.today_hours ?? null
    // MH-7 guard metadata. Older backends without these fields fall
    // through to the defaults set on the ref (8h / 16h) — matches the
    // backend literal.
    if (typeof status?.warning_after_hours === 'number') {
      warningAfterHours.value = status.warning_after_hours
    }
    if (typeof status?.max_shift_hours === 'number') {
      maxShiftHours.value = status.max_shift_hours
    }
    if (clockedIn.value) {
      startElapsedTimer()
    } else {
      stopElapsedTimer()
      // Reset the dismissal so a new shift gets a fresh prompt arc.
      stillWorkingDismissed.value = false
    }
  } catch {
    clockedIn.value = false
    onBreak.value = false
  }
}

async function fetchEntries() {
  entriesLoading.value = true
  try {
    const data = await api.get('/api/timeclock/entries')
    const payload = data?.data || data
    const raw = Array.isArray(payload) ? payload : payload?.items || []
    entries.value = raw.map((e) => ({
      ...e,
      clock_in: e.clock_in || e.clock_in_at || null,
      clock_out: e.clock_out || e.clock_out_at || null,
      hours:
        e.hours != null
          ? e.hours
          : e.minutes != null
            ? Number((e.minutes / 60).toFixed(2))
            : null,
    }))
  } catch {
    entries.value = []
  } finally {
    entriesLoading.value = false
  }
}

async function fetchJobs() {
  try {
    const data = await api.get('/api/jobs')
    const rows = Array.isArray(data) ? data : data?.items || data?.data || []
    availableJobs.value = rows.map((j) => ({
      label: `${j.customer_name || j.customer || 'Job'} - ${j.job_type || j.type || 'Service'}`,
      value: j.id,
    }))
  } catch {
    availableJobs.value = []
  }
}

async function clockIn() {
  actionLoading.value = true
  try {
    await gpsWithTimeout()
    const body = buildGpsBody()
    if (selectedJobId.value) body.job_id = selectedJobId.value
    await api.post('/api/timeclock/clock-in', body)
    toast.add({ severity: 'success', summary: 'Clocked In', life: 2500 })
    await Promise.all([fetchStatus(), fetchEntries()])
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Clock-in failed', detail: err?.message, life: 4000 })
  } finally {
    actionLoading.value = false
  }
}

async function clockOut() {
  actionLoading.value = true
  try {
    await gpsWithTimeout()
    const body = buildGpsBody()
    await api.post('/api/timeclock/clock-out', body)
    toast.add({ severity: 'success', summary: 'Clocked Out', life: 2500 })
    await Promise.all([fetchStatus(), fetchEntries()])
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Clock-out failed', detail: err?.message, life: 4000 })
  } finally {
    actionLoading.value = false
  }
}

async function startBreak() {
  breakLoading.value = true
  try {
    await api.post('/api/timeclock/break/start', {})
    onBreak.value = true
    toast.add({ severity: 'info', summary: 'Break started', life: 2500 })
    await Promise.all([fetchStatus(), fetchEntries()])
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Break start failed', detail: err?.message, life: 4000 })
  } finally {
    breakLoading.value = false
  }
}

async function endBreak() {
  breakLoading.value = true
  try {
    await api.post('/api/timeclock/break/end', {})
    onBreak.value = false
    toast.add({ severity: 'info', summary: 'Break ended', life: 2500 })
    await Promise.all([fetchStatus(), fetchEntries()])
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Break end failed', detail: err?.message, life: 4000 })
  } finally {
    breakLoading.value = false
  }
}

onMounted(() => {
  initGps()
  Promise.all([fetchStatus(), fetchEntries(), fetchJobs()])
})

onUnmounted(() => {
  stopElapsedTimer()
})
</script>

<style scoped>
.mobile-timeclock {
  padding: 0.75rem 0.75rem calc(5rem + env(safe-area-inset-bottom));
  max-width: 800px;
  margin: 0 auto;
}

.mobile-page-head {
  margin-bottom: 0.75rem;
}
.mobile-page-head h1 {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}

.status-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem;
  padding: 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  margin-bottom: 1rem;
}

.status-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}
.status-label {
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 600;
}

.elapsed {
  font-family: monospace;
  font-size: 1.8rem;
  font-weight: 800;
  text-align: center;
  color: var(--p-primary-color, #2563eb);
}
.since {
  text-align: center;
  font-size: 0.8rem;
}

.today-total {
  text-align: center;
  font-size: 0.95rem;
}

.job-row {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.job-row label {
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 500;
}

.gps-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.8rem;
}

.actions {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-top: 0.4rem;
}
.actions :deep(.p-button.big-btn),
.big-btn {
  min-height: 56px;
  font-size: 1rem;
  font-weight: 700;
}

.entries-section {
  margin-top: 0.5rem;
}
.section-title {
  margin: 0 0 0.5rem;
  font-size: 1rem;
  font-weight: 700;
}

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.entry-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  padding: 0.6rem 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.entry-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}
.entry-hours {
  font-family: monospace;
  font-weight: 700;
  font-size: 0.95rem;
}
.entry-meta {
  font-size: 0.8rem;
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}

.state-msg {
  text-align: center;
  padding: 1.5rem 1rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.4rem;
}

.muted { color: var(--p-text-muted-color, #6b7280); }
.w-full { width: 100%; }

/* MH-7 (audit P1 #9): max-shift guard banners. Both render at the top
   of the page above the status card. Brand-blue info; amber-red warning
   for the >=16h case so a tech in glove-and-sunlight conditions can't
   miss it. */
.banner {
  display: flex;
  align-items: flex-start;
  gap: 0.6rem;
  padding: 0.75rem 0.9rem;
  border-radius: 0.625rem;
  margin-bottom: 0.6rem;
  font-size: 0.9rem;
  line-height: 1.35;
}
.banner i {
  font-size: 1.2rem;
  margin-top: 0.1rem;
}
.banner-body {
  flex: 1 1 auto;
  min-width: 0;
}
.banner-info {
  background: rgba(37, 99, 235, 0.10);
  border: 1px solid rgba(37, 99, 235, 0.35);
  color: var(--text-primary, inherit);
}
.banner-info i {
  color: #2563eb;
}
.banner-warning {
  background: rgba(220, 38, 38, 0.10);
  border: 1px solid rgba(220, 38, 38, 0.45);
  color: var(--text-primary, inherit);
}
.banner-warning i {
  color: #dc2626;
}
[data-theme="dark"] .banner-info {
  background: rgba(37, 99, 235, 0.18);
  border-color: rgba(37, 99, 235, 0.55);
}
[data-theme="dark"] .banner-warning {
  background: rgba(220, 38, 38, 0.18);
  border-color: rgba(220, 38, 38, 0.55);
}
</style>

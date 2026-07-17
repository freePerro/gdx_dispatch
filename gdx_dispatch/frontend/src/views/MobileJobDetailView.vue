<template>
  <section class="mobile-job-detail">
    <header class="detail-head">
      <Button
        icon="pi pi-arrow-left"
        text
        rounded
        aria-label="Back"
        data-testid="mobile-job-detail-back"
        @click="goBack"
      />
      <h1>Job details</h1>
    </header>

    <div v-if="loading" class="state-msg">
      <i class="pi pi-spin pi-spinner" />
      <span>Loading…</span>
    </div>
    <div v-else-if="error" class="state-msg state-msg-error">
      <i class="pi pi-exclamation-triangle" />
      <span>{{ error }}</span>
      <Button label="Retry" size="small" outlined @click="load" />
    </div>

    <template v-else-if="job">
      <div class="detail-card">
        <div class="detail-row detail-row-top">
          <div class="detail-customer" data-testid="mobile-job-detail-customer">
            {{ customer?.name || '—' }}
          </div>
          <span :class="['status-pill', `status-${(job.dispatch_status || 'assigned').replace(' ', '_')}`]">
            {{ statusLabel(job.dispatch_status) }}
          </span>
        </div>
        <div v-if="job.title" class="detail-title">{{ job.title }}</div>
        <div v-if="job.scheduled_at" class="detail-meta">
          <i class="pi pi-calendar" />
          {{ formatScheduled(job.scheduled_at) }}
        </div>
        <div v-else class="detail-meta detail-meta-muted">
          <i class="pi pi-calendar" />
          No date — do when in the area
        </div>
      </div>

      <div class="detail-card">
        <h2>Customer</h2>
        <a
          v-if="customer?.phone"
          class="contact-row"
          :href="`tel:${customer.phone}`"
          data-testid="mobile-job-detail-phone"
        >
          <i class="pi pi-phone" />
          <span>{{ customer.phone }}</span>
        </a>
        <a
          v-if="customer?.address"
          class="contact-row"
          :href="navigationLink"
          target="_blank"
          rel="noopener"
          data-testid="mobile-job-detail-address"
        >
          <i class="pi pi-map-marker" />
          <span>{{ customer.address }}</span>
        </a>
        <div v-if="!customer?.phone && !customer?.address" class="detail-meta detail-meta-muted">
          No contact info on file — ask dispatch.
        </div>
      </div>

      <div v-if="job.description" class="detail-card">
        <h2>Description</h2>
        <p class="detail-description">{{ job.description }}</p>
      </div>

      <div v-if="notes.length" class="detail-card">
        <h2>Notes</h2>
        <ul class="note-list">
          <li v-for="n in notes" :key="n.id">
            <div class="note-body">{{ n.note }}</div>
            <div class="note-when">{{ formatScheduled(n.created_at) }}</div>
          </li>
        </ul>
      </div>

      <div class="detail-card">
        <div class="photo-head">
          <h2>Photos</h2>
          <span v-if="pendingPhotos" class="photo-pending" data-testid="mjd-photo-pending">
            <i class="pi pi-cloud-upload" />
            {{ pendingPhotos }} waiting for signal
          </span>
        </div>

        <div v-if="photos.length" class="photo-strip">
          <a
            v-for="p in photos"
            :key="p.id"
            :href="p.url"
            target="_blank"
            rel="noopener"
            class="photo-thumb"
          >
            <img v-if="p.url" :src="p.url" :alt="p.caption || p.filename || 'Job photo'" loading="lazy" />
            <span v-else class="photo-name">{{ p.filename || 'Photo' }}</span>
          </a>
        </div>
        <div v-else class="detail-meta detail-meta-muted">No photos yet.</div>

        <!-- The tenant can require a slot. When it's optional the server
             defaults to "during", so don't make the tech choose for nothing. -->
        <div v-if="photoSlotRequired" class="photo-kinds" data-testid="mjd-photo-kinds">
          <Button
            v-for="k in PHOTO_KINDS"
            :key="k"
            :label="k"
            size="small"
            :outlined="photoKind !== k"
            :severity="photoKind === k ? 'primary' : 'secondary'"
            :data-testid="`mjd-photo-kind-${k}`"
            @click="photoKind = k"
          />
        </div>

        <!-- A real file input, not a Button — only an input can open the
             camera. Deliberately NO `capture` attribute: Android honours it by
             forcing a single shot straight to the lens, which kills `multiple`
             AND locks the tech out of the gallery, so a photo taken before the
             app was open can never be attached. Bare accept="image/*" makes
             Android offer Camera or Files, which is both. -->
        <label class="photo-add" data-testid="mjd-photo-add">
          <input
            ref="photoInput"
            type="file"
            accept="image/*"
            multiple
            @change="onPhotoPicked"
          />
          <span>
            <i class="pi pi-camera" />
            {{ photoBusy ? 'Saving…' : 'Add photo' }}
          </span>
        </label>
        <small v-if="photoSlotRequired && !photoKind" class="photo-hint">
          Pick before / during / after first.
        </small>
      </div>

      <!-- Time is shown, never edited here. Arriving starts the job clock and
           completing ends it; that path is the one PR #154 actually guards. A
           Stop button would close the timer and switch that guard off — an
           attested 2h job then bills 5h. Read-only until that is fixed and
           proven on Postgres. -->
      <div v-if="job.arrived_at" class="detail-card">
        <h2>Time</h2>
        <div class="detail-meta" data-testid="mobile-job-detail-timer">
          <i class="pi pi-clock" />
          <!-- Deliberately NOT "arrived → completed". A job arrived at in May
               and closed out in July spans two months, and labelling that span
               "tracked" reads as two months of work — the hours the tech
               actually attested at closeout were 1.5. Never imply a duration
               from two stamps that don't bound the work. -->
          <span v-if="job.completed_at">
            Arrived {{ formatScheduled(job.arrived_at) }} · closed out {{ formatScheduled(job.completed_at) }}
          </span>
          <span v-else>Tracking since you arrived, {{ formatScheduled(job.arrived_at) }}</span>
        </div>
        <div class="detail-meta detail-meta-muted">
          Hours for this job come from what you entered at close-out. Your paid
          hours come from the day clock.
        </div>
      </div>

      <!-- Sticky so the tech can act without scrolling a long job. -->
      <div class="action-bar" data-testid="mobile-job-detail-actions">
        <Button
          v-if="canGoEnRoute"
          label="On my way"
          icon="pi pi-send"
          :loading="advancing"
          data-testid="mjd-en-route"
          @click="onMyWay"
        />
        <Button
          v-if="job.dispatch_status === 'en_route'"
          label="I'm here"
          icon="pi pi-map-marker"
          :loading="advancing"
          data-testid="mjd-arrived"
          @click="imHere"
        />
        <Button
          v-if="job.dispatch_status === 'on_site'"
          label="Complete"
          icon="pi pi-check"
          severity="success"
          data-testid="mjd-complete"
          @click="closeoutOpen = true"
        />
        <Button
          v-if="canBill"
          label="Bill / collect"
          icon="pi pi-receipt"
          severity="secondary"
          data-testid="mjd-bill"
          @click="invoiceOpen = true"
        />
        <Button
          v-if="job.navigation_link"
          label="Navigate"
          icon="pi pi-directions"
          severity="secondary"
          outlined
          data-testid="mjd-navigate"
          @click="openMaps"
        />
      </div>

      <MobileJobCloseoutDialog
        v-model:visible="closeoutOpen"
        :job-id="String(job.id)"
        :job-title="job.title || ''"
        :customer-name="customer?.name || ''"
        @closed-out="onCloseoutDone"
      />
      <MobileInvoiceDialog
        v-model:visible="invoiceOpen"
        :job="job"
        @invoiced="refresh"
      />
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { usePhotoQueue } from '../composables/usePhotoQueue'
import MobileJobCloseoutDialog from '../components/MobileJobCloseoutDialog.vue'
import MobileInvoiceDialog from '../components/MobileInvoiceDialog.vue'

const api = useApi()
const toast = useToast()
const route = useRoute()
const router = useRouter()
const { pendingPhotos, capturePhoto } = usePhotoQueue()

const PHOTO_KINDS = ['before', 'during', 'after']
const photoInput = ref(null)
const photoKind = ref(null)
const photoBusy = ref(false)
const photoSlotRequired = ref(false)

const loading = ref(true)
const error = ref(null)
const job = ref(null)
const notes = ref([])
const photos = ref([])
const advancing = ref(false)
const closeoutOpen = ref(false)
const invoiceOpen = ref(false)

// The customer rides on the job (same shape the Today cards read), so the
// actions can reach job.customer without caring which screen mounted them.
const customer = computed(() => job.value?.customer || null)

async function load() {
  loading.value = true
  error.value = null
  try {
    const r = await api.get(`/api/mobile/job/${route.params.id}`)
    job.value = r?.job || null
    notes.value = r?.notes || []
    photos.value = r?.photos || []
    if (!job.value) error.value = 'Job not found'
  } catch (err) {
    // The ownership gate 404s jobs that aren't yours — same message either way.
    error.value = err?.status === 404 ? 'Job not found' : (err?.message || 'Could not load job')
  } finally {
    loading.value = false
  }
}

// After an action, NOT on first paint. A refetch that fails must never take the
// job off the screen: `error` out-ranks `job` in the template, so routing a
// dead-zone refetch through load() means the tech taps "On my way", is told
// "Saved offline", and then watches the job vanish — the write succeeded and
// the screen broke anyway. Keep what we have; the queue will drain later.
async function refresh() {
  try {
    const r = await api.get(`/api/mobile/job/${route.params.id}`)
    if (r?.job) {
      job.value = r.job
      notes.value = r.notes || []
      photos.value = r.photos || []
    }
  } catch {
    // Offline or a blip. The queued write still lands on reconnect.
  }
}

const navigationLink = computed(() => job.value?.navigation_link || null)

const canGoEnRoute = computed(() => {
  const s = job.value?.dispatch_status
  return !s || s === 'assigned' || s === 'unassigned'
})

// Today's guards are dispatch_status-only, which is safe there because Today is
// only ever today. This screen opens ANY job, including one invoiced in April —
// a status-only guard would cheerfully offer to bill it again.
//
// `billed` is derived server-side from real invoices. Do NOT reach for
// job.billing_status: it looks like the answer and is a dead column that only
// ever says "unbilled" (core/billing_predicates.py).
// Requires an explicit false: if the server didn't say, we don't know, and
// inviting a second invoice is the one mistake here that costs money.
const canBill = computed(() => {
  const j = job.value
  return Boolean(j) && j.dispatch_status === 'done' && j.billed === false
})

function openMaps() {
  if (navigationLink.value) window.open(navigationLink.value, '_blank', 'noopener')
}

// Queued, not posted: a tech taps these in driveways and dead zones. postQueued
// lands the row locally and drains on reconnect; a 4xx still throws (a real
// answer is not an outage).
async function advance(path, body, actionType, okMsg) {
  advancing.value = true
  try {
    const r = await api.postQueued(`/api/mobile/jobs/${job.value.id}/${path}`, body, {
      actionType, resourceId: String(job.value.id),
    })
    if (r?.queued) {
      toast.add({ severity: 'warn', summary: 'Saved offline', detail: 'Sends when you have signal', life: 3000 })
    } else {
      toast.add({ severity: 'success', summary: okMsg, life: 2000 })
    }
    // Refetch rather than guess the new state locally — Today flips the status
    // before checking the result and never rolls it back on failure, so its
    // card can show "en route" while an error toast fires. Don't copy that.
    await refresh()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not save', detail: err?.message || '', life: 4000 })
  } finally {
    advancing.value = false
  }
}

function onMyWay() {
  return advance('en-route', {}, 'job.en_route', 'On my way')
}

async function imHere() {
  return advance('arrived', await currentPosition(), 'job.arrived', "You're here")
}

// Best-effort: a tech in a metal building may never get a fix, and arriving
// must not depend on it.
function currentPosition() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve({})
    const done = (v) => resolve(v)
    const timer = setTimeout(() => done({}), 3000)
    navigator.geolocation.getCurrentPosition(
      (p) => { clearTimeout(timer); done({ lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy }) },
      () => { clearTimeout(timer); done({}) },
      { timeout: 3000 },
    )
  })
}

function onCloseoutDone() {
  closeoutOpen.value = false
  refresh()
}

// Cached, because this is a network GET that fails precisely when the tech is
// offline — which is the whole use case. Failing open there would hide the slot
// picker, send kind=null, and the server would 400 the photo hours later at
// drain time, long after the tech drove away. Remember the last known answer.
const SLOT_CACHE_KEY = 'gdx_photo_slot_required'

async function loadPhotoSettings() {
  try {
    photoSlotRequired.value = localStorage.getItem(SLOT_CACHE_KEY) === '1'
  } catch { /* private mode */ }
  try {
    const r = await api.get('/api/me/tech-mobile-settings')
    const required =
      (r?.settings || {})['tech_mobile.photo_slot_tagging'] === 'required'
    photoSlotRequired.value = required
    try { localStorage.setItem(SLOT_CACHE_KEY, required ? '1' : '0') } catch { /* ignore */ }
  } catch {
    // Offline or unreachable: keep whatever the cache said rather than
    // guessing "optional" and setting the tech up for a rejected upload.
  }
}

async function onPhotoPicked(e) {
  const files = Array.from(e?.target?.files || [])
  if (!files.length) return
  if (photoSlotRequired.value && !photoKind.value) {
    toast.add({ severity: 'warn', summary: 'Pick before / during / after first', life: 3000 })
    if (photoInput.value) photoInput.value.value = ''
    return
  }

  photoBusy.value = true
  let queued = 0
  try {
    for (const f of files) {
      const r = await capturePhoto(job.value.id, f, photoKind.value)
      if (r?.queued) queued += 1
    }
    // The photo is SAVED either way — that's the point of storing the blob
    // before uploading. Say which happened; "Uploaded" when it's sitting in
    // IndexedDB is the lie that makes a tech re-shoot a door.
    if (queued) {
      toast.add({
        severity: 'warn',
        summary: queued === files.length ? 'Saved on your phone' : 'Some saved on your phone',
        detail: 'Uploads when you have signal',
        life: 3500,
      })
    } else {
      toast.add({ severity: 'success', summary: files.length > 1 ? 'Photos added' : 'Photo added', life: 2000 })
    }
    // The 201 carries no url — the strip can only render after a refetch.
    await refresh()
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: err?.code === 'photo_backlog_full' ? 'Too many photos waiting' : 'Could not save photo',
      detail: err?.code === 'photo_backlog_full'
        ? 'Get some signal so these upload before adding more.'
        : (err?.message || ''),
      life: 5000,
    })
  } finally {
    photoBusy.value = false
    // Let the same file be picked again (Chrome won't re-fire change otherwise).
    if (photoInput.value) photoInput.value.value = ''
  }
}

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/mobile/jobs')
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

function formatScheduled(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString([], {
      weekday: 'short', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    })
  } catch { return iso }
}

onMounted(() => {
  load()
  // Not awaited: it only decides whether the slot picker shows, and the job
  // must render even if settings are unreachable.
  loadPhotoSettings()
})
</script>

<style scoped>
.mobile-job-detail { padding: 0.75rem; max-width: 800px; margin: 0 auto; display: flex; flex-direction: column; gap: 0.6rem; }
.detail-head { display: flex; align-items: center; gap: 0.25rem; }
.detail-head h1 { margin: 0; font-size: 1.25rem; font-weight: 700; }
.detail-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem; padding: 0.85rem 1rem;
  display: flex; flex-direction: column; gap: 0.45rem;
}
.detail-card h2 { margin: 0; font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--p-text-muted-color, #6b7280); }
.detail-row { display: flex; align-items: center; gap: 0.5rem; }
.detail-row-top { justify-content: space-between; }
.detail-customer { font-size: 1.1rem; font-weight: 700; }
.detail-title { font-size: 0.95rem; }
.detail-meta { color: var(--p-text-muted-color, #6b7280); font-size: 0.9rem; display: flex; align-items: center; gap: 0.35rem; }
.detail-meta-muted { font-style: italic; }
.detail-description { margin: 0; white-space: pre-wrap; font-size: 0.95rem; }
.contact-row {
  display: flex; align-items: center; gap: 0.5rem;
  color: var(--p-primary-color, #2563eb); text-decoration: none;
  font-size: 0.95rem; padding: 0.25rem 0;
}
.note-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.note-body { font-size: 0.95rem; white-space: pre-wrap; }
.note-when { font-size: 0.75rem; color: var(--p-text-muted-color, #9ca3af); }
.photo-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.photo-pending {
  display: inline-flex; align-items: center; gap: 0.3rem;
  font-size: 0.75rem; font-weight: 600;
  color: var(--p-amber-600, #b45309);
}
.photo-kinds { display: flex; gap: 0.4rem; }
.photo-kinds :deep(.p-button) { flex: 1; min-height: 44px; text-transform: capitalize; }
/* A styled <label> wrapping a hidden file input — capture="environment" is
   what jumps straight to the back camera, and only a real input gets that. */
.photo-add {
  display: flex; align-items: center; justify-content: center;
  min-height: 44px; border-radius: 0.5rem; cursor: pointer;
  border: 1px dashed var(--p-content-border-color, #d1d5db);
  color: var(--p-primary-color, #2563eb);
  font-size: 0.95rem; font-weight: 600;
}
.photo-add input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.photo-add span { display: inline-flex; align-items: center; gap: 0.4rem; }
.photo-hint { color: var(--p-text-muted-color, #6b7280); font-size: 0.75rem; }
.photo-strip { display: flex; gap: 0.5rem; overflow-x: auto; }
.photo-thumb { flex: 0 0 auto; width: 96px; height: 96px; border-radius: 0.4rem; overflow: hidden; border: 1px solid var(--p-content-border-color, #e5e7eb); display: flex; align-items: center; justify-content: center; }
.photo-thumb img { width: 100%; height: 100%; object-fit: cover; }
.photo-name { font-size: 0.7rem; padding: 0.25rem; word-break: break-all; }
.status-pill {
  display: inline-flex; align-items: center; gap: 0.3rem;
  padding: 0.2rem 0.55rem; border-radius: 999px;
  font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em;
  border: 1px solid transparent;
}
.status-assigned { background: #475569; color: #fff; }
.status-unassigned { background: #6b7280; color: #fff; }
.status-en_route { background: #f59e0b; color: #1f2937; }
.status-on_site { background: #2563eb; color: #fff; }
.status-done { background: #15803d; color: #fff; }
.state-msg {
  text-align: center; padding: 2rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
  display: flex; flex-direction: column; align-items: center; gap: 0.5rem;
}
.state-msg-error { color: #b91c1c; }
/* Sticky so a long job doesn't hide the actions. 44px is the tap-target floor
   from e2e/mobile-touch-targets.spec.js, which now opens the first job and
   walks this screen too — it previously only covered param-less routes, which
   is how the screen a tech works from went uncovered. */
.action-bar {
  position: sticky; bottom: 0; z-index: 5;
  display: flex; flex-wrap: wrap; gap: 0.5rem;
  padding: 0.6rem; margin: 0 -0.75rem -0.75rem;
  background: var(--p-content-background, #fff);
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}
.action-bar:empty { display: none; }
.action-bar :deep(.p-button) { flex: 1 1 auto; min-height: 44px; }
</style>

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

      <!-- Always rendered, never `v-if="notes.length"`: the tech with nothing
           written yet is exactly the one who needs somewhere to write. -->
      <div class="detail-card">
        <h2>Notes</h2>
        <ul v-if="notes.length" class="note-list">
          <li v-for="n in notes" :key="n.id">
            <div class="note-body">{{ n.note }}</div>
            <div class="note-when">
              <span v-if="n._failed" class="failed-flag">
                <i class="pi pi-exclamation-triangle" /> didn't send — tap Add note to retry
              </span>
              <span v-else-if="n._pending" class="pending-flag">
                <i class="pi pi-cloud-upload" /> waiting for signal
              </span>
              <span v-else>
                <!-- Who wrote it matters: more than one tech works a job, and
                     "who found the frayed cable" is the next question. Omitted
                     entirely rather than shown as "Unknown" when we genuinely
                     don't know — the office screen's `|| 'Unknown'` read as a
                     display default and hid the fact that NOT ONE note in
                     production had an author recorded. -->
                <span v-if="n.author_name" class="note-author">{{ n.author_name }}</span>
                <span v-if="n.author_name"> · </span>
                <span>{{ formatScheduled(n.created_at) }}</span>
              </span>
            </div>
          </li>
        </ul>
        <div v-else class="detail-meta detail-meta-muted">No notes yet.</div>

        <Textarea
          v-model="noteDraft"
          rows="2"
          auto-resize
          placeholder="What did you find?"
          data-testid="mjd-note-input"
        />
        <Button
          label="Add note"
          icon="pi pi-plus"
          size="small"
          :loading="noteBusy"
          :disabled="!noteDraft.trim()"
          data-testid="mjd-note-add"
          @click="addNote"
        />
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
          <!-- AuthedImage, not a bare <img>: the url needs a Bearer token
               and an <img src> can't send one — it 401s and paints a broken
               icon, which is exactly what a real phone showed. -->
          <div v-for="p in photos" :key="p.id" class="photo-thumb">
            <AuthedImage :src="p.url" :alt="p.caption || p.filename || 'Job photo'">
              <template #fallback>
                <span class="photo-name">{{ p.filename || 'Photo' }}</span>
              </template>
            </AuthedImage>
          </div>
        </div>
        <div v-else class="detail-meta detail-meta-muted">No photos yet.</div>

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
      </div>

      <div class="detail-card">
        <h2>Parts</h2>
        <ul v-if="parts.length" class="part-list" data-testid="mjd-part-list">
          <li v-for="p in parts" :key="p.id">
            <div class="part-main">
              <span class="part-name">{{ p.part_name }}</span>
              <span class="part-qty">×{{ p.quantity || 1 }}</span>
            </div>
            <div class="part-meta">
              <span v-if="p.sku" class="part-sku">{{ p.sku }}</span>
              <span v-if="p.urgency === 'urgent'" class="part-urgent">urgent</span>
              <span v-if="p._failed" class="failed-flag">
                <i class="pi pi-exclamation-triangle" /> didn't send
              </span>
              <span v-else-if="p._pending" class="pending-flag">
                <i class="pi pi-cloud-upload" /> waiting for signal
              </span>
              <span v-else class="part-status">{{ p.status || 'needed' }}</span>
            </div>
          </li>
        </ul>
        <div v-else class="detail-meta detail-meta-muted">No parts requested yet.</div>

        <div class="part-add">
          <!-- Search is the same catalog the estimate builder searches. Typing
               is never blocked on it: offline, or for a part nobody has
               catalogued, the free-text name is submitted with sku=null and the
               request still reaches dispatch. -->
          <!-- maxlength mirrors JobPartNeeded.part_name String(200). Without
               it a long free-text name 422s — loudly online, and SILENTLY
               offline, where the queue marks it failed and the request is
               simply gone. Stop it at the keyboard instead. -->
          <InputText
            v-model="partQuery"
            maxlength="200"
            placeholder="Search parts, or just type a name"
            data-testid="mjd-part-search"
            @input="onPartQuery"
          />
          <ul v-if="partSuggestions.length" class="suggest-list" data-testid="mjd-part-suggestions">
            <li v-for="s in partSuggestions" :key="`${s.source}-${s.sku}-${s.name}`">
              <button type="button" @click="pickPart(s)">
                <span class="suggest-name">{{ s.name }}</span>
                <span class="suggest-meta">
                  <span v-if="s.sku">{{ s.sku }}</span>
                  <span v-if="s.qty_on_hand != null" class="suggest-stock">
                    {{ s.qty_on_hand }} on hand
                  </span>
                  <span v-else-if="s.source === 'door_catalog'" class="muted">door</span>
                </span>
              </button>
            </li>
          </ul>

          <div v-if="partQuery.trim()" class="part-controls">
            <InputNumber
              v-model="partQty"
              :min="1"
              :max="99"
              show-buttons
              button-layout="horizontal"
              data-testid="mjd-part-qty"
            >
              <template #incrementbuttonicon><i class="pi pi-plus" /></template>
              <template #decrementbuttonicon><i class="pi pi-minus" /></template>
            </InputNumber>
            <div class="urgent-toggle">
              <Checkbox v-model="partUrgent" input-id="mjd-part-urgent" binary />
              <label for="mjd-part-urgent">Urgent</label>
            </div>
            <Button
              label="Request"
              icon="pi pi-plus"
              size="small"
              :loading="partBusy"
              data-testid="mjd-part-add"
              @click="addPart"
            />
          </div>
        </div>
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

      <!-- Sticky so the tech can act without scrolling a long job.
           Hidden for as long as the part composer is open — while suggestions
           show AND while a picked/typed name is pending submit. This bar
           floats over that whole area: on a real phone it covered four of six
           suggestions (a tap meant for the third landed on "On my way",
           firing a dispatch action the tech never chose), and once a part was
           picked it covered the Request button itself. Raising the composer's
           z-index doesn't fix it — the app's bottom nav is a separate stacking
           context and still wins — but yielding the space does, and a tech
           mid-compose isn't reaching for these buttons anyway. -->
      <div
        v-if="!partComposerOpen"
        class="action-bar"
        data-testid="mobile-job-detail-actions"
      >
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
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { queuedWriteStatus, useOfflineSync } from '../composables/useOfflineSync'
import { usePhotoQueue } from '../composables/usePhotoQueue'
import AuthedImage from '../components/AuthedImage.vue'
import MobileJobCloseoutDialog from '../components/MobileJobCloseoutDialog.vue'
import MobileInvoiceDialog from '../components/MobileInvoiceDialog.vue'

const api = useApi()
const toast = useToast()
const route = useRoute()
const router = useRouter()
const { pendingPhotos, capturePhoto } = usePhotoQueue()

// Registers the queue's `online` + `visibilitychange` drain listeners for as
// long as this screen is mounted, and tears them down after.
//
// They live in useOfflineSync()'s onMounted, and until now MobileTodayView was
// the ONLY caller in the app — so a tech who queued a note, a part request or
// an arrival from this screen and then regained signal drained nothing: the
// writes sat in IndexedDB until they happened to navigate back to Today.
// Caught on a real phone by watching a note stay "waiting for signal" with the
// wifi back on. syncNow() guards on a module-level `syncing` ref, so a second
// caller can't double-drain.
const { pendingCount } = useOfflineSync()

const photoInput = ref(null)
const photoBusy = ref(false)

const loading = ref(true)
const error = ref(null)
const job = ref(null)
const notes = ref([])
const photos = ref([])
const parts = ref([])
const advancing = ref(false)
const closeoutOpen = ref(false)
const invoiceOpen = ref(false)

const noteDraft = ref('')
const noteBusy = ref(false)

const partQuery = ref('')
const partSku = ref(null)
const partQty = ref(1)
const partUrgent = ref(false)
const partBusy = ref(false)
const partSuggestions = ref([])
let partSearchTimer = null
let partSearchSeq = 0

// The customer rides on the job (same shape the Today cards read), so the
// actions can reach job.customer without caring which screen mounted them.
const customer = computed(() => job.value?.customer || null)

// True from the first keystroke in the part search until the request is filed
// or the field is cleared — i.e. exactly while the suggestion list or the
// qty/urgent/Request row is on screen and needs the sticky bar out of the way.
const partComposerOpen = computed(
  () => partSuggestions.value.length > 0 || partQuery.value.trim().length > 0,
)

async function load() {
  loading.value = true
  error.value = null
  try {
    const r = await api.get(`/api/mobile/job/${route.params.id}`)
    job.value = r?.job || null
    notes.value = r?.notes || []
    photos.value = r?.photos || []
    parts.value = r?.parts || []
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
      notes.value = await withStillQueued(r.notes || [], notes.value)
      photos.value = r.photos || []
      parts.value = await withStillQueued(r.parts || [], parts.value)
    }
  } catch {
    // Offline or a blip. The queued write still lands on reconnect.
  }
}

/**
 * Server rows + the optimistic rows whose own write hasn't landed yet.
 *
 * The server list is authoritative for everything it knows about, but it
 * cannot know about a write still sitting in the queue. Overwriting wholesale
 * would erase a note the tech wrote in a dead zone the moment any later
 * refresh runs — the write is safe in the queue, but it looks lost, which is
 * the same failure the vanishing-job split above exists to prevent.
 *
 * A row is dropped only when ITS OWN key leaves the queue, so it can never
 * linger beside the server's copy of itself. A write the server REJECTED stays
 * on screen flagged "didn't send": it isn't coming back on a refresh, and
 * quietly deleting the tech's work is worse than showing it failed.
 */
async function withStillQueued(serverRows, currentRows) {
  const local = (currentRows || []).filter((r) => r._pending || r._failed)
  if (!local.length) return serverRows
  const survivors = []
  for (const row of local) {
    const state = await queuedWriteStatus(row._key)
    if (state === 'waiting') survivors.push({ ...row, _pending: true, _failed: false })
    else if (state === 'failed') survivors.push({ ...row, _pending: false, _failed: true })
    // null → it landed; the server row above is the real one.
  }
  return [...serverRows, ...survivors]
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

// ─── Notes ───────────────────────────────────────────────────────────────
async function addNote() {
  const body = noteDraft.value.trim()
  if (!body || noteBusy.value) return
  noteBusy.value = true
  try {
    // The mobile endpoint, not the office one at /api/jobs/{id}/notes. Both
    // write JobNote.body and both work; this one goes through the same
    // _assert_job_access gate as the rest of this screen, which is the reason
    // to prefer it.
    //
    // (It also *tries* to record author_name for per-tech attribution — but
    // resolves it from name/full_name/email on the user dict, and the JWT
    // carries only sub/role/tenant_id, so it always writes NULL. Verified
    // against a real technician token: the column is empty for every note in
    // the DB. Don't count that feature as working.)
    //
    // Its field is `note`; the office endpoint's is `body`; the read side
    // aliases the column back (SELECT body AS note). Three names, one column —
    // so what you post is not what you get back. Posting the wrong one 422s.
    const r = await api.postQueued(
      `/api/mobile/jobs/${job.value.id}/notes`,
      { note: body },
      { actionType: 'job.note', resourceId: String(job.value.id) },
    )
    if (r?.queued) {
      // Show it now, flagged. It is safe in the queue; the tech needs to see
      // that what they wrote wasn't dropped.
      notes.value = [
        ...notes.value,
        { id: `pending-${r.idempotency_key}`, note: body, _pending: true, _key: r.idempotency_key },
      ]
      toast.add({ severity: 'info', summary: 'Saved offline', detail: 'Sends when you have signal', life: 3000 })
    } else {
      await refresh()
    }
    noteDraft.value = ''
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not add note', detail: err?.message || '', life: 4000 })
  } finally {
    noteBusy.value = false
  }
}

// ─── Parts ───────────────────────────────────────────────────────────────
function onPartQuery() {
  // Typing a name by hand is a valid request on its own, so clear any SKU the
  // tech previously picked — otherwise an edited name keeps riding the old
  // sku and dispatch orders the wrong part.
  partSku.value = null
  const q = partQuery.value.trim()
  clearTimeout(partSearchTimer)
  if (q.length < 2) {
    partSuggestions.value = []
    return
  }
  partSearchTimer = setTimeout(() => searchParts(q), 250)
}

async function searchParts(q) {
  const seq = ++partSearchSeq
  try {
    const r = await api.get(
      `/api/parts-needed/sku-suggest?q=${encodeURIComponent(q)}&limit=6`,
      { suppressErrorToast: true },
    )
    // A slow earlier request must not overwrite a newer one's results.
    if (seq !== partSearchSeq) return
    partSuggestions.value = Array.isArray(r) ? r : []
  } catch {
    // Offline, or the search is down. Free-text still works — say nothing and
    // let the tech type.
    if (seq === partSearchSeq) partSuggestions.value = []
  }
}

function pickPart(s) {
  partQuery.value = s.name || s.sku || ''
  partSku.value = s.sku || null
  partSuggestions.value = []
}

async function addPart() {
  const name = partQuery.value.trim()
  if (!name || partBusy.value) return
  partBusy.value = true
  // Clamp here, not just on the input. `:max` only clamps on blur, so a tech
  // who types a qty and taps Request without leaving the field submits whatever
  // is in it — and the server accepts anything up to 999 (PartNeededIn), so a
  // fat-fingered 267 would reach dispatch as a real order for 267 rollers. The
  // bound belongs on the path that sends the value.
  const qty = Math.min(99, Math.max(1, Math.trunc(Number(partQty.value) || 1)))
  const urgency = partUrgent.value ? 'urgent' : 'normal'
  try {
    const r = await api.postQueued(
      `/api/jobs/${job.value.id}/parts-needed`,
      { part_name: name, sku: partSku.value, quantity: qty, urgency },
      { actionType: 'job.part_needed', resourceId: String(job.value.id) },
    )
    if (r?.queued) {
      parts.value = [
        ...parts.value,
        {
          id: `pending-${r.idempotency_key}`,
          part_name: name,
          sku: partSku.value,
          quantity: qty,
          urgency,
          status: 'needed',
          _pending: true,
          _key: r.idempotency_key,
        },
      ]
      toast.add({ severity: 'info', summary: 'Saved offline', detail: 'Sends when you have signal', life: 3000 })
    } else {
      await refresh()
      toast.add({ severity: 'success', summary: 'Part requested', life: 2500 })
    }
    partQuery.value = ''
    partSku.value = null
    partQty.value = 1
    partUrgent.value = false
    partSuggestions.value = []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not request part', detail: err?.message || '', life: 4000 })
  } finally {
    partBusy.value = false
  }
}


async function onPhotoPicked(e) {
  const files = Array.from(e?.target?.files || [])
  if (!files.length) return
  photoBusy.value = true
  let queued = 0
  try {
    for (const f of files) {
      const r = await capturePhoto(job.value.id, f)
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

// Draining is silent — it happens in the queue, not here — so without this the
// tech watches "waiting for signal" sit there after the write has already
// landed. Refetch whenever the queue shrinks: withStillQueued() then finds the
// key gone and swaps the optimistic row for the server's, timestamp and all.
watch(pendingCount, (now, before) => {
  if (now < before) refresh()
})

onMounted(load)
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
.note-author { font-weight: 600; }
.photo-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.photo-pending {
  display: inline-flex; align-items: center; gap: 0.3rem;
  font-size: 0.75rem; font-weight: 600;
  color: var(--p-amber-600, #b45309);
}
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
.photo-strip { display: flex; gap: 0.5rem; overflow-x: auto; }
.photo-thumb { flex: 0 0 auto; width: 96px; height: 96px; border-radius: 0.4rem; overflow: hidden; border: 1px solid var(--p-content-border-color, #e5e7eb); display: flex; align-items: center; justify-content: center; }
.photo-thumb :deep(img) { width: 100%; height: 100%; object-fit: cover; }
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

/* Notes + parts composers. Every control here clears the same 44px tap floor
   the action bar does — a tech taps these wearing gloves. */
.detail-card :deep(.p-textarea) { width: 100%; margin-top: 0.6rem; }
.detail-card :deep(.p-inputtext) { width: 100%; min-height: 44px; }
.detail-card > :deep(.p-button) { margin-top: 0.5rem; min-height: 44px; }

.pending-flag {
  display: inline-flex; align-items: center; gap: 0.3rem;
  color: var(--p-primary-color, #3b82f6);
}
.failed-flag {
  display: inline-flex; align-items: center; gap: 0.3rem;
  color: var(--p-red-500, #ef4444); font-weight: 600;
}

.part-list { list-style: none; margin: 0 0 0.6rem; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.part-list li { border-bottom: 1px solid var(--p-content-border-color, #e5e7eb); padding-bottom: 0.5rem; }
.part-list li:last-child { border-bottom: 0; }
.part-main { display: flex; justify-content: space-between; gap: 0.5rem; font-size: 0.95rem; }
.part-name { font-weight: 500; }
.part-qty { color: var(--p-text-muted-color, #9ca3af); white-space: nowrap; }
.part-meta {
  display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center;
  font-size: 0.75rem; color: var(--p-text-muted-color, #9ca3af); margin-top: 0.15rem;
}
.part-sku { font-family: ui-monospace, monospace; }
.part-urgent {
  color: var(--p-red-500, #ef4444); font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.02em;
}
.part-status { text-transform: capitalize; }

.part-add { display: flex; flex-direction: column; gap: 0.5rem; }
.suggest-list {
  list-style: none; margin: 0; padding: 0;
  border: 1px solid var(--p-content-border-color, #e5e7eb); border-radius: 6px;
  overflow: hidden;
  /* The catalog is 2,600 items; a long match list must scroll inside itself
     rather than push the Request button off-screen. */
  max-height: 40vh; overflow-y: auto;
}
.suggest-list button {
  width: 100%; min-height: 44px; text-align: left; cursor: pointer;
  display: flex; flex-direction: column; gap: 0.15rem;
  padding: 0.5rem 0.6rem; border: 0; border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
  background: var(--p-content-background, #fff); color: inherit; font: inherit;
}
.suggest-list li:last-child button { border-bottom: 0; }
.suggest-list button:active { background: var(--p-content-hover-background, #f3f4f6); }
.suggest-name { font-size: 0.9rem; }
.suggest-meta {
  display: flex; gap: 0.5rem; font-size: 0.72rem;
  color: var(--p-text-muted-color, #9ca3af); font-family: ui-monospace, monospace;
}
.suggest-stock { color: var(--p-green-600, #16a34a); font-family: inherit; }

.part-controls { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; }
.part-controls :deep(.p-inputnumber-input) { width: 3rem; text-align: center; }
.part-controls :deep(.p-inputnumber .p-button) { min-width: 44px; min-height: 44px; }
.urgent-toggle { display: flex; align-items: center; gap: 0.4rem; min-height: 44px; }
.urgent-toggle label { font-size: 0.9rem; }
.part-controls :deep(.p-button) { min-height: 44px; }
</style>

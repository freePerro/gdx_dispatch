<template>
  <section class="mdl">
    <header class="mdl-head">
      <h1>List a Door</h1>
      <p class="sub">Pulled a good door off a tear-out? Send it in and the office will price it.</p>
    </header>

    <div v-if="error" class="banner banner--err" data-testid="mdl-error">
      {{ error }}
      <button
        v-if="pendingListingId && shots.length"
        type="button"
        class="retry"
        :disabled="saving"
        data-testid="mdl-retry"
        @click="retryPhotos"
      >
        <i class="pi" :class="saving ? 'pi-spinner pi-spin' : 'pi-refresh'" /> Retry photos
      </button>
    </div>

    <!-- Photo first: a tech is standing in front of the door, and a listing
         cannot be published without one, so asking for it last invites a
         submission the office has to bounce. -->
    <div class="card">
      <div class="card-title">
        <i class="pi pi-camera" /> Photos
        <span class="req">required</span>
      </div>

      <div v-if="shots.length" class="shots">
        <div v-for="(s, i) in shots" :key="i" class="shot">
          <img :src="s.preview" :alt="`Door photo ${i + 1}`" />
          <button type="button" class="shot-x" :aria-label="`Remove photo ${i + 1}`" @click="removeShot(i)">
            <i class="pi pi-times" />
          </button>
        </div>
      </div>

      <!-- A styled <label> wrapping a real hidden input: capture="environment"
           is what opens the back camera directly, and only a genuine file
           input gets that behaviour. Same pattern as MobileJobDetailView. -->
      <label class="shot-add" data-testid="mdl-photo-add">
        <input type="file" accept="image/*" capture="environment" multiple @change="onPick" />
        <span><i class="pi pi-camera" /> {{ shots.length ? 'Add another' : 'Take a photo' }}</span>
      </label>
    </div>

    <div class="card">
      <div class="card-title"><i class="pi pi-arrows-h" /> Size</div>
      <!-- Chips first because 9x7 and 16x7 are most of what comes off a
           tear-out — two taps beats four number entries with gloves on. -->
      <div class="chips">
        <button
          v-for="p in SIZE_PRESETS"
          :key="p.label"
          type="button"
          class="chip"
          :class="{ 'chip--on': form.width_in === p.w && form.height_in === p.h }"
          @click="pickSize(p)"
        >{{ p.label }}</button>
      </div>
      <div class="two-up">
        <label>Width (in)<input v-model.number="form.width_in" type="number" inputmode="numeric" min="0" /></label>
        <label>Height (in)<input v-model.number="form.height_in" type="number" inputmode="numeric" min="0" /></label>
      </div>
      <p class="hint">{{ sizeHint }}</p>
    </div>

    <div class="card">
      <div class="card-title"><i class="pi pi-star" /> Condition</div>
      <div class="chips">
        <button
          v-for="c in CONDITIONS"
          :key="c.value"
          type="button"
          class="chip"
          :class="{ 'chip--on': form.condition === c.value }"
          @click="form.condition = c.value"
        >{{ c.label }}</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><i class="pi pi-tag" /> Details</div>
      <label class="stack">Colour<input v-model="form.color" type="text" placeholder="Sandtone, white…" /></label>
      <label class="stack">
        Anything the office should know
        <textarea v-model="form.description" rows="3" placeholder="Dented bottom section, glass all intact…"></textarea>
      </label>
      <!-- Deliberately NO price field. The office sets price at approval; a
           number typed in the field would render on the website as our asking
           price. -->
      <p class="hint">The office sets the price when they approve it.</p>
    </div>

    <button
      type="button"
      class="submit"
      :disabled="!canSubmit || saving"
      data-testid="mdl-submit"
      @click="submit"
    >
      <i class="pi" :class="saving ? 'pi-spinner pi-spin' : 'pi-send'" />
      {{ saving ? 'Sending…' : 'Send for review' }}
    </button>
    <p v-if="!canSubmit" class="hint hint--center">{{ blockedReason }}</p>

    <!-- Their own submissions, so a rejection is visible where they submitted -->
    <section class="mine">
      <h2>Your submissions</h2>
      <div v-if="loadingMine" class="hint">Loading…</div>
      <p v-else-if="!mine.length" class="hint">Nothing sent in yet.</p>
      <ul v-else class="mine-list">
        <li v-for="m in mine" :key="m.id" class="mine-row">
          <div class="mine-main">
            <span class="mine-title">{{ m.title }}</span>
            <span class="pill" :class="`pill--${pillKind(m.status)}`">{{ STATUS_LABEL[m.status] || m.status }}</span>
          </div>
          <p v-if="m.status === 'rejected' && m.rejection_reason" class="mine-why">
            <i class="pi pi-info-circle" /> {{ m.rejection_reason }}
          </p>
          <p v-if="!m.photos?.length && CAN_STILL_EDIT.includes(m.status)" class="mine-why mine-why--warn">
            <i class="pi pi-exclamation-triangle" /> No photo — the office can't publish it
          </p>
          <label
            v-if="CAN_STILL_EDIT.includes(m.status)"
            class="mine-add"
            :data-testid="`mdl-add-photo-${m.id}`"
          >
            <input type="file" accept="image/*" capture="environment" @change="addPhotoTo(m, $event)" />
            <span><i class="pi pi-camera" /> Add a photo to this one</span>
          </label>
        </li>
      </ul>
    </section>
  </section>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

const api = useApi()
const toast = useToast()

const SIZE_PRESETS = [
  { label: "8×7", w: 96, h: 84 },
  { label: "9×7", w: 108, h: 84 },
  { label: "10×7", w: 120, h: 84 },
  { label: "16×7", w: 192, h: 84 },
  { label: "18×7", w: 216, h: 84 },
]
const CONDITIONS = [
  { value: 'like_new', label: 'Like new' },
  { value: 'good', label: 'Good' },
  { value: 'fair', label: 'Fair' },
]
// Statuses the submitter may still attach photos to — mirrors
// SUBMITTER_EDITABLE_STATUSES on the server.
const CAN_STILL_EDIT = ['pending_review', 'rejected']

const STATUS_LABEL = {
  pending_review: 'Waiting on office',
  published: 'Live on site',
  rejected: 'Not accepted',
  sold: 'Sold',
  draft: 'Draft',
  archived: 'Archived',
}

function feetInches(n) {
  const t = Math.round(n)
  const f = Math.floor(t / 12); const i = t % 12
  return i ? `${f}'${i}"` : `${f}'`
}

const shots = ref([])
// Set when the row was created but its photos failed — enables a photo-only retry.
const pendingListingId = ref(null)
const mine = ref([])
const loadingMine = ref(false)
const saving = ref(false)
const error = ref(null)

const form = reactive({
  width_in: null, height_in: null, condition: 'good', color: '', description: '',
})

function pickSize(p) {
  form.width_in = p.w
  form.height_in = p.h
}

const sizeHint = computed(() => {
  const { width_in: w, height_in: h } = form
  if (!w || !h) return 'Tap a common size, or type the opening in inches.'
  return `That's ${feetInches(w)} × ${feetInches(h)}.`
})

// Title is derived, not typed: a tech should not have to compose marketing copy
// on a phone, and the office can rename it at approval.
const derivedTitle = computed(() => {
  const { width_in: w, height_in: h, color } = form
  const short = (n) => {
    if (!n) return null
    const t = Math.round(n); const f = Math.floor(t / 12); const i = t % 12
    return i ? `${f}'${i}"` : `${f}`
  }
  const size = w && h ? `${short(w)}x${short(h)}` : 'Used door'
  return [size, color?.trim()].filter(Boolean).join(' ') + ' (from the field)'
})

// Smallest/largest openings we would ever actually list, in INCHES. The guard
// exists because the chips are labelled in FEET ("16×7") right above inputs
// labelled "(in)" — typing 16 and 7 there yields a 1'4"x0'7" door that the
// backend happily accepts (ge=0) and the office then prices.
const MIN_IN = 48
const MAX_IN = 288

const sizeLooksLikeFeet = computed(() => {
  const { width_in: w, height_in: h } = form
  return (w && w < MIN_IN) || (h && h < MIN_IN)
})

const canSubmit = computed(() =>
  shots.value.length > 0 &&
  !!form.width_in && !!form.height_in &&
  !sizeLooksLikeFeet.value &&
  form.width_in <= MAX_IN && form.height_in <= MAX_IN
)

const blockedReason = computed(() => {
  if (!shots.value.length) return 'Add at least one photo.'
  if (!form.width_in || !form.height_in) return 'Add the opening size.'
  if (sizeLooksLikeFeet.value) {
    return `These are INCHES, not feet — a ${form.width_in}" door isn't a door. A 16ft opening is 192.`
  }
  if (form.width_in > MAX_IN || form.height_in > MAX_IN) return `That's over ${MAX_IN}" — check the numbers.`
  return ''
})

const MAX_SHOTS = 8   // mirrors service.MAX_PHOTOS_PER_LISTING

function onPick(e) {
  const files = Array.from(e?.target?.files || [])
  for (const f of files) {
    if (!f.type.startsWith('image/')) continue
    if (shots.value.length >= MAX_SHOTS) {
      toast.add({ severity: 'warn', summary: `${MAX_SHOTS} photos is the limit`, life: 3000 })
      break
    }
    shots.value.push({ file: f, preview: URL.createObjectURL(f) })
  }
  // Let the same file be re-picked after a remove.
  if (e?.target) e.target.value = ''
}

function removeShot(i) {
  const [gone] = shots.value.splice(i, 1)
  if (gone?.preview) URL.revokeObjectURL(gone.preview)
}

function pillKind(status) {
  if (status === 'published') return 'ok'
  if (status === 'rejected') return 'bad'
  if (status === 'pending_review') return 'wait'
  return 'mute'
}

async function fetchMine() {
  loadingMine.value = true
  try {
    const data = await api.get('/api/door-listings?mine=1')
    mine.value = data.listings || []
  } catch {
    // A failed history load must not block submitting a door.
    mine.value = []
  } finally {
    loadingMine.value = false
  }
}

async function submit() {
  // Explicit re-entry guard. Relying on :disabled alone leaves a window on a
  // double-tap, and a lost create ack has no idempotency key behind it.
  if (!canSubmit.value || saving.value) return
  saving.value = true
  error.value = null
  try {
    const created = await api.post('/api/door-listings', {
      title: derivedTitle.value,
      listing_type: 'used',
      condition: form.condition,
      width_in: form.width_in,
      height_in: form.height_in,
      color: form.color?.trim() || null,
      description: form.description?.trim() || null,
    }, { suppressErrorToast: true })

    const failed = await uploadShots(created.id, shots.value)

    if (failed.length) {
      // DO NOT clear shots. Discarding the blobs here was data loss: the row
      // exists as pending_review with no photo, the office cannot publish it,
      // and the tech has nothing left to retry with. Keep the previews, keep
      // the form, and point them at Retry.
      shots.value = failed
      pendingListingId.value = created.id
      error.value =
        `Door saved, but ${failed.length} photo${failed.length > 1 ? 's' : ''} did not upload. ` +
        'It cannot go on the website without one — tap Retry photos.'
    } else {
      toast.add({ severity: 'success', summary: 'Sent to the office', life: 2500 })
      resetForm()
    }
    await fetchMine()
  } catch (e) {
    error.value = e?.message || 'Could not send that door. Check your signal and try again.'
  } finally {
    saving.value = false
  }
}

/** Upload each shot; return the ones that failed so the caller can retry. */
async function uploadShots(listingId, list) {
  const failed = []
  for (const s of list) {
    const fd = new FormData()
    fd.append('file', s.file)
    try {
      await api.post(`/api/door-listings/${listingId}/photos`, fd, { suppressErrorToast: true })
    } catch {
      failed.push(s)
    }
  }
  return failed
}

/** Retry only the photos, against the row that already exists. */
async function retryPhotos() {
  if (!pendingListingId.value || saving.value) return
  saving.value = true
  try {
    const failed = await uploadShots(pendingListingId.value, shots.value)
    shots.value = failed
    if (!failed.length) {
      error.value = null
      pendingListingId.value = null
      toast.add({ severity: 'success', summary: 'Photos uploaded', life: 2500 })
      resetForm()
    } else {
      error.value = `Still ${failed.length} photo${failed.length > 1 ? 's' : ''} to go — check your signal.`
    }
    await fetchMine()
  } finally {
    saving.value = false
  }
}

/** Add a photo to a submission from a previous session (the day-after case).
 *  The API already allows it while the row is pending_review or rejected. */
async function addPhotoTo(listing, e) {
  const files = Array.from(e?.target?.files || [])
  if (!files.length) return
  saving.value = true
  try {
    const failed = await uploadShots(listing.id, files.map((f) => ({ file: f })))
    if (failed.length) {
      toast.add({ severity: 'error', summary: 'Photo did not upload', detail: 'Check your signal.', life: 5000 })
    } else {
      toast.add({ severity: 'success', summary: 'Photo added', life: 2500 })
    }
    await fetchMine()
  } finally {
    saving.value = false
    if (e?.target) e.target.value = ''
  }
}

function resetForm() {
  shots.value.forEach((s) => URL.revokeObjectURL(s.preview))
  shots.value = []
  pendingListingId.value = null
  form.width_in = null; form.height_in = null
  form.color = ''; form.description = ''; form.condition = 'good'
}

onMounted(fetchMine)
onBeforeUnmount(() => shots.value.forEach((s) => URL.revokeObjectURL(s.preview)))
</script>

<style scoped>
.mdl { padding: 0.75rem 0.75rem 6rem; display: flex; flex-direction: column; gap: 0.75rem; }
.mdl-head h1 { font-size: 1.35rem; font-weight: 700; margin: 0; }
.sub, .hint { color: var(--p-text-muted-color, #6b7280); font-size: 0.85rem; margin: 0.25rem 0 0; }
.hint--center { text-align: center; }
.card {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.75rem; padding: 0.85rem;
  background: var(--p-content-background, #fff);
  display: flex; flex-direction: column; gap: 0.6rem;
}
.card-title { display: flex; align-items: center; gap: 0.45rem; font-weight: 600; }
.req { margin-left: auto; font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--p-amber-600, #b45309); }
.shots { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.shot { position: relative; }
.shot img {
  /* 128x96, not 88x66. The remove control below cannot go under ~44px (the
     platform touch floor, and a global min-height enforces it anyway), so the
     thumb has to be big enough that a 44px button sits in its corner instead of
     swallowing it. Found on a real Pixel 8: at 88px the X covered the door. */
  width: 128px; height: 96px; object-fit: cover; border-radius: 0.5rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  display: block;
}
.shot-x {
  position: absolute; top: -8px; right: -8px;
  width: 44px; height: 44px; min-height: 44px; padding: 0;
  border-radius: 999px; border: 2px solid var(--p-content-background, #fff);
  cursor: pointer; background: var(--p-red-500, #ef4444); color: #fff;
  font-size: 0.85rem; line-height: 1;
  display: flex; align-items: center; justify-content: center;
}
.shot-add {
  display: flex; align-items: center; justify-content: center; gap: 0.4rem;
  min-height: 48px; border-radius: 0.5rem; cursor: pointer;
  border: 1px dashed var(--p-primary-color, #2563eb);
  color: var(--p-primary-color, #2563eb); font-weight: 600;
}
.shot-add input { display: none; }
.chips { display: flex; flex-wrap: wrap; gap: 0.4rem; }
/* 44px minimum — the platform touch-target floor. */
.chip {
  min-height: 44px; padding: 0 0.9rem; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--p-content-border-color, #d1d5db);
  background: var(--p-content-background, #fff);
  color: inherit; font-size: 0.95rem; font-weight: 600;
}
.chip--on {
  background: var(--p-primary-color, #2563eb); color: #fff;
  border-color: var(--p-primary-color, #2563eb);
}
.two-up { display: grid; grid-template-columns: 1fr 1fr; gap: 0.6rem; }
label.stack, .two-up label { display: flex; flex-direction: column; gap: 0.3rem; font-size: 0.85rem; font-weight: 600; }
input[type='text'], input[type='number'], textarea {
  /* font-family: inherit — a textarea defaults to monospace, which next to the
     sans-serif Colour input above it read as a rendering bug on the phone. */
  font-family: inherit;
  min-height: 44px; padding: 0.5rem 0.6rem; font-size: 1rem;
  border: 1px solid var(--p-content-border-color, #d1d5db);
  border-radius: 0.5rem; background: var(--p-content-background, #fff); color: inherit;
  width: 100%; box-sizing: border-box;
}
textarea { min-height: 72px; }
.submit {
  min-height: 52px; border: none; border-radius: 0.65rem; cursor: pointer;
  background: var(--p-primary-color, #2563eb); color: #fff;
  font-size: 1.05rem; font-weight: 700;
  display: flex; align-items: center; justify-content: center; gap: 0.5rem;
}
.submit:disabled { opacity: 0.5; }
.mine { margin-top: 0.5rem; }
.mine h2 { font-size: 1rem; font-weight: 700; margin: 0 0 0.5rem; }
.mine-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
.mine-row {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem; padding: 0.6rem 0.7rem;
}
.mine-main { display: flex; align-items: center; gap: 0.5rem; }
.mine-title { font-weight: 600; font-size: 0.9rem; flex: 1; }
.mine-why { margin: 0.35rem 0 0; font-size: 0.8rem; color: var(--p-text-muted-color, #6b7280); }
.pill { font-size: 0.7rem; font-weight: 700; padding: 0.2rem 0.5rem; border-radius: 999px; white-space: nowrap; }
.pill--ok { background: color-mix(in srgb, var(--p-green-500, #22c55e) 18%, transparent); color: var(--p-green-700, #15803d); }
.pill--bad { background: color-mix(in srgb, var(--p-red-500, #ef4444) 18%, transparent); color: var(--p-red-700, #b91c1c); }
.pill--wait { background: color-mix(in srgb, var(--p-amber-500, #f59e0b) 20%, transparent); color: var(--p-amber-700, #b45309); }
.pill--mute { background: var(--p-content-hover-background, #f3f4f6); color: var(--p-text-muted-color, #6b7280); }
.banner { border-radius: 0.5rem; padding: 0.6rem 0.75rem; font-size: 0.9rem; }
.retry {
  display: inline-flex; align-items: center; gap: 0.35rem; margin-left: 0.5rem;
  min-height: 40px; padding: 0 0.8rem; border-radius: 0.4rem; cursor: pointer;
  border: 1px solid currentColor; background: transparent; color: inherit;
  font-weight: 700; font-size: 0.85rem;
}
.mine-why--warn { color: var(--p-amber-600, #b45309); font-weight: 600; }
:root[data-theme='dark'] .mine-why--warn { color: var(--p-amber-400, #fbbf24); }
.mine-add {
  display: inline-flex; align-items: center; gap: 0.35rem; margin-top: 0.45rem;
  min-height: 44px; padding: 0 0.7rem; border-radius: 0.4rem; cursor: pointer;
  border: 1px dashed var(--p-primary-color, #2563eb);
  color: var(--p-primary-color, #2563eb); font-size: 0.85rem; font-weight: 600;
}
.mine-add input { display: none; }
.banner--err {
  background: color-mix(in srgb, var(--p-red-500, #ef4444) 12%, transparent);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid color-mix(in srgb, var(--p-red-500, #ef4444) 30%, transparent);
}
:root[data-theme='dark'] .pill--ok { color: var(--p-green-300, #86efac); }
:root[data-theme='dark'] .pill--bad { color: var(--p-red-300, #fca5a5); }
:root[data-theme='dark'] .pill--wait { color: var(--p-amber-300, #fcd34d); }
:root[data-theme='dark'] .banner--err { color: var(--p-red-300, #fca5a5); }
</style>

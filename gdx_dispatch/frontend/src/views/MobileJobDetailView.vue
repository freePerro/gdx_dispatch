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

      <div v-if="photos.length" class="detail-card">
        <h2>Photos</h2>
        <div class="photo-strip">
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
      </div>
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import { useApi } from '../composables/useApi'

const api = useApi()
const route = useRoute()
const router = useRouter()

const loading = ref(true)
const error = ref(null)
const job = ref(null)
const customer = ref(null)
const notes = ref([])
const photos = ref([])

async function load() {
  loading.value = true
  error.value = null
  try {
    const r = await api.get(`/api/mobile/job/${route.params.id}`)
    job.value = r?.job || null
    customer.value = r?.customer || null
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

const navigationLink = computed(() => {
  const addr = customer.value?.address
  if (!addr) return null
  return `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(addr)}`
})

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
</style>

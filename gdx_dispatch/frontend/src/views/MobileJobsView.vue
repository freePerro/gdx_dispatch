<template>
    <section class="mobile-jobs">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>My Jobs</h1>
          <Button
            v-if="canCreateJob"
            label="New"
            icon="pi pi-plus"
            size="small"
            data-testid="mobile-jobs-new-btn"
            @click="createJob"
          />
        </div>
        <div class="filter-row">
          <SelectButton
            v-model="filter"
            :options="FILTER_OPTIONS"
            optionLabel="label"
            optionValue="value"
            :allowEmpty="false"
            aria-label="Filter"
          />
        </div>
      </header>

      <div v-if="loading && jobs.length === 0" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading…</span>
      </div>
      <div v-else-if="error" class="state-msg state-msg-error">
        <i class="pi pi-exclamation-triangle" />
        <span>{{ error }}</span>
      </div>
      <div v-else-if="visibleJobs.length === 0" class="state-msg">
        <i class="pi pi-briefcase empty-icon" />
        <div class="empty-title">{{ emptyTitle }}</div>
        <div class="empty-help">Pull to refresh or check with dispatch.</div>
      </div>

      <ol v-else class="job-list">
        <li v-for="job in visibleJobs" :key="job.id" class="job-card">
          <div class="job-row job-row-top">
            <div class="job-customer">{{ job.customer_name || '—' }}</div>
            <span :class="['status-pill', `status-${(job.dispatch_status || 'assigned').replace(' ','_')}`]">
              <i :class="statusIcon(job.dispatch_status)" />
              {{ statusLabel(job.dispatch_status) }}
            </span>
          </div>
          <div v-if="job.scheduled_at" class="job-when">
            <i class="pi pi-calendar" />
            {{ formatScheduled(job.scheduled_at) }}
          </div>
          <div
            class="job-address"
            :class="{ 'job-address-missing': !job.customer_address }"
          >
            <i class="pi pi-map-marker" />
            <span>{{ job.customer_address || 'No address — ask dispatch' }}</span>
          </div>
          <div v-if="job.title" class="job-title">{{ job.title }}</div>
        </li>
      </ol>

      <MobileJobNewDialog v-model:visible="newDialogOpen" @created="onJobCreated" />
    </section>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import Button from 'primevue/button'
import SelectButton from 'primevue/selectbutton'
import { useApi } from '../composables/useApi'
import { usePermission } from '../composables/usePermission'
import MobileJobNewDialog from '../components/MobileJobNewDialog.vue'

const api = useApi()
const { hasPermission } = usePermission()

// Anyone with jobs.write — including techs. The mobile-shaped dialog
// works at every viewport and doesn't need the desktop /jobs route, so
// the prior tech exclusion is gone (Doug 2026-05-10: "tech need to be
// able to make a new job, and add a new customer and parts").
const canCreateJob = computed(() => hasPermission('jobs.write'))

const newDialogOpen = ref(false)
function createJob() {
  newDialogOpen.value = true
}
function onJobCreated() {
  // Reload the list so the just-created job shows up in "All". It won't
  // be in "Active" until dispatch assigns + schedules; that's expected.
  load()
}
const FILTER_OPTIONS = [
  { label: 'Active', value: 'active' },
  { label: 'Done', value: 'done' },
  { label: 'All', value: 'all' },
]
const filter = ref('active')
const loading = ref(true)
const error = ref(null)
const jobs = ref([])

async function load() {
  loading.value = true
  error.value = null
  try {
    const r = await api.get('/api/mobile/jobs')
    jobs.value = (r?.jobs || r || []).map((j) => ({
      ...j,
      customer_name: j.customer_name || j.customer?.name || '',
      customer_address: j.customer_address || j.customer?.address || '',
    }))
  } catch (err) {
    error.value = err?.message || 'Could not load jobs'
  } finally {
    loading.value = false
  }
}

const visibleJobs = computed(() => {
  if (filter.value === 'all') return jobs.value
  if (filter.value === 'done') {
    return jobs.value.filter((j) => j.dispatch_status === 'done')
  }
  // active = anything not done
  return jobs.value.filter((j) => j.dispatch_status !== 'done')
})

const emptyTitle = computed(() => {
  if (filter.value === 'done') return 'Nothing closed yet'
  if (filter.value === 'all') return 'No jobs assigned'
  return 'No active jobs'
})

function statusLabel(s) {
  return ({
    en_route: 'En route',
    on_site: 'On site',
    done: 'Done',
    unassigned: 'Unassigned',
    assigned: 'Assigned',
  })[s] || s || 'Assigned'
}
function statusIcon(s) {
  return ({
    en_route: 'pi pi-send',
    on_site: 'pi pi-map-marker',
    done: 'pi pi-check',
    unassigned: 'pi pi-circle',
    assigned: 'pi pi-circle-fill',
  })[s] || 'pi pi-circle-fill'
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
.mobile-jobs { padding: 0.75rem; max-width: 800px; margin: 0 auto; }
.mobile-page-head { display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 0.75rem; }
.head-row { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.mobile-page-head h1 { margin: 0; font-size: 1.25rem; font-weight: 700; }
.filter-row { display: flex; }
.filter-row :deep(.p-selectbutton) { display: grid; grid-template-columns: 1fr 1fr 1fr; width: 100%; }
.filter-row :deep(.p-selectbutton .p-button) { padding-block: 0.5rem; }
.job-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.job-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem; padding: 0.85rem 1rem;
  display: flex; flex-direction: column; gap: 0.45rem;
}
.job-row { display: flex; align-items: center; gap: 0.5rem; }
.job-row-top { justify-content: space-between; }
.job-customer { font-size: 1.05rem; font-weight: 700; }
.job-when { color: var(--p-text-muted-color, #6b7280); font-size: 0.9rem; display: flex; align-items: center; gap: 0.35rem; }
.job-address { display: flex; align-items: center; gap: 0.35rem; font-size: 0.95rem; color: var(--p-primary-color, #2563eb); }
.job-address.job-address-missing { color: var(--p-text-muted-color, #9ca3af); font-style: italic; }
.job-address span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.job-title { color: var(--p-text-muted-color, #6b7280); font-size: 0.85rem; }
.status-pill {
  display: inline-flex; align-items: center; gap: 0.3rem;
  padding: 0.2rem 0.55rem; border-radius: 999px;
  font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em;
  border: 1px solid transparent;
}
.status-pill i { font-size: 0.7rem; }
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
.empty-icon { font-size: 2rem; opacity: 0.5; }
.empty-title { font-size: 1.05rem; font-weight: 600; }
.empty-help { font-size: 0.85rem; }
</style>

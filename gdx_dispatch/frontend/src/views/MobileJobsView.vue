<template>
    <section class="mobile-jobs">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>{{ scope === 'company' ? 'All Jobs' : 'My Jobs' }}</h1>
          <Button
            v-if="canCreateJob"
            label="New"
            icon="pi pi-plus"
            size="small"
            data-testid="mobile-jobs-new-btn"
            @click="createJob"
          />
        </div>
        <!-- Company-wide scope (2026-07-22): only rendered when the tenant
             option (or a dispatch role) allows it — the server 403s the
             company fetch otherwise, so this toggle is presentation only. -->
        <div v-if="allJobsEnabled" class="scope-row" data-testid="mobile-jobs-scope">
          <SelectButton
            v-model="scope"
            :options="SCOPE_OPTIONS"
            optionLabel="label"
            optionValue="value"
            :allowEmpty="false"
            aria-label="Job scope"
            @update:modelValue="load"
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
        <!-- 2026-07-01 UX audit: text said "Pull to refresh" but no pull gesture
             exists anywhere — give the empty state a real refresh button. -->
        <div class="empty-help">Refresh to check for new jobs, or check with dispatch.</div>
        <Button
          label="Refresh"
          icon="pi pi-refresh"
          size="small"
          outlined
          :loading="loading"
          data-testid="mobile-jobs-refresh-btn"
          @click="load"
        />
      </div>

      <template v-else>
        <p
          v-if="scope === 'company' && truncated"
          class="truncated-note"
          data-testid="mobile-jobs-truncated"
        >
          Showing the most recent jobs — older ones are cut off. Use desktop for the full list.
        </p>
        <ol class="job-list">
        <li v-for="job in visibleJobs" :key="job.id">
          <!-- 2026-07-16: cards used to be display-only — a tech had NO path
               from this list to notes/phone/description. The whole card is
               now a link into the mobile job detail view. -->
          <router-link
            :to="`/mobile/jobs/${job.id}`"
            class="job-card"
            :data-testid="`mobile-job-card-${job.id}`"
          >
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
            <!-- Whose job? Only meaningful company-wide; in "mine" it's you. -->
            <div
              v-if="scope === 'company' && job.assigned_tech_name"
              class="job-tech"
              data-testid="mobile-job-tech"
            >
              <i class="pi pi-user" />
              {{ job.assigned_tech_name }}
            </div>
            <div
              class="job-address"
              :class="{ 'job-address-missing': !job.customer_address }"
            >
              <i class="pi pi-map-marker" />
              <span>{{ job.customer_address || 'No address — ask dispatch' }}</span>
            </div>
            <div class="job-row job-row-bottom">
              <div v-if="job.title" class="job-title">{{ job.title }}</div>
              <i class="pi pi-chevron-right job-chevron" />
            </div>
          </router-link>
        </li>
      </ol>
      </template>

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
const SCOPE_OPTIONS = [
  { label: 'My jobs', value: 'mine' },
  { label: 'All jobs', value: 'company' },
]
const filter = ref('active')
// 'mine' | 'company'. The company option only renders once the server has
// said allJobsEnabled (tenant setting tech_mobile.techs_see_all_jobs, or a
// dispatch-manager role) — and the server re-checks on every company fetch.
const scope = ref('mine')
const allJobsEnabled = ref(false)
const truncated = ref(false)
const loading = ref(true)
const error = ref(null)
const jobs = ref([])

async function load() {
  loading.value = true
  error.value = null
  let retrying = false
  try {
    const r = await api.get(
      scope.value === 'company' ? '/api/mobile/jobs?scope=company' : '/api/mobile/jobs'
    )
    allJobsEnabled.value = Boolean(r?.all_jobs_enabled)
    truncated.value = Boolean(r?.truncated)
    jobs.value = (r?.jobs || r || []).map((j) => ({
      ...j,
      customer_name: j.customer_name || j.customer?.name || '',
      customer_address: j.customer_address || j.customer?.address || '',
    }))
  } catch (err) {
    error.value = err?.message || 'Could not load jobs'
    // A 403 here means the tenant option was turned off mid-session —
    // drop back to the personal list instead of stranding an error state.
    // Cannot loop: scope is forced to 'mine' before the retry, and this
    // branch requires scope === 'company'.
    if (scope.value === 'company' && err?.status === 403) {
      scope.value = 'mine'
      allJobsEnabled.value = false
      retrying = true
      return load()
    }
  } finally {
    // Skip the spinner reset when handing off to the retry — it manages
    // its own loading lifecycle (else the spinner dies mid-retry).
    if (!retrying) loading.value = false
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
  if (scope.value === 'company') return 'No jobs in the company yet'
  if (filter.value === 'done') return 'Nothing closed yet'
  // "No jobs assigned" was a lie for creators: jobs you create show here
  // (until dispatch assigns them), not only jobs assigned to you.
  if (filter.value === 'all') return 'No jobs yet'
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
.scope-row { display: flex; }
.scope-row :deep(.p-selectbutton) { display: grid; grid-template-columns: 1fr 1fr; width: 100%; }
.scope-row :deep(.p-selectbutton .p-button) { padding-block: 0.45rem; }
.job-tech { color: var(--p-text-muted-color, #6b7280); font-size: 0.9rem; display: flex; align-items: center; gap: 0.35rem; }
.truncated-note { margin: 0 0 0.6rem; font-size: 0.8rem; color: var(--p-text-muted-color, #6b7280); font-style: italic; }
.job-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.job-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem; padding: 0.85rem 1rem;
  display: flex; flex-direction: column; gap: 0.45rem;
  color: inherit; text-decoration: none;
}
.job-card:active { background: var(--p-content-hover-background, #f3f4f6); }
.job-row { display: flex; align-items: center; gap: 0.5rem; }
.job-row-top { justify-content: space-between; }
.job-row-bottom { justify-content: space-between; min-height: 1rem; }
.job-chevron { color: var(--p-text-muted-color, #9ca3af); font-size: 0.8rem; margin-left: auto; }
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

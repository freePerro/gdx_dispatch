<template>
    <section class="mobile-parts-order">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>Parts to Order</h1>
          <Button v-tooltip="'Refresh'" icon="pi pi-refresh" aria-label="Refresh" text size="small" :loading="loading" @click="fetchPending" data-test="mp-refresh" />
        </div>
        <SelectButton
          v-model="filter"
          :options="FILTERS"
          optionLabel="label"
          optionValue="value"
          :allowEmpty="false"
          aria-label="Filter"
          class="filter-switch"
        />
      </header>

      <div v-if="loading && !parts.length" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading…</span>
      </div>
      <div v-else-if="!visibleParts.length" class="state-msg">
        <i class="pi pi-check-circle empty-icon" />
        <div class="empty-title">{{ emptyTitle }}</div>
      </div>

      <ol v-else class="card-list">
        <li
          v-for="p in visibleParts"
          :key="p.id"
          class="part-card"
          :class="[`prio-${p.urgency || p.priority || 'normal'}`]"
          data-test="mp-part-row"
        >
          <div class="part-row">
            <span class="part-name">{{ p.name || p.part_name || '—' }}</span>
            <span v-if="p.urgency === 'critical' || p.priority === 'critical'" class="pill pill-danger">CRITICAL</span>
            <span v-else-if="p.urgency === 'high' || p.priority === 'high'" class="pill pill-warn">HIGH</span>
          </div>
          <div class="part-meta">
            <span v-if="p.sku" class="meta-item"><i class="pi pi-tag" /> {{ p.sku }}</span>
            <span v-if="p.quantity" class="meta-item"><i class="pi pi-hashtag" /> ×{{ p.quantity }}</span>
            <span v-if="p.job_title || p.job?.title" class="meta-item"><i class="pi pi-briefcase" /> {{ p.job_title || p.job?.title }}</span>
            <span v-if="p.requested_by_name" class="meta-item"><i class="pi pi-user" /> {{ p.requested_by_name }}</span>
            <Tag :value="prettyStatus(p.status)" :severity="statusSeverity(p.status)" />
          </div>
          <div v-if="p.notes" class="part-notes">{{ p.notes }}</div>
          <div class="part-actions">
            <Button
              v-if="p.status === 'needed'"
              label="Mark ordered"
              icon="pi pi-shopping-cart"
              size="small"
              :loading="savingId === p.id"
              @click="updateStatus(p, 'ordered')"
              data-test="mp-mark-ordered"
            />
            <Button
              v-if="p.status === 'ordered'"
              label="Mark received"
              icon="pi pi-check"
              size="small"
              severity="success"
              :loading="savingId === p.id"
              @click="updateStatus(p, 'received')"
              data-test="mp-mark-received"
            />
          </div>
        </li>
      </ol>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

import Button from 'primevue/button'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'

const api = useApi()
const toast = useToast()

const FILTERS = [
  { label: 'Open', value: 'open' },
  { label: 'Ordered', value: 'ordered' },
  { label: 'All', value: 'all' },
]
const filter = ref('open')

const parts = ref([])
const loading = ref(false)
const savingId = ref(null)

const visibleParts = computed(() => {
  if (filter.value === 'all') return parts.value
  if (filter.value === 'ordered') return parts.value.filter((p) => p.status === 'ordered')
  return parts.value.filter((p) => p.status === 'needed')
})

const emptyTitle = computed(() => {
  if (filter.value === 'ordered') return 'Nothing ordered'
  if (filter.value === 'all') return 'No parts to track'
  return 'No parts needed'
})

function prettyStatus(s) {
  if (!s) return '—'
  return String(s).charAt(0).toUpperCase() + String(s).slice(1)
}

function statusSeverity(s) {
  const k = String(s || '').toLowerCase()
  if (k === 'received') return 'success'
  if (k === 'ordered') return 'info'
  if (k === 'needed') return 'warning'
  return 'secondary'
}

async function fetchPending() {
  loading.value = true
  try {
    const r = await api.get('/api/parts-needed/pending')
    parts.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Load failed', detail: err.message, life: 4000 })
  } finally {
    loading.value = false
  }
}

async function updateStatus(p, status) {
  savingId.value = p.id
  try {
    // 2026-07-01 UX audit: offline-queued (see useOfflineSync) — a status
    // flip tapped in a dead zone replays on reconnect instead of erroring.
    const r = await api.patchQueued(`/api/parts-needed/${p.id}/status`, { status }, {
      actionType: 'parts.status', resourceId: String(p.id),
    })
    if (r?.queued) {
      toast.add({ severity: 'warn', summary: 'Saved offline', detail: 'Will sync when you reconnect.', life: 3000 })
    } else {
      toast.add({ severity: 'success', summary: `Marked ${status}`, life: 2000 })
    }
    p.status = status
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Update failed', detail: err.message, life: 4000 })
  } finally {
    savingId.value = null
  }
}

onMounted(fetchPending)
</script>

<style scoped>
.mobile-parts-order {
  padding: 0.75rem 0.75rem calc(5rem + env(safe-area-inset-bottom));
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

.mobile-page-head h1 {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}

.filter-switch :deep(.p-selectbutton) {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  width: 100%;
}

.filter-switch :deep(.p-selectbutton .p-button) {
  padding-block: 0.5rem;
}

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.part-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.75rem 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.part-card.prio-critical {
  border-left: 3px solid var(--color-danger-500);
}

.part-card.prio-high {
  border-left: 3px solid var(--color-warning-500);
}

.part-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.part-name {
  font-weight: 700;
  font-size: 1rem;
  flex: 1;
}

.part-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: center;
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #6b7280);
}

.part-notes {
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
  background: var(--p-content-hover-background, #f3f4f6);
  border-radius: 0.4rem;
  padding: 0.4rem 0.55rem;
}

.part-actions {
  display: flex;
  gap: 0.4rem;
}

.pill {
  display: inline-flex;
  align-items: center;
  padding: 0.1rem 0.4rem;
  border-radius: 999px;
  font-size: 0.7rem;
  font-weight: 700;
}

.pill-danger {
  background: var(--color-danger-bg);
  border: 1px solid var(--color-danger-border);
  color: var(--color-danger-500);
}

.pill-warn {
  background: var(--color-warning-bg);
  border: 1px solid var(--color-warning-border);
  color: var(--color-warning-500);
}

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

.empty-title {
  font-size: 1.05rem;
  font-weight: 600;
}
</style>

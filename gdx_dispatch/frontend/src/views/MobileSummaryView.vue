<script setup>
// Sprint tech_mobile Phase 4.2 (S4-B3) — Day summary screen.
//
// Persistent /mobile/summary route. Same data the wrap card on
// /mobile/today shows, but with a date picker so a tech can look
// back. No clock-out modal — friction-free wrap.
import { onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import Tag from 'primevue/tag'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

const api = useApi()
const toast = useToast()

const summary = ref(null)
const loading = ref(true)
const selectedDate = ref(new Date())

function fmtMoney(n) { return `$${(Number(n) || 0).toFixed(2)}` }
function _ymd(d) {
  if (!d) return ''
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

async function load() {
  loading.value = true
  try {
    summary.value = await api.get(`/api/mobile/day-summary?date=${_ymd(selectedDate.value)}`)
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not load summary', detail: e.message, life: 4000 })
  } finally {
    loading.value = false
  }
}

watch(selectedDate, load)
onMounted(load)
</script>

<template>
    <section class="summary-page">
      <header class="summary-header">
        <h1>Day wrap</h1>
        <DatePicker
          v-model="selectedDate"
          showIcon
          :maxDate="new Date()"
          dateFormat="M d, yy"
        />
      </header>

      <div v-if="loading" class="summary-loading">
        <i class="pi pi-spin pi-spinner" /> Loading…
      </div>

      <div v-else-if="summary" class="summary-grid">
        <div class="kpi-card kpi-jobs">
          <div class="kpi-label">Jobs completed</div>
          <div class="kpi-value">{{ summary.jobs_completed_count }}</div>
        </div>
        <div class="kpi-card kpi-hours">
          <div class="kpi-label">Hours on the clock</div>
          <div class="kpi-value">{{ summary.labor_hours }}</div>
        </div>
        <div class="kpi-card kpi-parts">
          <div class="kpi-label">Parts requested</div>
          <div class="kpi-value">{{ summary.parts_requested_count }}</div>
        </div>
        <div class="kpi-card kpi-revenue">
          <div class="kpi-label">Invoiced</div>
          <div class="kpi-value">{{ fmtMoney(summary.revenue_invoiced) }}</div>
          <div class="kpi-sub">{{ summary.invoices_count }} invoice{{ summary.invoices_count === 1 ? '' : 's' }}</div>
        </div>

        <div v-if="summary.jobs_completed.length" class="job-list">
          <h2>Jobs done</h2>
          <ul>
            <li v-for="j in summary.jobs_completed" :key="j.id" class="job-item">
              <strong>{{ j.title }}</strong>
              <span class="muted">{{ j.customer_name }} · {{ j.customer_address }}</span>
            </li>
          </ul>
        </div>

        <div v-if="summary.next_first_stop" class="next-stop">
          <h2>Tomorrow's first stop</h2>
          <div class="next-stop-card">
            <Tag value="UP NEXT" severity="info" />
            <strong>{{ summary.next_first_stop.title }}</strong>
            <span class="muted">{{ summary.next_first_stop.customer_name }} · {{ summary.next_first_stop.customer_address }}</span>
          </div>
        </div>
      </div>
    </section>
</template>

<style scoped>
.summary-page { padding: 0.75rem; max-width: 800px; margin: 0 auto; }
.summary-header { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; margin-bottom: 0.85rem; }
.summary-header h1 { font-size: 1.25rem; margin: 0; }

.summary-loading { padding: 2rem; text-align: center; color: var(--p-text-muted-color); }

.summary-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.6rem; }
.kpi-card {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.65rem;
  padding: 0.85rem 1rem;
  background: var(--p-content-background, white);
}
.kpi-label { font-size: 0.75rem; color: var(--p-text-muted-color); text-transform: uppercase; letter-spacing: 0.05em; }
.kpi-value { font-size: 1.6rem; font-weight: 600; margin-top: 0.2rem; }
.kpi-sub { font-size: 0.75rem; color: var(--p-text-muted-color); margin-top: 0.15rem; }
.kpi-jobs { border-left: 4px solid #15803d; }
.kpi-hours { border-left: 4px solid #2563eb; }
.kpi-parts { border-left: 4px solid #f59e0b; }
.kpi-revenue { border-left: 4px solid #7c3aed; }

.job-list, .next-stop { grid-column: 1 / -1; margin-top: 0.85rem; }
.job-list h2, .next-stop h2 { font-size: 0.85rem; color: var(--p-text-muted-color); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.4rem; }
.job-list ul { list-style: none; padding: 0; margin: 0; }
.job-item {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  padding: 0.6rem 0.85rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.5rem;
  margin-bottom: 0.4rem;
  background: var(--p-content-background, white);
}
.next-stop-card {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  padding: 0.85rem 1rem;
  border: 2px solid #2563eb;
  border-radius: 0.65rem;
  background: #eff6ff;
}
.muted { color: var(--p-text-muted-color, #6b7280); font-size: 0.85rem; }
</style>

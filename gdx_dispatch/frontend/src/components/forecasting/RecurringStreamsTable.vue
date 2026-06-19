<template>
  <DataTable
    :value="streams"
    :loading="loading"
    stripedRows
    responsiveLayout="scroll"
    class="streams-table"
  >
    <template #empty>
      <div class="empty-state">{{ emptyText }}</div>
    </template>
    <Column field="label" header="Payment" :sortable="true">
      <template #body="{ data }">
        <div class="cell-label">
          <div class="label-text">{{ data.label }}</div>
          <div class="label-meta">{{ data.payee_pattern }}</div>
        </div>
      </template>
    </Column>
    <Column field="amount_min" header="Amount" :sortable="true">
      <template #body="{ data }">{{ formatAmount(data) }}</template>
    </Column>
    <Column field="cadence" header="Cadence" :sortable="true">
      <template #body="{ data }">
        <span class="cap">{{ data.cadence }}</span>
      </template>
    </Column>
    <Column header="Progress">
      <template #body="{ data }">
        <div v-if="data.term_total_occurrences" class="progress-cell">
          <ProgressBar :value="progressPct(data)" />
          <span class="progress-label">{{ data.occurrences_seen }} / {{ data.term_total_occurrences }}</span>
        </div>
        <span v-else-if="data.term_end_date" class="cell-muted">
          ends {{ formatDate(data.term_end_date) }}
        </span>
        <span v-else class="cell-muted">open-ended ({{ data.occurrences_seen }} seen)</span>
      </template>
    </Column>
    <Column field="next_expected_date" header="Next">
      <template #body="{ data }">
        <span v-if="data.next_expected_date">{{ formatDate(data.next_expected_date) }}</span>
        <span v-else class="cell-muted">—</span>
      </template>
    </Column>
    <Column field="source" header="Source">
      <template #body="{ data }">
        <Tag :value="data.source" :severity="sourceSeverity(data.source)" />
      </template>
    </Column>
    <Column header="Status" v-if="mode === 'ended'">
      <template #body="{ data }">
        <Tag :value="data.status" :severity="endedSeverity(data.status)" />
        <span v-if="data.ended_at" class="ended-on"> on {{ formatDate(data.ended_at) }}</span>
      </template>
    </Column>
    <Column header="Actions">
      <template #body="{ data }">
        <div class="actions">
          <template v-if="mode === 'suggested'">
            <Button label="Confirm" icon="pi pi-check" size="small" @click="$emit('confirm', data)" />
            <Button label="Edit" icon="pi pi-pencil" size="small" severity="secondary" outlined @click="$emit('edit', data)" />
            <Button label="Dismiss" icon="pi pi-times" size="small" severity="danger" text @click="$emit('dismiss', data)" />
          </template>
          <template v-else-if="mode === 'active'">
            <Button label="Edit" icon="pi pi-pencil" size="small" severity="secondary" outlined @click="$emit('edit', data)" />
            <Button label="End" icon="pi pi-stop-circle" size="small" severity="warning" @click="$emit('end', data)" />
          </template>
          <span v-else class="cell-muted">read-only</span>
        </div>
      </template>
    </Column>
  </DataTable>
</template>

<script setup>
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Tag from 'primevue/tag';
import Button from 'primevue/button';
import ProgressBar from 'primevue/progressbar';

defineProps({
  streams: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  emptyText: { type: String, default: 'No data' },
  mode: { type: String, default: 'active', validator: (v) => ['suggested', 'active', 'ended'].includes(v) },
});

defineEmits(['confirm', 'dismiss', 'edit', 'end']);

function formatAmount(s) {
  const fmt = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(n));
  const lo = Number(s.amount_min);
  const hi = Number(s.amount_max);
  if (Math.abs(hi - lo) < 0.01) return fmt(lo);
  return `${fmt(lo)} – ${fmt(hi)}`;
}

function formatDate(d) {
  if (!d) return '';
  try { return new Date(d).toLocaleDateString('en-US'); } catch { return d; }
}

function progressPct(s) {
  if (!s.term_total_occurrences) return 0;
  const pct = (Number(s.occurrences_seen) / Number(s.term_total_occurrences)) * 100;
  return Math.min(100, Math.max(0, Math.round(pct)));
}

function sourceSeverity(src) {
  if (src === 'observed') return 'info';
  if (src === 'manual') return 'success';
  return 'secondary';
}

function endedSeverity(status) {
  if (status === 'paid_off') return 'success';
  if (status === 'cancelled') return 'warning';
  return 'secondary';
}
</script>

<style scoped>
.streams-table { font-size: 0.95rem; }
.cell-label .label-text { font-weight: 600; }
.cell-label .label-meta { font-size: 0.75rem; color: var(--p-text-muted-color); margin-top: 0.125rem; }
.cell-muted { color: var(--p-text-muted-color); font-size: 0.875rem; }
.cap { text-transform: capitalize; }
.actions { display: flex; gap: 0.25rem; flex-wrap: wrap; }
.progress-cell { display: flex; flex-direction: column; gap: 0.25rem; min-width: 8rem; }
.progress-label { font-size: 0.75rem; color: var(--p-text-muted-color); }
.empty-state { padding: 2rem; text-align: center; color: var(--p-text-muted-color); }
.ended-on { font-size: 0.75rem; color: var(--p-text-muted-color); margin-left: 0.5rem; }
</style>

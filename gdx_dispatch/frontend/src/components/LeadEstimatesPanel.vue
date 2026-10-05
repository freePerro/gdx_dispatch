<!--
  Every estimate made for a lead, and which one counts as won. Accepting an
  estimate wins
  the lead and picks it automatically when nothing is picked yet; staff move
  the pick here when a different accepted estimate should count.
-->
<template>
  <div class="lead-estimates" data-testid="lead-estimates-panel">
    <h4 class="lead-estimates-title">Estimates</h4>
    <div v-if="loading" class="lead-estimates-empty">Loading…</div>
    <div v-else-if="loadError" class="lead-estimates-empty" data-testid="lead-estimates-error">
      Could not load this lead's estimates.
    </div>
    <div v-else-if="!estimates.length" class="lead-estimates-empty" data-testid="lead-estimates-empty">
      No estimates yet. Start one from this lead and it will show here.
    </div>
    <ul v-else class="lead-estimates-list">
      <li
        v-for="e in estimates"
        :key="e.id"
        class="lead-estimates-row"
        :class="{ selected: e.id === selectedId }"
        :data-testid="`lead-estimate-${e.id}`"
      >
        <RouterLink :to="`/estimates/${e.id}`" class="lead-estimates-number">{{ e.estimate_number }}</RouterLink>
        <span class="lead-estimates-label">{{ e.label || '' }}</span>
        <Tag :value="estimateStatusLabel(e.status)" :severity="estimateStatusSeverity(e.status)" />
        <span class="lead-estimates-total">{{ formatMoney(e.total) }}</span>
        <span class="lead-estimates-pick">
          <Tag
            v-if="e.id === selectedId"
            value="Counts as won"
            severity="success"
            icon="pi pi-check"
            data-testid="lead-estimate-selected"
          />
          <Button
            v-else-if="canWrite && e.status === 'accepted'"
            label="Count this one"
            size="small"
            text
            :loading="saving === e.id"
            :data-testid="`lead-estimate-select-${e.id}`"
            @click="select(e.id)"
          />
        </span>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Tag from 'primevue/tag';
import { useApi } from '../composables/useApi';
import { formatMoney } from '../composables/useFormatters';
import { estimateStatusLabel, estimateStatusSeverity } from '../utils/statusSeverity';

const props = defineProps({
  leadId: { type: String, required: true },
  canWrite: { type: Boolean, default: false },
});
const emit = defineEmits(['selected']);

const api = useApi();
const estimates = ref([]);
const selectedId = ref(null);
const loading = ref(false);
const loadError = ref(false);
const saving = ref(null);

async function load() {
  loading.value = true;
  loadError.value = false;
  try {
    const body = await api.get(`/api/leads/${props.leadId}/estimates`, { suppressErrorToast: true });
    estimates.value = body?.estimates || [];
    selectedId.value = body?.selected_estimate_id || null;
  } catch {
    loadError.value = true;
    estimates.value = [];
  } finally {
    loading.value = false;
  }
}

async function select(estimateId) {
  saving.value = estimateId;
  try {
    const lead = await api.put(
      `/api/leads/${props.leadId}/selected-estimate`,
      { estimate_id: estimateId },
      { successMessage: 'Won estimate updated' },
    );
    selectedId.value = lead?.selected_estimate_id || estimateId;
    emit('selected', lead);
  } catch {
    // useApi already told the user why.
  } finally {
    saving.value = null;
  }
}

onMounted(load);
watch(() => props.leadId, load);
defineExpose({ load });
</script>

<style scoped>
.lead-estimates-title {
  margin: 0 0 0.5rem;
  font-size: 0.95rem;
}
.lead-estimates-empty {
  color: var(--p-text-muted-color);
  font-size: 0.9rem;
}
.lead-estimates-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.lead-estimates-row {
  display: grid;
  grid-template-columns: auto 1fr auto auto auto;
  align-items: center;
  gap: 0.6rem;
  padding: 0.4rem 0.5rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
}
.lead-estimates-row.selected {
  border-color: var(--p-green-500);
}
.lead-estimates-number {
  font-weight: 600;
  white-space: nowrap; /* EST-000086-1 must not break at its hyphen on a phone */
}
.lead-estimates-label {
  color: var(--p-text-muted-color);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.lead-estimates-total {
  font-variant-numeric: tabular-nums;
}
@media (max-width: 560px) {
  /* Phone: number + status on one line, the pick on its own line below. */
  .lead-estimates-row {
    grid-template-columns: 1fr auto;
  }
  .lead-estimates-total,
  .lead-estimates-label {
    display: none;
  }
  .lead-estimates-pick {
    grid-column: 1 / -1;
    justify-self: start;
  }
  .lead-estimates-pick:empty {
    display: none;
  }
}
</style>

<!--
  Sprint dispatch-capacity (2026-05-20) — per-tech efficiency leaderboard.

  Daily + weekly ratio of scheduler-estimated hours ÷ actual closeout
  hours. Higher = the tech finished faster than the estimate. Feeds the
  future bonus structure; for now it's pure visibility.

  Mounts as a collapsible panel on the Dispatch board so dispatchers can
  see who's beating their estimates without leaving the planning surface.
-->
<template>
  <Card class="tech-efficiency-panel" data-testid="tech-efficiency-panel">
    <template #title>
      <div class="te-header" @click="open = !open" style="cursor:pointer; display:flex; align-items:center; gap:0.5rem;">
        <i :class="open ? 'pi pi-chevron-down' : 'pi pi-chevron-right'"></i>
        <span>Tech efficiency</span>
        <small class="muted" v-if="!loading && !error">
          ({{ weekly?.rows?.length || 0 }} techs this week)
        </small>
        <span style="margin-left:auto;">
          <SelectButton
            v-model="window"
            :options="[{label:'Today', value:'daily'},{label:'This week', value:'weekly'}]"
            optionLabel="label"
            optionValue="value"
            :allowEmpty="false"
            size="small"
            @click.stop
            data-testid="tech-efficiency-window-toggle"
          />
        </span>
      </div>
    </template>
    <template #content v-if="open">
      <div v-if="loading" class="muted" style="padding:0.5rem 0;">Loading…</div>
      <div v-else-if="error" class="inline-error">{{ error }}</div>
      <div v-else-if="!rows.length" class="muted" style="padding:0.5rem 0;">
        No completed jobs with both an estimated time and a closeout in this window yet.
      </div>
      <DataTable v-else :value="rows" size="small" striped-rows data-testid="tech-efficiency-table">
        <Column field="tech_name" header="Tech" />
        <Column field="job_count" header="Jobs" style="width:5rem; text-align:right;" />
        <Column header="Scheduled" style="width:7rem; text-align:right;">
          <template #body="{ data }">{{ fmt(data.scheduled_hours) }}h</template>
        </Column>
        <Column header="Actual" style="width:7rem; text-align:right;">
          <template #body="{ data }">{{ fmt(data.actual_hours) }}h</template>
        </Column>
        <Column header="Beat by" style="width:7rem; text-align:right;">
          <template #body="{ data }">
            <strong :class="ratioClass(data.efficiency_ratio)">
              {{ data.efficiency_ratio != null ? data.efficiency_ratio.toFixed(2) + 'x' : '—' }}
            </strong>
          </template>
        </Column>
      </DataTable>
    </template>
  </Card>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import Card from 'primevue/card';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import SelectButton from 'primevue/selectbutton';
import { useApi } from '../composables/useApi';

const api = useApi();

const open = ref(true);
const window = ref('daily');
const loading = ref(false);
const error = ref('');
const daily = ref({ rows: [] });
const weekly = ref({ rows: [] });

const rows = computed(() => (window.value === 'weekly' ? weekly.value.rows : daily.value.rows));

function fmt(n) {
  if (n == null) return '0';
  const v = Number(n);
  return (Math.round(v * 100) / 100).toString().replace(/\.?0+$/, '') || '0';
}
function ratioClass(r) {
  if (r == null) return '';
  if (r >= 1.25) return 'te-ratio--good';
  if (r >= 1.0) return 'te-ratio--ok';
  return 'te-ratio--low';
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = await api.get('/api/reports/tech-efficiency');
    daily.value = data?.daily || { rows: [] };
    weekly.value = data?.weekly || { rows: [] };
  } catch (e) {
    error.value = e?.message || 'Could not load efficiency report.';
  } finally {
    loading.value = false;
  }
}

onMounted(load);
watch(open, (v) => { if (v) load(); });
defineExpose({ refresh: load });
</script>

<style scoped>
.tech-efficiency-panel { margin-top: 0.75rem; }
.te-header { font-size: 0.95rem; }
.te-ratio--good { color: var(--p-green-600, #16a34a); }
.te-ratio--ok   { color: var(--p-text-color, #111827); }
.te-ratio--low  { color: var(--p-red-500, #ef4444); }
.inline-error   { color: var(--p-red-500, #ef4444); padding: 0.5rem 0; }
.muted          { color: var(--p-text-muted-color, #6b7280); }
</style>

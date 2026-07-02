<template>
    <section class="loadsheet-view view-card">
      <!-- Header -->
      <div class="loadsheet-header">
        <div>
          <h2 class="page-title">Daily Load Sheet</h2>
          <p class="subtitle">{{ techName }} — {{ formattedDate }}</p>
        </div>
        <div class="header-right">
          <DatePicker v-model="selectedDate" dateFormat="yy-mm-dd" :showIcon="true"
            class="date-picker" @date-select="loadData" data-testid="loadsheet-date" />
          <Tag :value="`${checkedCount} / ${items.length} loaded`"
            :severity="checkedCount === items.length && items.length > 0 ? 'success' : 'info'" class="progress-tag" />
        </div>
      </div>

      <!-- Jobs Summary -->
      <div v-if="jobs.length" class="jobs-strip">
        <div v-for="job in jobs" :key="job.id" class="job-chip" @click="$router.push(`/jobs/${job.id}`)">
          <strong>{{ job.customer }}</strong>
          <span class="job-title-small">{{ job.title }}</span>
          <span class="job-address">{{ job.address }}</span>
        </div>
      </div>

      <div v-if="loading" class="loading-state"><ProgressSpinner /></div>

      <div v-else-if="!items.length" class="empty-state">
        <i class="pi pi-truck" style="font-size:3rem;color:var(--p-text-muted-color)"></i>
        <p>No jobs scheduled for this day.</p>
      </div>

      <!-- Grouped Checklist -->
      <template v-else>
        <div v-for="(catItems, catName) in grouped" :key="catName" class="category-group">
          <div class="category-header">{{ catName }}</div>
          <div v-for="item in catItems" :key="item.description" class="checklist-item"
            :class="{ checked: item.checked }" @click="item.checked = !item.checked">
            <Checkbox v-model="item.checked" :binary="true" class="item-check" />
            <div class="item-body">
              <div class="item-top">
                <span class="item-desc">{{ item.description }}</span>
                <span class="item-qty">×{{ item.total_qty }}</span>
              </div>
              <div class="item-jobs">
                <span v-for="j in item.jobs" :key="j.job_id" class="job-ref">
                  {{ j.customer }} <span v-if="j.qty > 1">({{ j.qty }})</span>
                </span>
              </div>
            </div>
          </div>
        </div>
      </template>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApi } from "../composables/useApi";
import { formatDate } from "../composables/useFormatters";
import Checkbox from "primevue/checkbox";
import DatePicker from "primevue/datepicker";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";

const api = useApi();
const loading = ref(true);
const techName = ref("");
const selectedDate = ref(new Date());
const jobs = ref([]);
const items = ref([]);

const formattedDate = computed(() => {
  const d = selectedDate.value instanceof Date ? selectedDate.value : new Date(selectedDate.value);
  return formatDate(d, { options: { weekday: "long", month: "long", day: "numeric", year: "numeric" } });
});

const checkedCount = computed(() => items.value.filter((i) => i.checked).length);

const grouped = computed(() => {
  const groups = {};
  for (const item of items.value) {
    const cat = item.category || "Other";
    if (!groups[cat]) groups[cat] = [];
    groups[cat].push(item);
  }
  return groups;
});

function dateStr(d) {
  const dt = d instanceof Date ? d : new Date(d);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
}

async function loadData() {
  loading.value = true;
  try {
    const data = await api.get(`/api/technicians/daily-loadsheet?date=${dateStr(selectedDate.value)}`);
    techName.value = data.technician_name || "Technician";
    jobs.value = data.jobs || [];
    items.value = (data.items || []).map((i) => ({ ...i, checked: false }));
  } catch {
    jobs.value = [];
    items.value = [];
  } finally {
    loading.value = false;
  }
}

onMounted(loadData);
</script>

<style scoped>
.loadsheet-view { max-width: 700px; margin: 0 auto; }

.loadsheet-header { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 1rem; margin-bottom: 1rem; }
.page-title { margin: 0; font-size: 1.4rem; font-weight: 700; }
.subtitle { color: var(--p-text-muted-color); font-size: 0.9rem; margin-top: 0.2rem; }
.header-right { display: flex; align-items: center; gap: 0.75rem; }
.date-picker { max-width: 160px; }
.progress-tag { font-size: 0.85rem; padding: 0.3rem 0.7rem; }

/* Jobs strip */
.jobs-strip { display: flex; gap: 0.5rem; overflow-x: auto; padding-bottom: 0.5rem; margin-bottom: 1rem; }
.job-chip { background: var(--p-content-hover-background, #1e293b); border: 1px solid var(--p-content-border-color, #334155); border-radius: 8px; padding: 0.5rem 0.75rem; min-width: 180px; cursor: pointer; flex-shrink: 0; }
.job-chip:hover { border-color: var(--p-primary-color); }
.job-chip strong { display: block; font-size: 0.9rem; }
.job-title-small { display: block; font-size: 0.78rem; color: var(--p-text-muted-color); }
.job-address { display: block; font-size: 0.72rem; color: var(--p-text-muted-color); margin-top: 0.2rem; }

/* Category groups */
.category-group { margin-bottom: 1.25rem; }
.category-header { font-size: 0.75rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.08em; color: var(--p-primary-color, #3b82f6); border-bottom: 2px solid var(--p-primary-color, #3b82f6); padding-bottom: 0.3rem; margin-bottom: 0.5rem; }

/* Checklist items */
.checklist-item { display: flex; align-items: flex-start; gap: 0.75rem; padding: 0.75rem; background: var(--p-content-hover-background, #1e293b); border: 1px solid var(--p-content-border-color, #334155); border-radius: 8px; margin-bottom: 0.4rem; cursor: pointer; transition: opacity 0.2s; }
.checklist-item.checked { opacity: 0.5; }
.checklist-item:hover { border-color: var(--p-primary-color); }

.item-check { margin-top: 2px; }
.item-body { flex: 1; min-width: 0; }
.item-top { display: flex; justify-content: space-between; align-items: flex-start; }
.item-desc { font-weight: 600; font-size: 1rem; }
.checklist-item.checked .item-desc { text-decoration: line-through; }
.item-qty { font-family: monospace; font-weight: 700; font-size: 1.1rem; background: var(--p-content-background, #0f172a); padding: 0.15rem 0.5rem; border-radius: 4px; flex-shrink: 0; }

.item-jobs { margin-top: 0.3rem; display: flex; flex-wrap: wrap; gap: 0.3rem; }
.job-ref { font-size: 0.75rem; color: var(--p-text-muted-color); }
.job-ref::before { content: "— "; }

/* Loading / Empty */
.loading-state { display: flex; justify-content: center; padding: 3rem; }
.empty-state { text-align: center; padding: 3rem; color: var(--p-text-muted-color); display: flex; flex-direction: column; align-items: center; gap: 0.5rem; }

@media (max-width: 600px) {
  .loadsheet-header { flex-direction: column; }
  .header-right { width: 100%; justify-content: space-between; }
  .item-desc { font-size: 0.95rem; }
}
</style>

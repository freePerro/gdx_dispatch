<template>
    <section class="activity-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Recent Activity</h2>
        </template>
        <template #end>
          <div class="filter-row">
            <Select
              v-model="filterEntity"
              :options="entityOptions"
              placeholder="Filter by entity"
              class="filter-select"
              showClear
              data-testid="activity-entity-filter"
            />
            <Select
              v-model="filterUser"
              :options="userOptions"
              placeholder="Filter by user"
              class="filter-select"
              showClear
              data-testid="activity-user-filter"
            />
            <Button
              icon="pi pi-filter-slash"
              label="Clear filters"
              severity="secondary"
              class="filter-clear"
              @click="clearFilters"
            />
          </div>
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-else
        :value="filteredActivity"
        striped-rows
        paginator
        :rows="20"
        responsive-layout="scroll"
        @row-click="openDetails"
        
      >
        <template #empty>
          <div class="empty-state">
            <i class="pi pi-history" style="font-size:3rem; color:#64748b;"></i>
            <h3>No activity recorded</h3>
            <p>The activity feed will appear once events start coming through.</p>
          </div>
        </template>
        <Column field="created_at" header="Time" style="width:180px">
          <template #body="{ data }">{{ formatDate(data.created_at, true) }}</template>
        </Column>
        <Column field="entity_type" header="Entity">
          <template #body="{ data }">
            <Tag :value="data.entity_type || 'Unknown'" severity="info" />
          </template>
        </Column>
        <Column field="action" header="Action" />
        <Column field="user_name" header="User" style="width:160px">
          <template #body="{ data }">{{ formatUser(data.user_name || data.user_id) }}</template>
        </Column>
        <Column field="changes" header="Details">
          <template #body="{ data }">
            {{ formatDetails(data.details || data.changes) }}
          </template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showDetails" header="Activity Details" modal :style="{ width: '520px' }">
        <div v-if="selectedEvent" class="detail-grid">
          <div>
            <strong>Entity</strong>
            <p>{{ selectedEvent.entity_type || '—' }}</p>
          </div>
          <div>
            <strong>Action</strong>
            <p>{{ selectedEvent.action }}</p>
          </div>
          <div>
            <strong>User</strong>
            <p>{{ formatUser(selectedEvent.user_name || selectedEvent.user_id) }}</p>
          </div>
          <div>
            <strong>Occurred</strong>
            <p>{{ formatDate(selectedEvent.created_at, true) }}</p>
          </div>
          <div class="full-width">
            <strong>Details</strong>
            <p>{{ selectedEvent.details || selectedEvent.changes || '—' }}</p>
          </div>
        </div>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();
const activity = ref([]);
const loading = ref(true);
const filterEntity = ref(null);
const filterUser = ref(null);
const showDetails = ref(false);
const selectedEvent = ref(null);

const entityOptions = computed(() => {
  const values = Array.from(new Set(activity.value.map((item) => item.entity_type).filter(Boolean)));
  return values.map((value) => ({ label: value, value }));
});

const userOptions = computed(() => {
  // Build {label, value} from the resolved user_name (audit.py returns it
  // alongside user_id). Falling back to user_id keeps system/anonymous
  // rows visible. Dedupe on user_id so a renamed user collapses to one row.
  const seen = new Map();
  for (const item of activity.value) {
    if (!item.user_id) continue;
    if (seen.has(item.user_id)) continue;
    const label = item.user_name && item.user_name !== item.user_id
      ? item.user_name
      : formatUser(item.user_id);
    seen.set(item.user_id, { label, value: item.user_id });
  }
  return Array.from(seen.values());
});

const filteredActivity = computed(() => {
  let list = [...activity.value];
  list.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  if (filterEntity.value) {
    list = list.filter((item) => item.entity_type === filterEntity.value);
  }
  if (filterUser.value) {
    list = list.filter((item) => item.user_id === filterUser.value);
  }
  return list;
});

function formatDate(value, includeTime = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  if (includeTime) {
    return date.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }
  return date.toLocaleDateString();
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function formatUser(value) {
  if (!value || value === 'anonymous' || value === 'system') return 'System';
  // If we got a raw UUID through (user since deleted, or resolver missed it),
  // show "Unknown user" + short id rather than a 36-char wall.
  if (typeof value === 'string' && UUID_RE.test(value)) {
    return `Unknown user (${value.slice(0, 8)})`;
  }
  return value;
}

function formatDetails(details) {
  if (!details) return '—';
  if (typeof details === 'string') return details;
  // Format common audit detail patterns as readable text
  const parts = [];
  if (details.email) parts.push(details.email);
  if (details.role) parts.push(`role: ${details.role}`);
  if (details.reason) parts.push(details.reason);
  if (details.path) parts.push(details.path);
  if (details.status_code) parts.push(`${details.status_code}`);
  if (details.module_key) parts.push(`module: ${details.module_key}`);
  if (parts.length) return parts.join(' · ');
  // Fallback: show keys summary
  const keys = Object.keys(details);
  if (keys.length <= 3) return keys.map(k => `${k}: ${details[k]}`).join(', ');
  return `${keys.length} fields`;
}

async function loadActivity() {
  loading.value = true;
  try {
    const data = await api.get('/api/activity/recent');
    activity.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

function clearFilters() {
  filterEntity.value = null;
  filterUser.value = null;
}

function openDetails(event) {
  selectedEvent.value = event;
  showDetails.value = true;
}

onMounted(() => {
  loadActivity();
});
</script>

<style scoped>
.page-title {
  margin: 0;
}
.filter-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.filter-select {
  min-width: 180px;
}
.filter-clear {
  white-space: nowrap;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem;
}
.clickable-row {
  cursor: pointer;
}
.empty-state {
  text-align: center;
  padding: 3rem;
  color: var(--p-text-muted-color);
}
.empty-state h3 {
  margin: 1rem 0 0.5rem;
  color: var(--text-color);
}
.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem;
}
.detail-grid strong {
  font-size: 0.75rem;
  text-transform: uppercase;
  color: var(--p-text-muted-color);
}
.detail-grid p {
  margin: 0.25rem 0 0;
}
</style>

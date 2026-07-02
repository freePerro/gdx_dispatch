<template>
    <section class="gps-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">GPS</h2>
        </template>
      </Toolbar>

      <Tabs value="live">
        <TabList>
          <Tab value="live">Live Techs</Tab>
          <Tab value="history">History</Tab>
        </TabList>
        <TabPanels>
        <TabPanel value="live">
          <div class="live-header">
            <div>
              <h3>Live technician positions</h3>
              <p class="muted">Refreshes automatically every 30 seconds.</p>
            </div>
            <div class="last-refresh">
              <span class="muted">Last refreshed</span>
              <strong>{{ lastRefreshLabel }}</strong>
            </div>
          </div>
          <div v-if="liveLoading" class="spinner-wrap"><ProgressSpinner /></div>
          <DataTable
            v-else
            :value="liveTechs"
            striped-rows
            responsiveLayout="scroll"
          >
            <template #empty>
              <EmptyState
                icon="pi pi-map-marker"
                title="No live technicians"
                message="Positions appear here when technicians share GPS from the mobile app."
              />
            </template>
            <Column field="tech_id" header="Tech ID" />
            <Column field="lat" header="Lat" :body="({ data }) => formatCoordinate(data.lat)" />
            <Column field="lng" header="Lng" :body="({ data }) => formatCoordinate(data.lng)" />
            <Column field="age_seconds" header="Age (s)" :body="({ data }) => data.age_seconds ?? '—'" />
            <Column field="speed_mph" header="Speed (mph)" :body="({ data }) => formatNumber(data.speed_mph)" />
            <Column field="heading_deg" header="Heading" :body="({ data }) => data.heading_deg ?? '—'" />
          </DataTable>
        </TabPanel>

        <TabPanel value="history">
          <div class="history-filters">
            <Select
              v-model="selectedTechId"
              :options="historyTechOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select technician"
              class="w-full"
            />
            <DatePicker
              v-model="selectedDate"
              dateFormat="yy-mm-dd"
              showIcon
              class="w-full"
              placeholder="Pick a date"
            />
            <Button
              label="Refresh"
              icon="pi pi-refresh"
              :disabled="!selectedTechId"
              @click="loadHistory"
            />
          </div>

          <div v-if="!selectedTechId" class="muted">Choose a technician from the live list to see history.</div>

          <div v-else>
            <div class="history-meta">
              <span>Points: {{ historyMeta?.count ?? historyEntries.length }}</span>
              <span v-if="historyMeta?.date">Date: {{ historyMeta.date }}</span>
            </div>
            <div v-if="historyLoading" class="spinner-wrap"><ProgressSpinner /></div>
            <DataTable
              v-else
              :value="historyEntries"
              striped-rows
              responsiveLayout="scroll"
            >
              <template #empty>
                <EmptyState
                  icon="pi pi-map"
                  title="No location points"
                  message="No GPS history was recorded for this technician on the chosen day."
                />
              </template>
              <Column
                field="recorded_at"
                header="Timestamp"
                :body="({ data }) => formatTimestamp(data.recorded_at)"
              />
              <Column field="lat" header="Lat" :body="({ data }) => formatCoordinate(data.lat)" />
              <Column field="lng" header="Lng" :body="({ data }) => formatCoordinate(data.lng)" />
              <Column
                field="speed_mph"
                header="Speed (mph)"
                :body="({ data }) => formatNumber(data.speed_mph)"
              />
              <Column
                field="heading_deg"
                header="Heading"
                :body="({ data }) => (data.heading_deg ?? '—')"
              />
              <Column
                field="accuracy_meters"
                header="Accuracy (m)"
                :body="({ data }) => formatNumber(data.accuracy_meters, 0)"
              />
            </DataTable>
          </div>
        </TabPanel>
        </TabPanels>
      </Tabs>
    </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatDateTime as formatTimestamp, formatNumber as fmtNumber, formatTime } from "../composables/useFormatters";
import EmptyState from "../components/EmptyState.vue";
import Button from "primevue/button";
import DatePicker from "primevue/datepicker";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Select from "primevue/select";
import ProgressSpinner from "primevue/progressspinner";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const liveTechs = ref([]);
const liveLoading = ref(false);
const lastRefresh = ref(null);

const selectedTechId = ref(null);
const selectedDate = ref(new Date());
const historyEntries = ref([]);
const historyMeta = ref(null);
const historyLoading = ref(false);

let liveTimer;

const historyTechOptions = computed(() =>
  liveTechs.value.map((tech) => ({
    label: tech.tech_id,
    value: tech.tech_id,
  }))
);

const lastRefreshLabel = computed(() =>
  formatTime(lastRefresh.value, {
    options: { hour: "2-digit", minute: "2-digit", second: "2-digit" },
  })
);

function formatCoordinate(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toFixed(5);
}

function formatNumber(value, digits = 1) {
  return fmtNumber(value, { digits });
}

async function loadLiveTechs() {
  liveLoading.value = true;
  try {
    const data = await api.get("/api/gps/technicians/live");
    liveTechs.value = Array.isArray(data) ? data : [];
    lastRefresh.value = new Date();
  } finally {
    liveLoading.value = false;
  }
}

async function loadHistory() {
  if (!selectedTechId.value || !selectedDate.value) return;
  historyLoading.value = true;
  try {
    const isoDate = selectedDate.value.toISOString().slice(0, 10);
    const data = await api.get(
      `/api/gps/technicians/${encodeURIComponent(selectedTechId.value)}/history?date=${isoDate}`
    );
    historyEntries.value = Array.isArray(data?.points) ? data.points : [];
    historyMeta.value = data;
  } finally {
    historyLoading.value = false;
  }
}

watch(
  () => liveTechs.value,
  (list) => {
    if (!selectedTechId.value && list.length) {
      selectedTechId.value = list[0].tech_id;
    }
  }
);

watch([selectedTechId, selectedDate], ([techId]) => {
  if (techId) {
    loadHistory();
  }
});

onMounted(() => {
  loadLiveTechs();
  liveTimer = setInterval(loadLiveTechs, 30000);
});

onUnmounted(() => {
  if (liveTimer) {
    clearInterval(liveTimer);
  }
});
</script>

<style scoped>
.gps-view {
  display: flex;
  flex-direction: column;
}

.live-header,
.history-filters {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 1rem;
  align-items: center;
  margin-bottom: 1rem;
}

.last-refresh {
  text-align: right;
}

.history-filters {
  align-items: flex-start;
}

.history-meta {
  display: flex;
  gap: 1rem;
  margin-bottom: 0.75rem;
  color: var(--text-secondary);
}

.muted {
  color: var(--text-secondary);
  font-size: 0.9rem;
}
</style>

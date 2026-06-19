<template>
    <section class="voice-view view-card" data-testid="voice-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title" data-testid="voice-title">Voice + Telephony</h2>
        </template>
        <template #end>
          <Button
            label="Refresh"
            icon="pi pi-refresh"
            :loading="loading"
            class="p-button-text"
            @click="loadVoice"
            data-testid="voice-refresh"
          />
        </template>
      </Toolbar>

      <Tabs v-model:value="activeTab" class="voice-tabs" data-testid="voice-tabs">
        <TabList>
          <Tab
            v-for="key in tabKeys"
            :key="`tab-${key}`"
            :value="key"
            :data-testid="`voice-tab-btn-${key}`"
          >{{ tabLabels[key] }}</Tab>
        </TabList>
        <TabPanels>
        <TabPanel
          v-for="key in tabKeys"
          :key="key"
          :value="key"
          :data-testid="`voice-tab-${key}`"
        >
          <div v-if="loading" class="spinner-wrap" data-testid="voice-loading">
            <ProgressSpinner />
          </div>
          <DataTable
        class="clickable-rows"
            v-else
            :value="filteredCallsForTab(key)"
            striped-rows
            responsiveLayout="scroll"
            emptyMessage="No calls"
            
            :data-testid="`voice-table-${key}`"
            @row-click="openCall"
          >
            <Column field="call_id" header="Call ID" />
            <Column field="direction" header="Direction" />
            <Column field="from_number" header="From" />
            <Column field="to_number" header="To" />
            <Column field="duration" header="Duration" :body="({ data }) => formatDuration(data.duration)" />
            <Column field="status" header="Status" />
            <Column field="recorded_at" header="Recorded" :body="({ data }) => formatTimestamp(data.recorded_at)" />
          </DataTable>
        </TabPanel>
        </TabPanels>
      </Tabs>

      <Dialog
        v-model:visible="audioDialogVisible"
        header="Call recording"
        modal
        class="voice-dialog"
        data-testid="voice-audio-dialog"
        :closable="false"
      >
        <div class="dialog-body" data-testid="voice-dialog-body">
          <div v-if="selectedCall">
            <p><strong>Call ID:</strong> {{ selectedCall.call_id || "—" }}</p>
            <p>
              <strong>Direction:</strong>
              {{ selectedCall.direction ? selectedCall.direction.replace(/\b\w/g, (c) => c.toUpperCase()) : "—" }}
            </p>
            <p><strong>From:</strong> {{ selectedCall.from_number || "—" }}</p>
            <p><strong>To:</strong> {{ selectedCall.to_number || "—" }}</p>
            <p><strong>Status:</strong> {{ selectedCall.status || "—" }}</p>
          </div>
          <div class="audio-player" data-testid="voice-audio-player-wrapper">
            <audio
              v-if="audioSource"
              controls
              :src="audioSource"
              class="w-full"
              data-testid="voice-audio-player"
            />
            <p v-else class="muted">No recording is available for this call.</p>
          </div>
        </div>
        <template #footer>
          <Button
            label="Close"
            severity="secondary"
            @click="closeDialog"
            data-testid="voice-audio-close"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import ProgressSpinner from "primevue/progressspinner";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const voiceCalls = ref([]);
const loading = ref(true);
const activeTab = ref("all");
const audioDialogVisible = ref(false);
const selectedCall = ref(null);

const tabKeys = ["all", "inbound", "outbound", "missed", "voicemail"];
const tabLabels = {
  all: "All",
  inbound: "Inbound",
  outbound: "Outbound",
  missed: "Missed",
  voicemail: "Voicemail",
};

const normalizeValue = (value) => (value ?? "").toLowerCase();

const tabFilters = {
  inbound: (item) => normalizeValue(item.direction) === "inbound",
  outbound: (item) => normalizeValue(item.direction) === "outbound",
  missed: (item) => normalizeValue(item.status) === "missed",
  voicemail: (item) => normalizeValue(item.status) === "voicemail",
};

const audioSource = computed(() => {
  if (!selectedCall.value) return null;
  return (
    selectedCall.value.recording_url ||
    selectedCall.value.audio_url ||
    selectedCall.value.recording?.url ||
    null
  );
});

function filteredCallsForTab(tabKey) {
  const filter = tabFilters[tabKey] ?? (() => true);
  return voiceCalls.value.filter(filter);
}

function formatDuration(value) {
  if (value === null || value === undefined) return "—";
  const seconds = Number(value);
  if (Number.isNaN(seconds)) return "—";
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (minutes) {
    return `${minutes}m ${secs}s`;
  }
  return `${secs}s`;
}

function formatTimestamp(value) {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch (error) {
    return value;
  }
}

async function loadVoice() {
  loading.value = true;
  try {
    const data = await api.get("/api/voice");
    voiceCalls.value = Array.isArray(data) ? data : data?.items ?? [];
  } finally {
    loading.value = false;
  }
}

function openCall(event) {
  selectedCall.value = event.data;
  audioDialogVisible.value = true;
}

function closeDialog() {
  audioDialogVisible.value = false;
  selectedCall.value = null;
}

onMounted(loadVoice);
</script>

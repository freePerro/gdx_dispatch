<template>
  <section class="recurring-streams-view view-card">
    <Toolbar>
      <template #start>
        <Button
          v-tooltip="'Back to Forecasting'"
          icon="pi pi-arrow-left"
          severity="secondary"
          text
          aria-label="Back to Forecasting"
          @click="$router.push('/forecasting')"
        />
        <h1 class="view-heading">Recurring Payments</h1>
        <span class="subtle">Observed from bank activity + manual labels</span>
      </template>
      <template #end>
        <Button label="Detect Now" icon="pi pi-search" severity="secondary" @click="onDetectNow" :loading="detecting" />
        <Button label="+ Mark Recurring" icon="pi pi-plus" @click="openCreate" />
      </template>
    </Toolbar>

    <Tabs v-model:value="activeTab" @update:value="onTabChange">
      <TabList>
        <Tab value="suggested">Suggested <Badge v-if="suggestedCount" :value="suggestedCount" severity="info" /></Tab>
        <Tab value="active">Active <Badge v-if="activeCount" :value="activeCount" severity="success" /></Tab>
        <Tab value="ended">Ended</Tab>
      </TabList>
      <TabPanels>
        <TabPanel value="suggested">
          <p class="hint">The detector found these patterns in your bank transactions. Confirm to track, or dismiss.</p>
          <RecurringStreamsTable
            :streams="filteredStreams('suggested')"
            :loading="loading"
            :empty-text="loading ? 'Loading…' : 'No suggestions — try Detect Now after a bank sync.'"
            mode="suggested"
            @confirm="onConfirm"
            @dismiss="onDismiss"
            @edit="openEdit"
          />
        </TabPanel>
        <TabPanel value="active">
          <p class="hint">Live recurring payments. Edit to refine cadence/term. End when paid off or cancelled.</p>
          <RecurringStreamsTable
            :streams="filteredStreams('active')"
            :loading="loading"
            :empty-text="loading ? 'Loading…' : 'No active recurring payments yet. Confirm a suggestion or mark one manually.'"
            mode="active"
            @edit="openEdit"
            @end="openEnd"
          />
        </TabPanel>
        <TabPanel value="ended">
          <p class="hint">Read-only history. Past hits are preserved for analytics.</p>
          <RecurringStreamsTable
            :streams="filteredStreams('ended')"
            :loading="loading"
            :empty-text="loading ? 'Loading…' : 'No ended streams.'"
            mode="ended"
          />
        </TabPanel>
      </TabPanels>
    </Tabs>

    <RecurringStreamForm
      v-model:visible="formOpen"
      :stream="editing"
      :submitting="submitting"
      @submit="onFormSubmit"
    />

    <EndRecurringDialog
      v-model:visible="endOpen"
      :stream="ending"
      :submitting="submitting"
      @submit="onEndSubmit"
    />
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import Toolbar from 'primevue/toolbar';
import Button from 'primevue/button';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import TabPanels from 'primevue/tabpanels';
import TabPanel from 'primevue/tabpanel';
import Badge from 'primevue/badge';

import RecurringStreamsTable from '../components/forecasting/RecurringStreamsTable.vue';
import RecurringStreamForm from '../components/forecasting/RecurringStreamForm.vue';
import EndRecurringDialog from '../components/forecasting/EndRecurringDialog.vue';
import { useRecurringStreams } from '../composables/useRecurringStreams';

const recurring = useRecurringStreams();
const { streams, loading } = recurring;

const activeTab = ref('suggested');
const formOpen = ref(false);
const endOpen = ref(false);
const editing = ref(null);
const ending = ref(null);
const submitting = ref(false);
const detecting = ref(false);

const _ENDED = ['paid_off', 'cancelled', 'expired'];

function filteredStreams(group) {
  if (group === 'ended') {
    return streams.value.filter((s) => _ENDED.includes(s.status));
  }
  return streams.value.filter((s) => s.status === group);
}

const suggestedCount = computed(() => filteredStreams('suggested').length);
const activeCount = computed(() => filteredStreams('active').length);

async function refreshAll() {
  // single call — server returns everything, we filter client-side per tab
  await recurring.list();
}

function onTabChange() {
  // No-op — data already loaded; tabs just filter.
}

function openCreate() {
  editing.value = null;
  formOpen.value = true;
}

function openEdit(stream) {
  editing.value = { ...stream };
  formOpen.value = true;
}

function openEnd(stream) {
  ending.value = stream;
  endOpen.value = true;
}

async function onFormSubmit(payload) {
  submitting.value = true;
  try {
    if (editing.value && editing.value.id) {
      await recurring.patch(editing.value.id, payload);
    } else {
      await recurring.create(payload);
    }
    formOpen.value = false;
    editing.value = null;
    await refreshAll();
  } finally {
    submitting.value = false;
  }
}

async function onEndSubmit(payload) {
  if (!ending.value) return;
  submitting.value = true;
  try {
    await recurring.end(ending.value.id, payload);
    endOpen.value = false;
    ending.value = null;
    await refreshAll();
  } finally {
    submitting.value = false;
  }
}

async function onConfirm(stream) {
  await recurring.confirm(stream.id);
  await refreshAll();
}

async function onDismiss(stream) {
  await recurring.dismiss(stream.id);
  await refreshAll();
}

async function onDetectNow() {
  detecting.value = true;
  try {
    await recurring.detectNow();
    await refreshAll();
  } finally {
    detecting.value = false;
  }
}

onMounted(refreshAll);
</script>

<style scoped>
.recurring-streams-view { padding: 1rem; }
.view-heading { font-size: 1.5rem; font-weight: 600; margin: 0 0.75rem 0 0; display: inline-block; }
.subtle { color: var(--p-text-muted-color); font-size: 0.875rem; }
.hint { color: var(--p-text-muted-color); font-size: 0.875rem; padding: 0.5rem 0 1rem; }
</style>

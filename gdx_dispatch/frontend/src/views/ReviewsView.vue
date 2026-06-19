<template>
    <section class="view-card reviews-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Reviews Inbox</h2>
        </template>
        <template #end>
          <Button
            icon="pi pi-sync"
            label="Refresh"
            text
            @click="loadReviews"
            data-testid="reviews-refresh"
          />
        </template>
      </Toolbar>

      <div class="filter-row">
        <Select
          v-model="sourceFilter"
          :options="sourceOptions"
          option-label="label"
          option-value="value"
          placeholder="Source"
          class="w-full"
          data-testid="reviews-source-filter"
        />
        <DatePicker
          v-model="reviewRange"
          selection-mode="range"
          date-format="yy-mm-dd"
          placeholder="Review date"
          show-icon
          class="w-full"
          data-testid="reviews-date-filter"
        />
        <div class="toggle-field">
          <label class="toggle-label" for="flagged-only">Flagged only</label>
          <ToggleSwitch
            id="flagged-only"
            v-model="flaggedOnly"
            on-label="Yes"
            off-label="No"
            class="toggle-control"
            data-testid="reviews-flagged-toggle"
          />
        </div>
      </div>

      <Tabs v-model:value="activeTab" class="view-tabs" data-testid="reviews-tabs">
        <TabList>
          <Tab v-for="tab in tabDefinitions" :key="tab.key" :value="tab.key">
            {{ buildTabHeader(tab) }}
          </Tab>
        </TabList>
        <TabPanels>
          <TabPanel v-for="tab in tabDefinitions" :key="tab.key" :value="tab.key">
            <p class="tab-note">{{ tab.note }}</p>
          </TabPanel>
        </TabPanels>
      </Tabs>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
      responsiveLayout="scroll"
        v-else
        :value="filteredReviews"
        paginator
        :rows="15"
        striped-rows
        class="clickable-row"
      >
        <Column field="source" header="Source">
          <template #body="{ data }">{{ sourceLabel(data.source) }}</template>
        </Column>
        <Column field="rating" header="Rating">
          <template #body="{ data }">
            <Rating :value="Number(data.rating)" :cancel="false" :readonly="true" :stars="5" />
          </template>
        </Column>
        <Column field="customer" header="Customer" />
        <Column field="content" header="Comment" />
        <Column field="responded" header="Responded">
          <template #body="{ data }">
            <Tag :value="data.responded ? 'Responded' : 'Pending'" :severity="data.responded ? 'success' : 'warning'" />
          </template>
        </Column>
        <Column field="created_at" header="Received">
          <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
        </Column>
        <Column header="Actions" style="width: 140px">
          <template #body="{ data }">
            <Button
              icon="pi pi-reply"
              label="Respond"
              severity="primary"
              size="small"
              @click.stop="openResponseDialog(data)"
              data-testid="reviews-respond-row"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="selectedReview ? `Respond to ${selectedReview.customer || 'review'}` : 'Respond to review'"
        modal
        :style="{ width: '520px' }"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label>Response</label>
            <Textarea
              v-model="responseForm.message"
              rows="4"
              class="w-full"
              data-testid="reviews-dialog-response"
            />
          </div>
          <div class="form-field toggle-field">
            <label class="toggle-label">Flag review</label>
            <ToggleSwitch
              v-model="responseForm.flagged"
              on-label="Yes"
              off-label="No"
              class="toggle-control"
              data-testid="reviews-dialog-flag"
            />
          </div>
        </div>
        <template #footer>
          <Button
            label="Cancel"
            severity="secondary"
            @click="closeResponseDialog"
            data-testid="reviews-dialog-cancel"
          />
          <Button
            label="Send"
            icon="pi pi-check"
            class="primary"
            :loading="sendingResponse"
            @click="postResponse"
            data-testid="reviews-dialog-send"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatTimestamp } from '../utils/formatTimestamp';
import Button from 'primevue/button';
import Toolbar from 'primevue/toolbar';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import ProgressSpinner from 'primevue/progressspinner';
import Rating from 'primevue/rating';
import Select from 'primevue/select';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import ToggleSwitch from 'primevue/toggleswitch';

const api = useApiWithToast();

const reviews = ref([]);
const loading = ref(true);
const loadError = ref(null);
const activeTab = ref('all');
const sourceFilter = ref(null);
const reviewRange = ref(null);
const flaggedOnly = ref(false);
const showDialog = ref(false);
const selectedReview = ref(null);
const sendingResponse = ref(false);

const responseForm = ref({
  message: '',
  flagged: false,
});

const sourceOptions = [
  { label: 'All sources', value: null },
  { label: 'Google', value: 'google' },
  { label: 'Yelp', value: 'yelp' },
  { label: 'Facebook', value: 'facebook' },
];

const tabDefinitions = [
  { key: 'all', label: 'All reviews', note: 'Every review in the inbox.' },
  { key: 'unresponded', label: 'Unresponded', note: 'Reviews waiting for a reply.' },
  { key: 'flagged', label: 'Flagged', note: 'Marked as needing attention.' },
];

const tabMatchers = {
  all: () => true,
  unresponded: (review) => !review.responded,
  flagged: (review) => Boolean(review.flagged),
};

const currentTabKey = computed(() => activeTab.value || 'all');

const tabCounts = computed(() =>
  tabDefinitions.reduce((acc, tab) => {
    const matcher = tabMatchers[tab.key] || tabMatchers.all;
    acc[tab.key] = reviews.value.filter(matcher).length;
    return acc;
  }, {})
);

const filteredReviews = computed(() => {
  let list = reviews.value
    .slice()
    .sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime());

  if (sourceFilter.value) {
    list = list.filter((review) => review.source === sourceFilter.value);
  }

  if (flaggedOnly.value) {
    list = list.filter((review) => review.flagged);
  }

  if (reviewRange.value?.length) {
    const [start, end] = reviewRange.value;
    if (start) {
      const startTime = new Date(start).setHours(0, 0, 0, 0);
      list = list.filter((review) => {
        if (!review.created_at) return false;
        const entryTime = new Date(review.created_at).getTime();
        if (end) {
          const endTime = new Date(end).setHours(23, 59, 59, 999);
          return entryTime >= startTime && entryTime <= endTime;
        }
        return entryTime >= startTime;
      });
    }
    if (end) {
      const endTime = new Date(end).setHours(23, 59, 59, 999);
      list = list.filter((review) => {
        if (!review.created_at) return false;
        const entryTime = new Date(review.created_at).getTime();
        return entryTime <= endTime;
      });
    }
  }

  const matcher = tabMatchers[currentTabKey.value] || tabMatchers.all;
  return list.filter(matcher);
});

function buildTabHeader(tab) {
  const count = tabCounts.value[tab.key] ?? 0;
  return count ? `${tab.label} (${count})` : tab.label;
}

function formatDate(value) {
  return formatTimestamp(value, 'date');
}

function sourceLabel(value) {
  const option = sourceOptions.find((item) => item.value === value);
  return option?.label || value?.toUpperCase() || 'Unknown';
}

async function loadReviews() {
  loading.value = true;
  try {
    const data = await api.get('/api/reviews');
    reviews.value = Array.isArray(data) ? data : data?.items || [];
  } catch (err) {
    reviews.value = [];
    loadError.value = err?.message || 'Unable to load reviews';
  } finally {
    loading.value = false;
  }
}

function openResponseDialog(review) {
  selectedReview.value = review;
  responseForm.value = {
    message: '',
    flagged: Boolean(review.flagged),
  };
  showDialog.value = true;
}

function closeResponseDialog() {
  showDialog.value = false;
  selectedReview.value = null;
}

async function postResponse() {
  if (!selectedReview.value) return;
  sendingResponse.value = true;
  try {
    const payload = {
      message: responseForm.value.message,
      flagged: responseForm.value.flagged,
    };
    await api.post(`/api/reviews/${selectedReview.value.id}/responses`, payload, { successMessage: 'Response posted' });
    await loadReviews();
    closeResponseDialog();
  } finally {
    sendingResponse.value = false;
  }
}

onMounted(() => {
  loadReviews();
});
</script>

<style scoped>
.reviews-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.filter-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.75rem;
}

.toggle-field {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.toggle-label {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}

.view-tabs {
  --p-tabview-content-padding: 0;
}

.tab-note {
  margin: 0;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 0.75rem;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.full-width {
  grid-column: 1 / -1;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}

.primary {
  min-width: 90px;
}
</style>

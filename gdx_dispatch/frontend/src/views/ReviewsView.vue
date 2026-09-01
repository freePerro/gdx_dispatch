<template>
    <section class="view-card reviews-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Reviews</h2>
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

      <!-- #473, option B (2026-09-01): reviews live on Google. This page lists
           the ratings the office has recorded; nothing ingests Google, Yelp or
           Facebook, so the platform filter and the "Flagged" tab (no such
           column) were promises the page could not keep. Gone, not hidden. -->
      <p class="muted reviews-note" data-testid="reviews-note">
        Customer reviews live on Google. This list is the ratings your office has recorded.
        <router-link to="/settings" data-testid="reviews-settings-link">Set your Google review link</router-link>
        and every receipt and invoice will ask for one.
      </p>
      <div class="filter-row">
        <DatePicker
          v-model="reviewRange"
          selection-mode="range"
          date-format="yy-mm-dd"
          placeholder="Review date"
          show-icon
          class="w-full"
          data-testid="reviews-date-filter"
        />
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
        <template #empty>
          <EmptyState
            icon="pi pi-star"
            title="No ratings recorded yet"
            message="Customer reviews live on Google — set your Google review link in Settings and every receipt and invoice will ask for one."
          />
        </template>
        <Column field="source" header="Source">
          <template #body="{ data }">{{ sourceLabel(data.source) }}</template>
        </Column>
        <Column field="rating" header="Rating">
          <template #body="{ data }">
            <Rating :model-value="Number(data.rating)" :cancel="false" :readonly="true" :stars="5" />
          </template>
        </Column>
        <Column field="customer" header="Customer" />
        <Column field="content" header="Comment" />
        <Column field="created_at" header="Received">
          <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
        </Column>
      </DataTable>

    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatTimestamp } from '../utils/formatTimestamp';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Toolbar from 'primevue/toolbar';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import ProgressSpinner from 'primevue/progressspinner';
import Rating from 'primevue/rating';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';

const api = useApiWithToast();

const reviews = ref([]);
const loading = ref(true);
const loadError = ref(null);
const activeTab = ref('all');
const reviewRange = ref(null);


// Display labels for a stored `source`. There is no platform FILTER any more:
// nothing ingests these platforms, and every row on this tenant has no source.
const sourceLabels = { google: 'Google', yelp: 'Yelp', facebook: 'Facebook', office: 'Office' };

const tabDefinitions = [
  { key: 'all', label: 'All ratings', note: 'Every rating the office has recorded.' },
];

const tabMatchers = {
  all: () => true,
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
  // A row with no recorded source reads "Unknown" — never a platform name
  // the row does not carry.
  return sourceLabels[value] || value?.toUpperCase() || 'Unknown';
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




onMounted(() => {
  loadReviews();
});
</script>

<style scoped>
.reviews-note { margin: 0 0 0.5rem; font-size: 0.9rem; }
.reviews-note a { text-decoration: underline; }
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

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}

.primary {
  min-width: 90px;
}
</style>

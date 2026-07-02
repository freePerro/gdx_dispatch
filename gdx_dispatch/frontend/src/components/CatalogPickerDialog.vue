<!--
  CatalogPickerDialog — shared "Add from Catalog" picker used by every surface
  that pulls line items from catalogs (invoices via LineItemEditor, estimates,
  etc.).

  It shows ONE TAB PER REAL CATALOG the tenant owns, sourced from /api/catalogs
  (the same list as the Catalogs page), so the picker always matches what the
  tenant actually has. The tenant's own catalogs load their items up front from
  the /api/catalogs/all-items aggregator; big read-only catalogs (CHI Doors/
  Parts) lazy-load their first page when their tab is opened. A "Built-in"
  starter tab is always offered.

  Contract:
    v-model:visible  → open/close the dialog (parent owns visibility).
    @add(items)      → emitted with the array of selected catalog items when the
                       user confirms. Each item is normalized to:
                       { id, name, sku, description, category, pricing_category,
                         cost, price }. The parent decides how to turn those into
                         its own line shape (invoice unit_price vs estimate
                         recomputeSell, etc.). Carrying cost + pricing_category
                         lets the backend tier engine mark the line up.

  Why a component (not just a composable): the tabs + table + search markup is
  identical everywhere, so sharing the template too keeps every surface in sync.
-->
<template>
  <Dialog
    :visible="visible"
    @update:visible="$emit('update:visible', $event)"
    :header="header"
    modal
    :style="{ width: '720px' }"
    data-testid="catalog-picker"
  >
    <InputText
      v-model="catalogSearch"
      placeholder="Search catalog…"
      class="w-full"
      data-testid="catalog-picker-search"
      style="margin-bottom: 1rem"
    />
    <div v-if="catalogLoading" class="muted">Loading catalogs…</div>
    <template v-else>
      <Tabs v-model:value="activeCatalogGroup" data-testid="catalog-picker-tabs" scrollable>
        <TabList>
          <Tab
            v-for="t in catalogTabs"
            :key="t.key"
            :value="t.key"
            :data-testid="`catalog-picker-tab-${t.key}`"
          >
            {{ t.name }} <span class="catalog-tab-count">({{ (t.total || 0).toLocaleString() }})</span>
          </Tab>
        </TabList>
      </Tabs>
      <div v-if="catalogGroupLoading" class="muted" style="padding: 1rem 0">Loading items…</div>
      <template v-else>
        <div v-if="catalogTruncatedHint" class="catalog-truncate-hint">{{ catalogTruncatedHint }}</div>
        <DataTable
          responsiveLayout="scroll"
          :value="filteredCatalogItems"
          :paginator="filteredCatalogItems.length > 10"
          :rows="10"
          selectionMode="multiple"
          v-model:selection="selectedCatalogItems"
          dataKey="_key"
          stripedRows
          data-testid="catalog-picker-table"
        >
          <template #empty>
            <span class="muted">No matching items in this catalog.</span>
          </template>
          <Column selectionMode="multiple" style="width: 3rem" />
          <Column field="name" header="Item" sortable />
          <Column field="category" header="Category" style="width: 140px" />
          <Column header="Price" style="width: 100px">
            <template #body="{ data }">{{ formatMoney(data.price) }}</template>
          </Column>
        </DataTable>
      </template>
    </template>
    <template #footer>
      <Button label="Cancel" severity="secondary" @click="$emit('update:visible', false)" />
      <Button
        :label="`Add ${selectedCatalogItems.length} item${selectedCatalogItems.length !== 1 ? 's' : ''}`"
        icon="pi pi-plus"
        :disabled="!selectedCatalogItems.length"
        data-testid="catalog-picker-add"
        @click="confirmAdd"
      />
    </template>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue';
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import Dialog from 'primevue/dialog';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import { useApi } from '../composables/useApi';
import { formatMoney } from '../composables/useFormatters';

const props = defineProps({
  visible: { type: Boolean, default: false },
  header: { type: String, default: 'Add from Catalog' },
});
const emit = defineEmits(['update:visible', 'add']);

const api = useApi();

const catalogSearch = ref('');
const selectedCatalogItems = ref([]);
const catalogLoaded = ref(false);
const catalogLoading = ref(false);
const catalogGroupLoading = ref(false);
const activeCatalogGroup = ref(null);
// One tab per real catalog: { key, name, total, lazy }. `key` is the catalog id
// (or 'builtin'). `lazy` catalogs (big read-only CHI ones) fetch items on first
// activation; everything else is loaded up front from the all-items aggregator.
const catalogTabs = ref([]);
// key -> mapped item array.
const catalogItemsByGroup = ref({});

const BUILTIN_KEY = 'builtin';
// The catalog items endpoint caps per_page at 100; catalogs larger than this
// show a "first N of total" hint. We deliberately don't paginate the huge
// read-only CHI catalog — the first page is enough here.
const LAZY_PAGE_SIZE = 100;

// Built-in service/part rates offered even when a tenant has no catalogs seeded.
const BUILT_IN_PARTS = [
  { id: 'bi-1', name: 'Torsion Spring Replacement', category: 'Springs', price: 185 },
  { id: 'bi-2', name: 'Extension Spring Replacement (pair)', category: 'Springs', price: 150 },
  { id: 'bi-3', name: 'Belt Drive Opener Installation', category: 'Openers', price: 450 },
  { id: 'bi-4', name: 'Chain Drive Opener Installation', category: 'Openers', price: 350 },
  { id: 'bi-5', name: 'Lift Cable & Drum Replacement', category: 'Parts', price: 45 },
  { id: 'bi-6', name: 'Nylon Roller Replacement (set of 12)', category: 'Parts', price: 120 },
  { id: 'bi-7', name: 'Door Panel/Section Replacement', category: 'Doors', price: 350 },
  { id: 'bi-8', name: 'Bottom Seal / Weatherstrip', category: 'Parts', price: 65 },
  { id: 'bi-9', name: 'Safety Sensor Replacement', category: 'Parts', price: 75 },
  { id: 'bi-10', name: 'Track Repair / Realignment', category: 'Parts', price: 125 },
  { id: 'bi-11', name: 'New Garage Door Installation', category: 'Doors', price: 1200 },
  { id: 'bi-12', name: 'Tune-Up & Maintenance', category: 'Labor', price: 95 },
  { id: 'bi-13', name: 'Service Call / Diagnostic', category: 'Labor', price: 85 },
  { id: 'bi-14', name: 'Emergency / After-Hours Fee', category: 'Labor', price: 150 },
  { id: 'bi-15', name: 'Wireless Keypad / Remote', category: 'Parts', price: 55 },
  { id: 'bi-16', name: 'Door Insulation Kit', category: 'Doors', price: 200 },
  { id: 'bi-17', name: 'Commercial Door Service', category: 'Labor', price: 500 },
];

// Normalize a catalog item (from all-items or /catalogs/:id/items) into the row
// shape the table + consumers expect. Carries cost + pricing_category so the
// backend tier engine can mark the resulting line up.
function mapCatalogItem(raw, groupKey) {
  return {
    _group: groupKey,
    _key: `${groupKey}:${raw.id}`,
    id: raw.id,
    name: raw.name || '',
    sku: raw.sku || '',
    description: raw.description_display || raw.description || raw.name || '',
    category: raw.category || raw.pricing_category || '',
    pricing_category: raw.pricing_category || '',
    cost: Number(raw.cost || 0),
    price: Number(raw.price || 0),
  };
}

const activeTabMeta = computed(() =>
  catalogTabs.value.find((t) => t.key === activeCatalogGroup.value) || null,
);

// Rows for the active tab, narrowed by the search box (scoped to the tab).
const filteredCatalogItems = computed(() => {
  const inGroup = catalogItemsByGroup.value[activeCatalogGroup.value] || [];
  const q = catalogSearch.value.trim().toLowerCase();
  if (!q) return inGroup;
  return inGroup.filter((it) =>
    [it.name, it.description, it.sku, it.category]
      .filter(Boolean)
      .some((v) => String(v).toLowerCase().includes(q)),
  );
});

// Shown when a lazy catalog is displaying only part of its items.
const catalogTruncatedHint = computed(() => {
  const meta = activeTabMeta.value;
  if (!meta || !meta.lazy) return null;
  const loaded = (catalogItemsByGroup.value[meta.key] || []).length;
  if (loaded && meta.total && loaded < meta.total) {
    return `Showing first ${loaded.toLocaleString()} of ${meta.total.toLocaleString()} — search to narrow.`;
  }
  return null;
});

// Fetch a lazy catalog's items the first time its tab is opened.
async function loadCatalogGroup(key) {
  if (key === BUILTIN_KEY) return;
  if (catalogItemsByGroup.value[key]) return; // already loaded
  const meta = catalogTabs.value.find((t) => t.key === key);
  if (!meta || !meta.lazy) return;
  catalogGroupLoading.value = true;
  try {
    const res = await api.get(`/api/catalogs/${key}/items?per_page=${LAZY_PAGE_SIZE}`, { suppressErrorToast: true });
    const rows = Array.isArray(res) ? res : res?.items || [];
    catalogItemsByGroup.value = {
      ...catalogItemsByGroup.value,
      [key]: rows.map((r) => mapCatalogItem(r, key)),
    };
  } catch (e) {
    catalogItemsByGroup.value = { ...catalogItemsByGroup.value, [key]: [] };
  } finally {
    catalogGroupLoading.value = false;
  }
}

// Lazy-load a catalog's items whenever its tab becomes active.
watch(activeCatalogGroup, (key) => {
  if (key) loadCatalogGroup(key);
});

async function loadCatalogs() {
  if (catalogLoaded.value) return;
  catalogLoading.value = true;
  try {
    // Tabs mirror the tenant's real catalogs. /api/catalogs is the authoritative
    // list (incl. read-only CHI Doors/Parts); /api/catalogs/all-items returns
    // every item across the tenant's own (non-read-only) catalogs in one shot,
    // tagged with catalog_id — group those eagerly, lazy-load the big read-only
    // ones only when their tab is opened.
    const [catsRes, itemsRes] = await Promise.allSettled([
      api.get('/api/catalogs', { suppressErrorToast: true }),
      api.get('/api/catalogs/all-items', { suppressErrorToast: true }),
    ]);
    const cats = catsRes.status === 'fulfilled'
      ? (Array.isArray(catsRes.value) ? catsRes.value : catsRes.value?.items || catsRes.value?.data || [])
      : [];
    const allItems = itemsRes.status === 'fulfilled'
      ? (Array.isArray(itemsRes.value) ? itemsRes.value : itemsRes.value?.items || [])
      : [];

    const eagerByCat = {};
    for (const it of allItems) {
      const cid = it.catalog_id;
      if (!cid) continue;
      (eagerByCat[cid] = eagerByCat[cid] || []).push(it);
    }

    const tabs = [];
    const itemsByGroup = {};
    for (const c of cats) {
      const eager = eagerByCat[c.id];
      if (eager && eager.length) {
        tabs.push({ key: c.id, name: c.name || 'Catalog', total: eager.length, lazy: false });
        itemsByGroup[c.id] = eager.map((r) => mapCatalogItem(r, c.id));
      } else if (Number(c.item_count) > 0) {
        tabs.push({ key: c.id, name: c.name || 'Catalog', total: Number(c.item_count), lazy: true });
      }
      // Empty catalogs get no tab.
    }
    tabs.push({ key: BUILTIN_KEY, name: 'Built-in', total: BUILT_IN_PARTS.length, lazy: false });
    itemsByGroup[BUILTIN_KEY] = BUILT_IN_PARTS.map((p) => mapCatalogItem(p, BUILTIN_KEY));

    catalogTabs.value = tabs;
    catalogItemsByGroup.value = itemsByGroup;
    activeCatalogGroup.value = tabs[0]?.key || null; // watcher lazy-loads if needed
    catalogLoaded.value = true;
  } catch (e) {
    catalogTabs.value = [{ key: BUILTIN_KEY, name: 'Built-in', total: BUILT_IN_PARTS.length, lazy: false }];
    catalogItemsByGroup.value = { [BUILTIN_KEY]: BUILT_IN_PARTS.map((p) => mapCatalogItem(p, BUILTIN_KEY)) };
    activeCatalogGroup.value = BUILTIN_KEY;
  } finally {
    catalogLoading.value = false;
  }
}

// Reset transient state + kick off loading each time the dialog opens.
watch(() => props.visible, (open) => {
  if (!open) return;
  selectedCatalogItems.value = [];
  catalogSearch.value = '';
  loadCatalogs();
});

function confirmAdd() {
  if (!selectedCatalogItems.value.length) return;
  emit('add', selectedCatalogItems.value.slice());
  emit('update:visible', false);
}
</script>

<style scoped>
.muted {
  color: var(--p-text-muted-color, #6b7280);
}
.catalog-tab-count {
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 400;
  font-size: 0.85em;
}
.catalog-truncate-hint {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.8rem;
  padding: 0.4rem 0 0.2rem;
}
</style>

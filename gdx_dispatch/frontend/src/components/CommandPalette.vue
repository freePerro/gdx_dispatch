<template>
  <Dialog
    :visible="modelValue"
    modal
    dismissable-mask
    :draggable="false"
    :style="{ width: 'min(42rem, 92vw)' }"
    class="command-palette"
    @update:visible="handleVisibilityChange"
    @hide="closePalette"
  >
    <template #header>
      <div class="palette-header">Quick Search</div>
    </template>

    <div class="palette-body" @keydown="handleKeydown">
      <InputText
        v-model="query"
        autofocus
        class="palette-input"
        placeholder="Search jobs, customers, invoices, estimates..."
        data-testid="palette-input"
      />

      <div class="results-wrap">
        <p v-if="searching" class="state-line" data-testid="palette-searching">Searching…</p>
        <p v-else-if="searchFailed" class="state-line" data-testid="palette-error">
          Search is unavailable right now — page matches still work.
        </p>

        <div
          v-for="group in visibleGroups"
          :key="group.key"
          class="result-group"
          :data-type="group.label"
          :data-testid="`palette-group-${group.key}`"
        >
          <h4>{{ group.label }}</h4>
          <button
            v-for="item in group.items"
            :key="item.key"
            type="button"
            class="result-item"
            :class="{ selected: item.flatIndex === selectedIndex }"
            :data-testid="`palette-item-${item.key}`"
            @click="navigateTo(item.to)"
            @mousemove="selectedIndex = item.flatIndex"
          >
            <i :class="item.icon" aria-hidden="true" />
            <span class="item-label">{{ item.label }}</span>
            <span v-if="item.sublabel" class="item-sublabel">{{ item.sublabel }}</span>
          </button>
        </div>

        <p v-if="showEmptyState" class="empty-state" data-testid="palette-empty">
          No results for "{{ query.trim() }}"
        </p>
      </div>
    </div>
  </Dialog>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import { QUICK_ACTIONS } from '../constants/modules';
import { useApi } from '../composables/useApi';
import { useTenantModules } from '../composables/useTenantModules';
import { useAuthStore } from '../stores/auth';
import { isTechnician } from '../constants/roles';
import { formatMoney } from '../composables/useFormatters';

const props = defineProps({
  modelValue: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(['update:modelValue']);

const router = useRouter();
const api = useApi();
const query = ref('');
const { allEnabledModules, isEnabled } = useTenantModules();
const auth = useAuthStore();

// --- Data search (/api/search) -------------------------------------------
// 2026-07-07: the palette used to filter PAGE NAMES only while its
// placeholder promised "Search jobs, customers, invoices..." — the backend
// /api/search endpoint existed but nothing called it. Now: debounced fetch,
// stale responses dropped via a request counter.
const DEBOUNCE_MS = 250;
const MIN_CHARS = 2;

const dataResults = ref(null); // null = no data query ran (short/empty term)
const searching = ref(false);
const searchFailed = ref(false);
let debounceTimer = null;
let requestSeq = 0;

watch(query, (value) => {
  if (debounceTimer) clearTimeout(debounceTimer);
  const term = value.trim();
  if (term.length < MIN_CHARS) {
    dataResults.value = null;
    searching.value = false;
    searchFailed.value = false;
    requestSeq += 1; // invalidate any in-flight response
    return;
  }
  searching.value = true;
  debounceTimer = setTimeout(() => runSearch(term), DEBOUNCE_MS);
});

async function runSearch(term) {
  const seq = ++requestSeq;
  try {
    const response = await api.get(`/api/search?q=${encodeURIComponent(term)}`);
    if (seq !== requestSeq) return; // a newer query superseded this one
    dataResults.value = response || null;
    searchFailed.value = false;
  } catch (_error) {
    if (seq !== requestSeq) return;
    dataResults.value = null;
    searchFailed.value = true;
  } finally {
    if (seq === requestSeq) searching.value = false;
  }
}

// The backend returns every section to any authenticated role; hide sections
// the user's nav doesn't include (same visibility source as the sidebar).
// UI courtesy, not security — the route guard + backend enforce access.
const visibleModuleKeys = computed(() => new Set(allEnabledModules.value.map((m) => m.key)));

const DATA_SECTIONS = [
  {
    key: 'customers',
    label: 'Customers',
    moduleKey: 'customers',
    icon: 'pi pi-user',
    toItem: (r) => ({
      key: `customer-${r.id}`,
      icon: 'pi pi-user',
      label: r.name,
      sublabel: r.phone || r.email || '',
      to: `/customers/${r.id}`,
    }),
  },
  {
    key: 'jobs',
    label: 'Jobs',
    moduleKey: 'jobs',
    icon: 'pi pi-briefcase',
    toItem: (r) => ({
      key: `job-${r.id}`,
      icon: 'pi pi-briefcase',
      label: r.number ? `#${r.number} — ${r.title}` : r.title,
      sublabel: r.customer_name || '',
      to: `/jobs/${r.id}`,
    }),
  },
  {
    key: 'invoices',
    label: 'Invoices',
    moduleKey: 'billing',
    icon: 'pi pi-dollar',
    toItem: (r) => ({
      key: `invoice-${r.id}`,
      icon: 'pi pi-dollar',
      label: `#${r.number}`,
      sublabel: [r.customer_name, formatMoney(r.total)].filter(Boolean).join(' · '),
      to: `/billing/${r.id}`,
    }),
  },
  {
    key: 'estimates',
    label: 'Estimates',
    moduleKey: 'estimates',
    icon: 'pi pi-file-edit',
    toItem: (r) => ({
      key: `estimate-${r.id}`,
      icon: 'pi pi-file-edit',
      label: r.label ? `#${r.number} — ${r.label}` : `#${r.number}`,
      sublabel: r.customer_name || '',
      to: `/estimates/${r.id}`,
    }),
  },
];

const dataGroups = computed(() => {
  const results = dataResults.value;
  if (!results) return [];
  return DATA_SECTIONS.filter(
    (section) => visibleModuleKeys.value.has(section.moduleKey) && (results[section.key] || []).length
  ).map((section) => ({
    key: section.key,
    label: section.label,
    items: results[section.key].map(section.toItem),
  }));
});

// --- Page + quick-action matches ------------------------------------------
// Nav records come straight from `allEnabledModules`, which useTenantModules
// already permission-filters (a tech never sees a module they lack the
// permission for, so Ctrl-K can't surface a hidden module either).
const pageRecords = computed(() =>
  allEnabledModules.value.map((module) => ({
    key: `page-${module.key}`,
    label: module.label,
    icon: module.icon,
    to: module.to,
  }))
);

// AppTopbar already hides the Create-job / Create-customer buttons for tech
// via `canCreate`. Mirror that here so Ctrl-K doesn't re-open those actions
// to a tech. (Quick-action gating is separate from module nav visibility.)
const actionRecords = computed(() => {
  const isTech = isTechnician(auth.user?.role || '');
  return QUICK_ACTIONS.filter((action) => {
    if (action.key === 'create-job') return isEnabled('jobs') && !isTech;
    if (action.key === 'create-customer') return isEnabled('customers') && !isTech;
    if (action.key === 'open-dispatch') return isEnabled('dispatch') && !isTech;
    return true;
  }).map((action) => ({
    key: `action-${action.key}`,
    label: action.label,
    icon: action.icon,
    to: action.to,
  }));
});

const term = computed(() => query.value.trim().toLowerCase());

const matchingPages = computed(() => {
  if (!term.value) return pageRecords.value;
  return pageRecords.value.filter((item) => item.label.toLowerCase().includes(term.value));
});

const matchingActions = computed(() => {
  if (!term.value) return actionRecords.value;
  return actionRecords.value.filter((item) => item.label.toLowerCase().includes(term.value));
});

// --- Combined render + keyboard model --------------------------------------
const visibleGroups = computed(() => {
  const groups = [
    ...dataGroups.value,
    { key: 'pages', label: 'Go to page', items: matchingPages.value },
    { key: 'actions', label: 'Quick actions', items: matchingActions.value },
  ].filter((g) => g.items.length);

  // Stamp each item with its index in the flattened list so arrow-key
  // selection can address items across groups.
  let flatIndex = 0;
  return groups.map((group) => ({
    ...group,
    items: group.items.map((item) => ({ ...item, flatIndex: flatIndex++ })),
  }));
});

const flatItems = computed(() => visibleGroups.value.flatMap((g) => g.items));

const selectedIndex = ref(0);

watch(flatItems, () => {
  selectedIndex.value = 0;
});

const showEmptyState = computed(
  () => !searching.value && term.value.length > 0 && flatItems.value.length === 0
);

watch(
  () => props.modelValue,
  (isOpen) => {
    if (!isOpen) {
      query.value = '';
      dataResults.value = null;
      searching.value = false;
      searchFailed.value = false;
      selectedIndex.value = 0;
      if (debounceTimer) clearTimeout(debounceTimer);
      requestSeq += 1;
    }
  },
);

function closePalette() {
  emit('update:modelValue', false);
}

function handleVisibilityChange(value) {
  emit('update:modelValue', value);
}

function moveSelection(delta) {
  const count = flatItems.value.length;
  if (!count) return;
  selectedIndex.value = (selectedIndex.value + delta + count) % count;
  nextTick(() => {
    document
      .querySelector('.command-palette .result-item.selected')
      ?.scrollIntoView({ block: 'nearest' });
  });
}

function handleKeydown(event) {
  if (event.key === 'Escape') {
    closePalette();
    return;
  }
  if (event.key === 'ArrowDown') {
    event.preventDefault();
    moveSelection(1);
    return;
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault();
    moveSelection(-1);
    return;
  }
  if (event.key === 'Enter') {
    const target = flatItems.value[selectedIndex.value] || flatItems.value[0];
    if (target) {
      navigateTo(target.to);
    }
  }
}

async function navigateTo(target) {
  await router.push(target);
  closePalette();
}
</script>

<style scoped>
.palette-header {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--text-primary);
}

.palette-body {
  display: grid;
  gap: var(--space-3);
}

.palette-input {
  width: 100%;
}

.results-wrap {
  max-height: 55vh;
  overflow: auto;
  display: grid;
  gap: var(--space-3);
}

.result-group {
  display: grid;
  gap: var(--space-2);
}

.result-group h4 {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.result-item {
  border: none;
  text-align: left;
  background: var(--surface-elevated);
  color: var(--text-primary);
  border-radius: 0.625rem;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
}

.result-item:hover,
.result-item.selected {
  background: var(--surface-hover);
}

.result-item.selected {
  outline: 1px solid var(--interactive-primary, var(--p-primary-color));
  outline-offset: -1px;
}

.item-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item-sublabel {
  color: var(--text-muted);
  font-size: 0.8125rem;
  white-space: nowrap;
}

.state-line {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.8125rem;
  padding: 0 var(--space-1);
}

.empty-state {
  margin: 0;
  color: var(--text-muted);
  padding: var(--space-3);
  text-align: center;
}
</style>

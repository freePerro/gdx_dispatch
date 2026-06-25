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
        placeholder="Search jobs, customers, invoices..."
      />

      <div class="results-wrap">
        <div
          v-for="group in groupedResults"
          :key="group.type"
          class="result-group"
          :data-type="group.type"
        >
          <h4>{{ group.type }}</h4>
          <button
            v-for="item in group.items"
            :key="item.key"
            type="button"
            class="result-item"
            @click="navigateTo(item.to)"
          >
            <i :class="item.icon" aria-hidden="true" />
            <span>{{ item.label }}</span>
          </button>
        </div>
        <p v-if="groupedResults.length === 0" class="empty-state">No matching commands</p>
      </div>
    </div>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import { QUICK_ACTIONS } from '../constants/modules';
import { useTenantModules } from '../composables/useTenantModules';
import { useAuthStore } from '../stores/auth';
import { isTechnician } from '../constants/roles';

const props = defineProps({
  modelValue: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(['update:modelValue']);

const router = useRouter();
const query = ref('');
const { allEnabledModules, isEnabled } = useTenantModules();
const auth = useAuthStore();

// Nav records come straight from `allEnabledModules`, which useTenantModules
// already permission-filters (a tech never sees a module they lack the
// permission for, so Ctrl-K can't surface a hidden module either).
const baseRecords = computed(() => {
  const role = auth.user?.role || '';
  const dynamicModules = allEnabledModules.value
    .map((module) => ({
      key: module.key,
      label: module.label,
      icon: module.icon,
      type: module.type,
      to: module.to,
    }));

  // AppTopbar already hides the Create-job / Create-customer buttons for tech
  // via `canCreate`. Mirror that here so Ctrl-K doesn't re-open those actions
  // to a tech. (Quick-action gating is separate from module nav visibility.)
  const isTech = isTechnician(role);

  const actions = QUICK_ACTIONS.filter((action) => {
    if (action.key === 'create-job') {
      return isEnabled('jobs') && !isTech;
    }

    if (action.key === 'create-customer') {
      return isEnabled('customers') && !isTech;
    }

    if (action.key === 'open-dispatch') {
      return isEnabled('dispatch') && !isTech;
    }

    return true;
  });

  return [...dynamicModules, ...actions];
});

const filteredResults = computed(() => {
  const term = query.value.trim().toLowerCase();
  if (!term) {
    return baseRecords.value;
  }

  return baseRecords.value.filter((item) => item.label.toLowerCase().includes(term));
});

const groupedResults = computed(() => {
  const grouped = new Map();

  filteredResults.value.forEach((item) => {
    if (!grouped.has(item.type)) {
      grouped.set(item.type, []);
    }
    grouped.get(item.type).push(item);
  });

  return Array.from(grouped.entries()).map(([type, items]) => ({ type, items }));
});

watch(
  () => props.modelValue,
  (isOpen) => {
    if (!isOpen) {
      query.value = '';
    }
  },
);

function closePalette() {
  emit('update:modelValue', false);
}

function handleVisibilityChange(value) {
  emit('update:modelValue', value);
}

function handleKeydown(event) {
  if (event.key === 'Escape') {
    closePalette();
    return;
  }

  if (event.key === 'Enter') {
    const firstResult = filteredResults.value[0];
    if (firstResult) {
      navigateTo(firstResult.to);
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

.result-item:hover {
  background: var(--surface-hover);
}

.empty-state {
  margin: 0;
  color: var(--text-muted);
  padding: var(--space-3);
  text-align: center;
}
</style>

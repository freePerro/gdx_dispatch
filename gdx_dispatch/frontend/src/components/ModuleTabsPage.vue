<template>
  <div class="module-tabs-page">
    <!-- 2026-07-07 tabbed-pages: shared layout for nav clusters (NAV_CLUSTERS).
         Each tab is a real child route — bookmarks, sidebar pins, and
         favorites keep their URLs; this bar is presentation only. Tabs
         mirror sidebar visibility (enablement + permission via
         allEnabledModules), and the bar hides entirely when fewer than two
         tabs survive filtering. -->
    <nav
      v-if="tabs.length > 1"
      class="module-tab-bar"
      :aria-label="`${clusterLabel} sections`"
      data-testid="module-tab-bar"
    >
      <router-link
        v-for="tab in tabs"
        :key="tab.key"
        :to="tab.to"
        class="module-tab"
        :class="{ active: isActiveTab(tab.to) }"
        :aria-current="isActiveTab(tab.to) ? 'page' : undefined"
        :data-testid="`module-tab-${tab.key}`"
      >
        <i :class="tab.icon" aria-hidden="true" />
        <span>{{ tab.label }}</span>
      </router-link>
    </nav>
    <router-view />
  </div>
</template>

<script setup>
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { clusterByKey } from '../constants/modules';
import { useTenantModules } from '../composables/useTenantModules';

const props = defineProps({
  clusterKey: {
    type: String,
    required: true,
  },
});

const route = useRoute();
const { allEnabledModules } = useTenantModules();

const clusterLabel = computed(() => clusterByKey(props.clusterKey)?.label || '');

const tabs = computed(() =>
  allEnabledModules.value
    .filter((m) => m.cluster === props.clusterKey)
    .map((m) => ({
      key: m.key,
      to: m.to,
      icon: m.icon,
      label: m.tabLabel || m.label,
    }))
);

function isActiveTab(targetPath) {
  const path = route?.path ?? '';
  return path === targetPath || path.startsWith(`${targetPath}/`);
}
</script>

<style scoped>
.module-tabs-page {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.module-tab-bar {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1);
  background: var(--surface-panel);
  border: 1px solid var(--border-subtle);
  border-radius: 0.875rem;
  box-shadow: var(--shadow-md);
  /* Narrow viewports: the bar scrolls inside itself instead of widening
     the page (systemic horizontal-overflow audit finding). */
  overflow-x: auto;
}

.module-tab {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: 0.625rem;
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 0.875rem;
  font-weight: 500;
  white-space: nowrap;
  transition: background-color var(--transition-fast), color var(--transition-fast);
}

.module-tab:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}

.module-tab.active {
  background: var(--interactive-primary-soft);
  color: var(--interactive-primary);
  font-weight: 600;
}

.module-tab i {
  font-size: 0.875rem;
}
</style>

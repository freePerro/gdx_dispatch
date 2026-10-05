<template>
  <!-- Captured doors on a job and how many are ordered. The HubX order email
       flips them automatically (backend modules/vendor_orders/hubx.py); the
       office's "Mark Ordered" does the same by hand. Renders nothing when the
       job has no captured door. -->
  <Tag
    v-if="view"
    :value="view.label"
    :severity="view.severity"
    :icon="view.icon"
    class="door-order-tag"
    data-testid="door-order-tag"
  />
</template>

<script setup>
import { computed } from 'vue';
import Tag from 'primevue/tag';

const props = defineProps({
  doors: { type: Object, default: null },
});

const view = computed(() => {
  const total = Number(props.doors?.total || 0);
  const ordered = Number(props.doors?.ordered || 0);
  if (!total) return null;
  if (ordered >= total) {
    return { label: total === 1 ? 'Door ordered' : 'Doors ordered', severity: 'info', icon: 'pi pi-truck' };
  }
  if (ordered === 0) {
    return { label: total === 1 ? 'Door to order' : 'Doors to order', severity: 'warn', icon: 'pi pi-shopping-cart' };
  }
  return { label: `${ordered} of ${total} doors ordered`, severity: 'warn', icon: 'pi pi-shopping-cart' };
});
</script>

<style scoped>
.door-order-tag {
  font-size: 0.75rem;
  white-space: nowrap;
}
</style>

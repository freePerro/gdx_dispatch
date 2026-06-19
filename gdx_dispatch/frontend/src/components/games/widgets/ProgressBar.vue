<template>
  <div class="progress-bar-widget">
    <div class="progress-bar-widget__header">
      <div class="progress-bar-widget__label">{{ config.label || 'Progress' }}</div>
      <div class="progress-bar-widget__value">{{ currentValue }} / {{ maxValue }}</div>
    </div>
    <div class="progress-bar-widget__track">
      <button
        v-for="i in maxValue"
        :key="i"
        type="button"
        class="progress-segment"
        :class="[
          { 'progress-segment--filled': i <= currentValue },
          `progress-segment--${color}`,
        ]"
        :aria-label="`Set ${config.label || 'progress'} to ${i}`"
        :title="`Click to set to ${i}`"
        @click="onClickSegment(i)"
      ></button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue';

const props = defineProps({
  state: { type: Object, required: true },
  config: { type: Object, required: true },
});
const emit = defineEmits(['event']);

const currentValue = computed(() => Number(props.state[props.config.bind] ?? 0));
const maxValue = computed(() => Number(props.state[props.config.max_bind] ?? 5));
const color = computed(() => props.config.color || 'amber');

function onClickSegment(target) {
  // Clicking a segment sets the value to that segment's index.
  // Translate that into an hp_change event with the delta.
  const delta = target - currentValue.value;
  if (delta === 0) return;
  const reason = window.prompt(`Why ${delta > 0 ? 'restore' : 'drain'} ${Math.abs(delta)}? (short reason)`);
  if (reason === null) return;
  emit('event', {
    event_type: 'hp_change',
    value: delta,
    reason: reason || null,
  });
}
</script>

<style scoped>
.progress-bar-widget { padding: 0.75rem 0; }
.progress-bar-widget__header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 0.5rem;
}
.progress-bar-widget__label { font-size: 0.875rem; font-weight: 600; color: #475569; }
.progress-bar-widget__value { font-size: 0.875rem; color: #64748b; font-variant-numeric: tabular-nums; }
.progress-bar-widget__track {
  display: flex;
  gap: 0.25rem;
  width: 100%;
}
.progress-segment {
  flex: 1;
  height: 36px;
  border: 2px solid transparent;
  border-radius: 0.375rem;
  background: #e2e8f0;
  cursor: pointer;
  transition: all 0.15s;
  min-width: 32px;
}
.progress-segment:hover { transform: scaleY(1.1); }
.progress-segment--filled.progress-segment--amber { background: #f59e0b; }
.progress-segment--filled.progress-segment--green { background: #10b981; }
.progress-segment--filled.progress-segment--red { background: #ef4444; }
.progress-segment--filled.progress-segment--blue { background: #3b82f6; }
</style>

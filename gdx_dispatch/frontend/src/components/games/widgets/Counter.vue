<template>
  <div class="counter-widget">
    <div class="counter-widget__header">
      <div class="counter-widget__label">{{ config.label || 'Score' }}</div>
      <div class="counter-widget__value">{{ currentValue }}</div>
    </div>
    <div class="counter-widget__buttons">
      <button
        v-for="(btn, i) in (config.buttons || [])"
        :key="i"
        type="button"
        class="counter-btn"
        @click="onClickButton(btn)"
      >
        {{ btn.label }}
      </button>
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

function onClickButton(btn) {
  const reason = window.prompt(`Why ${btn.label}? (short reason)`);
  if (reason === null) return;
  emit('event', {
    event_type: btn.event,
    value: btn.value,
    reason: reason || null,
  });
}
</script>

<style scoped>
.counter-widget { padding: 0.75rem 0; }
.counter-widget__header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 0.75rem;
}
.counter-widget__label { font-size: 0.875rem; font-weight: 600; color: #475569; }
.counter-widget__value {
  font-size: 1.5rem;
  font-weight: bold;
  color: #0f172a;
  font-variant-numeric: tabular-nums;
}
.counter-widget__buttons {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.counter-btn {
  flex: 1;
  min-width: 64px;
  min-height: 48px;
  padding: 0.5rem 1rem;
  font-size: 1rem;
  font-weight: 600;
  background: #3b82f6;
  color: white;
  border: none;
  border-radius: 0.5rem;
  cursor: pointer;
  transition: background 0.15s;
}
.counter-btn:hover { background: #2563eb; }
.counter-btn:active { transform: translateY(1px); }
</style>

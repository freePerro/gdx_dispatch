<template>
  <div class="heart-bar">
    <div class="heart-bar__label">{{ config.label || 'Lives' }}</div>
    <div class="heart-bar__row">
      <button
        v-for="i in maxValue"
        :key="i"
        type="button"
        class="heart"
        :class="{ 'heart--filled': i <= currentValue }"
        :aria-label="i <= currentValue ? `Take life ${i}` : `Empty slot ${i}`"
        :title="i <= currentValue ? 'Click to take a life' : 'Empty'"
        @click="onClickHeart(i)"
      >
        {{ i <= currentValue ? filledIcon : emptyIcon }}
      </button>
      <button
        type="button"
        class="heart-add"
        title="Grant a life back"
        aria-label="Grant a life"
        @click="onGrantLife"
        :disabled="currentValue >= maxValue"
      >
        +
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
const maxValue = computed(() => Number(props.state[props.config.max_bind] ?? 5));

// Icon switching: 'garage_door' uses door emoji, default uses hearts
const filledIcon = computed(() => (props.config.icon === 'garage_door' ? '🚪' : '❤️'));
const emptyIcon = computed(() => (props.config.icon === 'garage_door' ? '⬜' : '🤍'));

function onClickHeart(index) {
  // Clicking a filled heart takes a life. Clicking an empty heart does nothing.
  if (index > currentValue.value) return;
  const reason = window.prompt('Why take a life? (short reason)');
  if (reason === null) return; // user cancelled
  emit('event', {
    event_type: 'life_lost',
    value: 1,
    reason: reason || null,
  });
}

function onGrantLife() {
  if (currentValue.value >= maxValue.value) return;
  const reason = window.prompt('Why grant a life back? (short reason)');
  if (reason === null) return;
  emit('event', {
    event_type: 'life_gained',
    value: 1,
    reason: reason || null,
  });
}
</script>

<style scoped>
.heart-bar { padding: 0.75rem 0; }
.heart-bar__label { font-size: 0.875rem; font-weight: 600; color: #475569; margin-bottom: 0.5rem; }
.heart-bar__row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
.heart {
  font-size: 2rem;
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0.25rem 0.5rem;
  border-radius: 0.5rem;
  transition: transform 0.1s;
  min-width: 48px;
  min-height: 48px;
}
.heart:hover { transform: scale(1.15); background: rgba(0, 0, 0, 0.05); }
.heart--filled { filter: none; }
.heart:not(.heart--filled) { opacity: 0.4; cursor: default; }
.heart-add {
  font-size: 1.5rem;
  font-weight: bold;
  background: #10b981;
  color: white;
  border: none;
  border-radius: 0.5rem;
  width: 48px;
  height: 48px;
  cursor: pointer;
  margin-left: 0.5rem;
}
.heart-add:disabled { background: #cbd5e1; cursor: not-allowed; }
.heart-add:not(:disabled):hover { background: #059669; }
</style>

<template>
  <div class="event-feed">
    <div class="event-feed__label">{{ config.label || 'Recent activity' }}</div>
    <ul v-if="events.length" class="event-feed__list">
      <li v-for="event in events" :key="event.id" class="event-feed__item">
        <div class="event-feed__top">
          <span class="event-feed__type" :class="`event-feed__type--${event.event_type}`">
            {{ formatEventType(event.event_type) }}
          </span>
          <span v-if="event.value !== null && event.value !== undefined" class="event-feed__value">
            {{ event.value > 0 ? `+${event.value}` : event.value }}
          </span>
          <span class="event-feed__time">{{ formatTime(event.created_at) }}</span>
        </div>
        <div v-if="event.reason" class="event-feed__reason">"{{ event.reason }}"</div>
      </li>
    </ul>
    <div v-else class="event-feed__empty">No events yet — the game just started.</div>
  </div>
</template>

<script setup>
import { computed } from 'vue';

const props = defineProps({
  state: { type: Object, required: true },
  config: { type: Object, required: true },
});

const events = computed(() => {
  const all = props.state.recent_events || [];
  const limit = props.config.limit || 10;
  return all.slice(0, limit);
});

function formatEventType(type) {
  return (type || '').replace(/_/g, ' ');
}

function formatTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '';
  }
}
</script>

<style scoped>
.event-feed { padding: 0.75rem 0; }
.event-feed__label {
  font-size: 0.875rem;
  font-weight: 600;
  color: #475569;
  margin-bottom: 0.5rem;
}
.event-feed__list {
  list-style: none;
  padding: 0;
  margin: 0;
  border: 1px solid #e2e8f0;
  border-radius: 0.5rem;
  background: #f8fafc;
  max-height: 300px;
  overflow-y: auto;
}
.event-feed__item {
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid #e2e8f0;
}
.event-feed__item:last-child { border-bottom: none; }
.event-feed__top {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  font-size: 0.875rem;
}
.event-feed__type {
  font-weight: 600;
  text-transform: capitalize;
}
.event-feed__type--life_lost { color: #dc2626; }
.event-feed__type--life_gained { color: #10b981; }
.event-feed__type--hp_change { color: #f59e0b; }
.event-feed__type--xp_gained { color: #3b82f6; }
.event-feed__type--death { color: #7f1d1d; font-weight: bold; }
.event-feed__value {
  font-variant-numeric: tabular-nums;
  color: #475569;
}
.event-feed__time {
  margin-left: auto;
  color: #94a3b8;
  font-size: 0.75rem;
}
.event-feed__reason {
  margin-top: 0.25rem;
  font-size: 0.8125rem;
  color: #64748b;
  font-style: italic;
}
.event-feed__empty {
  padding: 1rem;
  text-align: center;
  color: #94a3b8;
  font-style: italic;
  font-size: 0.875rem;
}
</style>

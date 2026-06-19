<template>
  <div class="big-button-widget">
    <button
      type="button"
      class="big-button"
      :class="`big-button--${config.style || 'default'}`"
      @click="onClick"
    >
      {{ config.label || 'Action' }}
    </button>
  </div>
</template>

<script setup>
const props = defineProps({
  state: { type: Object, required: true },
  config: { type: Object, required: true },
});
const emit = defineEmits(['event']);

function onClick() {
  // If the config has a confirm prompt, require the user to type the trigger word.
  if (props.config.confirm) {
    const trigger = (props.config.label || 'confirm').toLowerCase();
    const typed = window.prompt(props.config.confirm);
    if (typed === null) return;
    if (typed.toLowerCase().trim() !== trigger) {
      alert(`You must type "${trigger}" exactly to confirm.`);
      return;
    }
  }
  const reason = window.prompt('Why? (short reason — optional)');
  if (reason === null) return;
  emit('event', {
    event_type: props.config.event,
    value: props.config.value || 1,
    reason: reason || null,
  });
}
</script>

<style scoped>
.big-button-widget { padding: 1rem 0 0 0; }
.big-button {
  width: 100%;
  padding: 0.875rem 1.5rem;
  font-size: 1rem;
  font-weight: 600;
  border: none;
  border-radius: 0.5rem;
  cursor: pointer;
  min-height: 48px;
  transition: all 0.15s;
}
.big-button--default {
  background: #3b82f6;
  color: white;
}
.big-button--default:hover { background: #2563eb; }
.big-button--danger {
  background: #fee2e2;
  color: #991b1b;
  border: 1px solid #fca5a5;
}
.big-button--danger:hover {
  background: #fecaca;
  border-color: #f87171;
}
.big-button:active { transform: translateY(1px); }
</style>

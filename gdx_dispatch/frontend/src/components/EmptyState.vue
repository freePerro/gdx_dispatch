<template>
  <div class="empty-state">
    <i :class="icon" class="empty-icon" />
    <h4 class="empty-title">{{ title }}</h4>
    <p class="empty-message">{{ message }}</p>
    <Button v-if="actionLabel" :label="actionLabel" size="small" @click="onAction" />
  </div>
</template>

<script setup>
import Button from "primevue/button";
import { useRouter } from "vue-router";

const props = defineProps({
  icon: { type: String, default: "pi pi-inbox" },
  title: { type: String, default: "No data yet" },
  message: { type: String, default: "Records will appear here once created." },
  actionLabel: { type: String, default: "" },
  actionTo: { type: String, default: "" },
});

const emit = defineEmits(["action"]);
const router = useRouter();

function onAction() {
  emit("action");
  if (props.actionTo) router.push(props.actionTo);
}
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 3rem 1rem;
  text-align: center;
  opacity: 0.7;
}
.empty-icon {
  font-size: 3rem;
  margin-bottom: 1rem;
  color: var(--p-text-muted-color);
}
.empty-title {
  margin: 0 0 0.5rem;
  font-weight: 600;
}
.empty-message {
  margin: 0 0 1rem;
  color: var(--p-text-muted-color);
  max-width: 400px;
}
</style>

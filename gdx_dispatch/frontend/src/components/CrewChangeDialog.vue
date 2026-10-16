<script setup>
// A crew job given a tech outside its crew: swap one member or add the tech.
// Nothing changes the crew without this answer. State comes from
// useCrewChangePrompt (composables/useCrewChange.js).
import Dialog from 'primevue/dialog';
import Button from 'primevue/button';

defineProps({
  prompt: { type: Object, required: true },
});
const emit = defineEmits(['answer']);
</script>

<template>
  <Dialog
    :visible="prompt.visible"
    :header="prompt.header"
    :modal="true"
    :closable="true"
    :style="{ width: '95vw', maxWidth: '440px' }"
    data-testid="crew-change-prompt"
    @update:visible="(v) => { if (!v) emit('answer', null) }"
  >
    <p class="crew-change-intro">
      <strong>{{ prompt.jobLabel }}</strong> has a crew: {{ prompt.crewLabel }}.
    </p>
    <div class="crew-change-options">
      <Button
        v-for="opt in prompt.options"
        :key="opt.key"
        :label="opt.label"
        :icon="opt.key === 'add' ? 'pi pi-user-plus' : 'pi pi-arrow-right-arrow-left'"
        severity="secondary"
        outlined
        :data-testid="`crew-change-${opt.key}`"
        @click="emit('answer', opt)"
      />
    </div>
    <template #footer>
      <Button label="Cancel" severity="secondary" text data-testid="crew-change-cancel" @click="emit('answer', null)" />
    </template>
  </Dialog>
</template>

<style scoped>
.crew-change-intro {
  margin-top: 0;
}
.crew-change-options {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.crew-change-options :deep(.p-button) {
  justify-content: flex-start;
}
</style>

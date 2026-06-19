<template>
  <Button
    type="button"
    class="p-button-text help-btn"
    aria-label="Open help"
    data-tour="help-button"
    data-test="topbar-help"
    @click="openHelp"
  >
    <i class="pi pi-question-circle" aria-hidden="true" />
  </Button>
</template>

<script setup>
import Button from 'primevue/button';
import { useHelp } from '../composables/useHelp';

const help = useHelp();

function openHelp() {
  help.open();
}
</script>

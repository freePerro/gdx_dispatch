<template>
  <div class="p-8 text-center">
    <div class="text-6xl font-bold text-surface-400 mb-4">404</div>
    <h1 class="text-2xl font-semibold mb-2">Page not found</h1>
    <p class="text-surface-500 mb-6">
      The URL <code class="px-1.5 py-0.5 rounded bg-surface-100 dark:bg-surface-800">{{ $route.fullPath }}</code> doesn't match any route.
    </p>
    <Button label="Go to Dashboard" icon="pi pi-home" @click="$router.replace('/dashboard')" />
  </div>
</template>

<script setup>
import Button from 'primevue/button';
</script>

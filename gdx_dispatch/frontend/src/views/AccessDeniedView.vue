<template>
  <div class="p-8 text-center" data-testid="access-denied">
    <div class="text-6xl font-bold text-surface-400 mb-4">
      <i class="pi pi-lock" style="font-size: 3.5rem" />
    </div>
    <h1 class="text-2xl font-semibold mb-2">You don't have access to this page</h1>
    <p class="text-surface-500 mb-2">
      <template v-if="$route.query.path">
        <code class="px-1.5 py-0.5 rounded bg-surface-100 dark:bg-surface-800">{{ $route.query.path }}</code>
        requires a permission your role doesn't have<template v-if="$route.query.permission">
        (<code class="px-1.5 py-0.5 rounded bg-surface-100 dark:bg-surface-800">{{ $route.query.permission }}</code>)</template>.
      </template>
      <template v-else>
        This page requires a permission your role doesn't have.
      </template>
    </p>
    <p class="text-surface-500 mb-6">
      Ask an admin to grant it under Role Permissions if you need it for your work.
    </p>
    <Button label="Go to Dashboard" icon="pi pi-home" @click="$router.replace('/dashboard')" />
  </div>
</template>

<script setup>
import Button from 'primevue/button';
</script>

<template>
  <slot />
</template>

<script setup>
import { onMounted, watch } from 'vue';
import { storeToRefs } from 'pinia';
import { useThemeStore } from '../stores/theme';

const theme = useThemeStore();
const { branding } = storeToRefs(theme);

onMounted(async () => {
  theme.applyThemeVars();
  // Re-hydrate branding on every app mount (including hard refresh).
  // LoginView also calls this, but a refresh of an already-authed session
  // skips LoginView entirely and would otherwise leave the store at the
  // 'GDX Platform' default.
  const token = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('gdx_access_token') : null;
  if (token) {
    await theme.loadBranding();
  }
});

watch(
  branding,
  () => {
    theme.applyThemeVars();
  },
  { deep: true },
);
</script>

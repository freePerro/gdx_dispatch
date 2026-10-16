<script setup>
// The quick-PIN / fingerprint gate of the Android app (GDXA-1187). The router
// holds every non-public route here while lib/nativeAuth.js says the session
// is locked; this page only mounts the unlock dialog and moves on once it
// clears the lock. "Forgot PIN" turns the PIN off and signs out to the
// password login: a forgotten PIN left on would lock the next session again.
import { useRoute, useRouter } from 'vue-router';
import MobilePinUnlockDialog from '../components/MobilePinUnlockDialog.vue';
import { useAuthStore } from '../stores/auth';
import { clearQuickPin } from '../lib/nativeAuth';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

function onUnlocked() {
  // The guard sends an unlocked /unlock on to the (in-app) redirect target.
  router.replace({ path: route.path, query: route.query, force: true }).catch(() => {});
}

function onForgotPassword() {
  clearQuickPin();
  auth.logout({ chosen: true });
  router.replace('/login').catch(() => {});
}
</script>

<template>
  <div data-testid="session-unlock">
    <MobilePinUnlockDialog
      :visible="true"
      @unlocked="onUnlocked"
      @forgot-password="onForgotPassword"
    />
  </div>
</template>

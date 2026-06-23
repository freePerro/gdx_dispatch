/**
 * useIdleLogout — log the user out after N minutes of no activity.
 *
 * The timeout is configured in Settings → Feature Settings and stored in
 * localStorage (per device). 0 = disabled. Mount once near the app root.
 *
 * ponytail: per-device (localStorage), no backend. If you need it enforced
 * tenant-wide by an admin, persist the value on TenantSettings and seed
 * getIdleTimeoutMin() from the API after login — the composable stays the same.
 */
import { onMounted, onUnmounted, watch } from 'vue';
import { useRouter } from 'vue-router';
import { useToast } from 'primevue/usetoast';
import { useAuthStore } from '../stores/auth';

const STORAGE_KEY = 'gdx_idle_timeout_min';
const CONFIG_EVENT = 'gdx:idle-config-changed';
const ACTIVITY_EVENTS = ['mousedown', 'keydown', 'scroll', 'touchstart', 'click', 'mousemove'];

export function getIdleTimeoutMin() {
  const v = Number(localStorage.getItem(STORAGE_KEY) || 0);
  return Number.isFinite(v) && v > 0 ? v : 0;
}

export function setIdleTimeoutMin(min) {
  const n = Math.max(0, Math.floor(Number(min) || 0));
  localStorage.setItem(STORAGE_KEY, String(n));
  window.dispatchEvent(new CustomEvent(CONFIG_EVENT));
}

export function useIdleLogout() {
  const auth = useAuthStore();
  const router = useRouter();
  const toast = useToast();
  let timer = null;
  let lastReset = 0;

  function clear() {
    if (timer) { clearTimeout(timer); timer = null; }
  }

  function onTimeout() {
    clear();
    if (!auth.isAuthenticated) return;
    auth.logout();
    toast.add({
      severity: 'info',
      summary: 'Signed out',
      detail: 'You were logged out due to inactivity.',
      life: 6000,
    });
    router.push('/login');
  }

  function reset() {
    // Throttle: mousemove fires constantly; one reset/sec is plenty.
    const now = Date.now();
    if (now - lastReset < 1000) return;
    lastReset = now;
    clear();
    const min = getIdleTimeoutMin();
    if (min > 0 && auth.isAuthenticated) {
      timer = setTimeout(onTimeout, min * 60 * 1000);
    }
  }

  function armFresh() {
    // Re-read config and (re)arm immediately, ignoring the throttle.
    lastReset = 0;
    reset();
  }

  onMounted(() => {
    ACTIVITY_EVENTS.forEach((e) => window.addEventListener(e, reset, { passive: true }));
    window.addEventListener(CONFIG_EVENT, armFresh);
    armFresh();
  });

  // Arm immediately on login (not just on the first mouse move afterward) and
  // cancel the timer on logout. Without this, a user who logs in and walks away
  // is never armed.
  watch(() => auth.isAuthenticated, () => armFresh());

  onUnmounted(() => {
    clear();
    ACTIVITY_EVENTS.forEach((e) => window.removeEventListener(e, reset));
    window.removeEventListener(CONFIG_EVENT, armFresh);
  });
}

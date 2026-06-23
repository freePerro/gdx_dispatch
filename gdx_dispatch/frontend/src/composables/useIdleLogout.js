/**
 * useIdleLogout — log the user out after N minutes of no activity.
 *
 * Tenant-wide: the timeout lives on TenantSettings and is fetched from
 * /api/session-policy on login, then cached in localStorage (which drives the
 * timer + survives reloads/offline). Admins set it in Settings → Feature
 * Settings → Security. 0 = disabled. Mount once near the app root.
 */
import { onMounted, onUnmounted, watch } from 'vue';
import { useRouter } from 'vue-router';
import { useToast } from 'primevue/usetoast';
import { useAuthStore } from '../stores/auth';
import { createApiClient } from './useApi';

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
  const apiClient = createApiClient();
  let timer = null;
  let lastReset = 0;

  // Pull the tenant-wide value from the server and cache it locally (which
  // re-arms via the config event). Best-effort: on failure keep the cached
  // localStorage value so offline / transient errors don't disable the timer.
  async function syncServerPolicy() {
    if (!auth.isAuthenticated) return;
    try {
      const data = await apiClient.get('/api/session-policy');
      if (data && typeof data.idle_timeout_minutes === 'number') {
        setIdleTimeoutMin(data.idle_timeout_minutes);
      }
    } catch {
      /* keep cached value */
    }
  }

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
    syncServerPolicy();
  });

  // On login: fetch the tenant policy and arm immediately (not just on the first
  // mouse move). On logout: armFresh re-reads and the isAuthenticated gate stops
  // the timer.
  watch(() => auth.isAuthenticated, (isAuth) => {
    armFresh();
    if (isAuth) syncServerPolicy();
  });

  onUnmounted(() => {
    clear();
    ACTIVITY_EVENTS.forEach((e) => window.removeEventListener(e, reset));
    window.removeEventListener(CONFIG_EVENT, armFresh);
  });
}

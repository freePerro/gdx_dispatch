// Sprint 5 / S5-C1 — GPS breadcrumb composable.
//
// Samples the device location on a timer while the tech is clocked in
// and posts each sample to /api/mobile/location. The server enforces
// the privacy boundary (refuses with 403 if the user is not clocked
// in); when the client sees that 403 it stops sampling until restart()
// is called explicitly.

import { ref, onBeforeUnmount } from "vue";
import { useApi } from "./useApi";

const DEFAULT_INTERVAL_MS = 30_000;
const MIN_INTERVAL_MS = 10_000;

export function useGpsBreadcrumb(options = {}) {
  const api = useApi();
  const intervalMs = ref(Math.max(MIN_INTERVAL_MS, options.intervalMs || DEFAULT_INTERVAL_MS));
  const enabled = ref(false);
  const lastError = ref(null);
  const lastSentAt = ref(null);
  const lastLocation = ref(null);
  let timer = null;

  function getCurrentPosition() {
    return new Promise((resolve, reject) => {
      if (!("geolocation" in navigator)) {
        reject(new Error("geolocation_unsupported"));
        return;
      }
      navigator.geolocation.getCurrentPosition(resolve, reject, {
        enableHighAccuracy: true,
        timeout: 15_000,
        maximumAge: 5_000,
      });
    });
  }

  async function sampleOnce() {
    try {
      const pos = await getCurrentPosition();
      const payload = {
        lat: pos.coords.latitude,
        lng: pos.coords.longitude,
        accuracy_m: pos.coords.accuracy ?? null,
        speed_mps: pos.coords.speed ?? null,
        heading_deg: pos.coords.heading ?? null,
        recorded_at: new Date(pos.timestamp || Date.now()).toISOString(),
        job_id: options.getJobId ? options.getJobId() : null,
      };
      // suppressErrorToast: the server returns 403 "Not clocked in" any time
      // a tech is off-shift, and a tenant-disabled GPS returns 403 too. Both
      // are expected, handled silently below — the global toast was noise on
      // every off-shift login (DT-2 prod-walk surfaced this).
      const created = await api.post("/api/mobile/location", payload, { suppressErrorToast: true });
      lastLocation.value = created;
      lastSentAt.value = new Date().toISOString();
      lastError.value = null;
      return created;
    } catch (err) {
      lastError.value = err?.message || String(err);
      // 403 = "Not clocked in" or "GPS disabled for tenant" — stop sampling.
      if (err?.status === 403) {
        stop();
      }
      return null;
    }
  }

  function start() {
    if (timer) return;
    enabled.value = true;
    sampleOnce();
    timer = setInterval(sampleOnce, intervalMs.value);
  }

  function stop() {
    enabled.value = false;
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function setInterval_(ms) {
    intervalMs.value = Math.max(MIN_INTERVAL_MS, ms);
    if (timer) {
      stop();
      start();
    }
  }

  onBeforeUnmount(() => stop());

  return {
    enabled,
    lastError,
    lastSentAt,
    lastLocation,
    intervalMs,
    start,
    stop,
    sampleOnce,
    setInterval: setInterval_,
  };
}

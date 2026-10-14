<template>
  <div v-if="visible" class="office-alerts" data-testid="office-alerts">
    <div class="oa-row">
      <label class="oa-label" for="office-alerts-switch">
        <i class="pi pi-bell" aria-hidden="true" />
        <span>Alerts on this phone</span>
      </label>
      <ToggleSwitch
        v-model="on"
        input-id="office-alerts-switch"
        :disabled="busy"
        data-testid="office-alerts-switch"
        @update:model-value="onToggle"
      />
    </div>
    <p v-if="error" class="oa-note oa-error" data-testid="office-alerts-error">{{ error }}</p>
    <p v-else-if="on && !status.batteryUnrestricted" class="oa-note" data-testid="office-alerts-battery">
      Alerts can be held back while the phone sleeps.
      <button type="button" class="oa-link" data-testid="office-alerts-battery-btn" @click="allowBattery">
        Allow GDX to run in the background
      </button>
    </p>
    <p v-else-if="!compact" class="oa-note">
      {{ on
        ? 'New texts, payments and leads show up on this phone even when the app is closed.'
        : 'Get new texts, payments and leads on this phone even when the app is closed.' }}
    </p>
  </div>
</template>

<script setup>
// Office alerts toggle. Office
// users only (nav.office) and only in the Android app; renders nothing
// anywhere else.
import { computed, onMounted, onUnmounted, ref } from 'vue';
import ToggleSwitch from 'primevue/toggleswitch';
import { useApi } from '../composables/useApi';
import { usePermission } from '../composables/usePermission';
import {
  disableOfficeAlerts,
  enableOfficeAlerts,
  getOfficeAlertsStatus,
  isOfficeAlertsAvailable,
  openBatterySettings,
} from '../lib/officeAlerts';

defineProps({
  // Hides the explanatory line; for tight headers.
  compact: { type: Boolean, default: false },
});

const api = useApi();
const { hasPermission } = usePermission();
const visible = computed(() => isOfficeAlertsAvailable() && hasPermission('nav.office'));

const status = ref({ enabled: false, batteryUnrestricted: false });
const on = ref(false);
const busy = ref(false);
const error = ref('');

function apply(next) {
  status.value = next;
  on.value = !!next.enabled;
}

async function refresh() {
  if (!visible.value) return;
  apply(await getOfficeAlertsStatus());
}

async function onToggle(want) {
  busy.value = true;
  error.value = '';
  try {
    if (want) {
      const next = await enableOfficeAlerts(api);
      apply(next);
      // The system dialog explains itself; ask right away while the user is
      // paying attention to this setting.
      if (next.enabled && !next.batteryUnrestricted) apply(await openBatterySettings());
    } else {
      apply(await disableOfficeAlerts('disabled'));
    }
  } catch (err) {
    error.value = err?.message || 'Could not change phone alerts.';
    await refresh();
  } finally {
    busy.value = false;
  }
}

async function allowBattery() {
  try {
    apply(await openBatterySettings());
  } catch (err) {
    error.value = err?.message || 'Could not open battery settings.';
  }
}

// The battery dialog returns to the app without an event; re-read on focus.
function onVisible() {
  if (document.visibilityState === 'visible') refresh();
}

onMounted(() => {
  refresh();
  document.addEventListener('visibilitychange', onVisible);
});
onUnmounted(() => document.removeEventListener('visibilitychange', onVisible));
</script>

<style scoped>
.office-alerts {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  padding: 0.5rem 0;
}
.oa-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}
.oa-label {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-weight: 600;
}
.oa-note {
  margin: 0;
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #64748b);
}
.oa-error {
  color: var(--p-red-500, #dc2626);
}
.oa-link {
  background: none;
  border: 0;
  padding: 0;
  color: var(--p-primary-color);
  text-decoration: underline;
  font: inherit;
  cursor: pointer;
}
</style>

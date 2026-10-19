<!--
  MyVacationLine — "Vacation <window>: Xh used of Yh · Zh left" on the tech's
  own clock pages (GDXA-1902). Built to be the one component under the
  desktop Time off card and the mobile Clock tab, as TimeOffRequestPanel is;
  only the desktop card uses it so far. MobileTimeclockView still renders its
  own copy with the start year until the mobile-tech follow-up adopts this
  (its spec's `data-test="mt-vacation-*"` selectors become the
  `data-testid="my-vacation-*"` ones below).

  Reads GET /api/timeclock/time-off/balances with no technician named, so the
  server answers for the caller (core/time_off.vacation_balances): every booked
  day counts, approved days still ahead included, so the "still ahead" hours
  are part of "used", never added to it. The label is the person's vacation
  year from window_start / window_end (utils/vacationWindow), not `year`,
  which under hire_anniversary is only the year the window starts in.

  A 403 is the role holding no vacation of its own (viewer), not a failed
  read: the line hides rather than telling them to try again.

  Contract (stable, for the mobile adoption): no props, no events.
  Fetches on mount; the parent calls the exposed `refresh()` after a time-off
  change (TimeOffRequestPanel's `changed`). A class set on it lands on the
  rendered line.
-->
<template>
  <p v-if="balance" class="vacation-line" data-testid="my-vacation-balance">
    <i class="pi pi-sun" aria-hidden="true" />
    <strong data-testid="my-vacation-window">Vacation {{ windowLabel }}:</strong>
    {{ hoursText(balance.used_hours) }}h used<template v-if="balance.scheduled_hours > 0">
      ({{ hoursText(balance.scheduled_hours) }}h of it still ahead)</template>
    <template v-if="balance.allowance_hours == null">
      · no allowance set — ask the office
    </template>
    <template v-else>
      of {{ hoursText(balance.allowance_hours) }}h ·
      <Tag
        :value="`${hoursText(balance.remaining_hours)}h left`"
        :severity="balance.overdrawn ? 'danger' : 'success'"
        data-testid="my-vacation-remaining"
      />
    </template>
  </p>
  <p v-else-if="failed" class="vacation-line vacation-muted" data-testid="my-vacation-error">
    Your vacation balance could not be read right now.
  </p>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import Tag from 'primevue/tag';
import { useApi } from '../composables/useApi';
import { vacationWindowLabel } from '../utils/vacationWindow';

const api = useApi();

const balance = ref(null);
const failed = ref(false);

const windowLabel = computed(() => vacationWindowLabel(balance.value));

async function refresh() {
  try {
    const data = await api.get('/api/timeclock/time-off/balances', { suppressErrorToast: true });
    balance.value = data?.balances?.[0] || null;
    failed.value = !balance.value;
  } catch (err) {
    balance.value = null;
    failed.value = err?.status !== 403;
  }
}

function hoursText(h) {
  return Number(h || 0).toFixed(2).replace(/\.00$/, '');
}

defineExpose({ refresh });

onMounted(refresh);
</script>

<style scoped>
.vacation-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  margin: 0 0 0.75rem;
}
.vacation-muted {
  color: var(--p-text-muted-color);
}
</style>

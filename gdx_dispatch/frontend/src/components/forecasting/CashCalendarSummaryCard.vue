<template>
  <!-- Renders nothing for a role without accounting.read: the endpoint would
       403, and a card that can only show an error is worse than no card. -->
  <Card v-if="calendar" class="cash-calendar-card" data-testid="cash-calendar-card">
    <template #title>
      <div class="ccc-title">
        <span><i class="pi pi-calendar" aria-hidden="true" /> Cash · next {{ calendar.days }} days</span>
        <router-link to="/forecasting" class="ccc-link" data-testid="cash-calendar-card-link">
          View calendar <i class="pi pi-angle-right" aria-hidden="true" />
        </router-link>
      </div>
    </template>
    <template #content>
      <div class="ccc-grid">
        <div>
          <div class="ccc-label">In the bank now</div>
          <div class="ccc-value">{{ money(calendar.starting_balance) }}</div>
        </div>
        <div :class="{ 'ccc-bad': lowBad }">
          <div class="ccc-label">Lowest point</div>
          <div class="ccc-value">{{ money(s.lowest_balance) }}</div>
          <div class="ccc-sub">{{ dayLabel(s.lowest_date) }}</div>
        </div>
        <div :class="{ 'ccc-bad': !!s.first_below_floor }" data-testid="cash-calendar-card-floor">
          <div class="ccc-label">
            {{ calendar.floor === null ? 'Cash floor' : `Under ${money(calendar.floor)}` }}
          </div>
          <div class="ccc-value">
            <template v-if="calendar.floor === null">Not set</template>
            <template v-else-if="s.first_below_floor">{{ dayLabel(s.first_below_floor) }}</template>
            <template v-else>{{ billsOutside > 0 ? 'None among dated items' : 'No tight days' }}</template>
          </div>
          <div class="ccc-sub" v-if="calendar.floor !== null && s.first_below_floor_if_nothing_comes_in && !s.first_below_floor">
            {{ dayLabel(s.first_below_floor_if_nothing_comes_in) }} if nothing comes in
          </div>
        </div>
      </div>
      <p v-if="outside.length" class="ccc-outside" data-testid="cash-calendar-card-outside">
        Not in these numbers: {{ outside.join(' · ') }}
      </p>
    </template>
  </Card>
</template>

<script setup>
import { computed, onMounted } from 'vue';
import Card from 'primevue/card';
import { CASH_CALENDAR_DEFAULT_DAYS, useCashCalendar } from '../../composables/useCashCalendar';
import { formatDate, formatMoney as money } from '../../composables/useFormatters';
import { useAuthStore } from '../../stores/auth';

const auth = useAuthStore();
const cc = useCashCalendar();
const calendar = cc.calendar;
const s = computed(() => calendar.value?.summary || {});
const lowBad = computed(() => {
  const floor = calendar.value?.floor;
  return floor !== null && floor !== undefined && s.value.lowest_balance < floor;
});

const billsOutside = computed(() => calendar.value?.unscheduled?.vendor_bills_past_due_or_undated?.total || 0);
const outside = computed(() => {
  const u = calendar.value?.unscheduled || {};
  const parts = [];
  if (u.vendor_bills_past_due_or_undated?.total > 0) parts.push(`${money(u.vendor_bills_past_due_or_undated.total)} in bills with no future due date`);
  if (u.customer_invoices_past_due?.total > 0) parts.push(`${money(u.customer_invoices_past_due.total)} customers owe past due`);
  const fin = u.finished_jobs_not_billed;
  if (fin?.count > 0) {
    const unpriced = fin.unpriced || 0;
    if (fin.total > 0) {
      parts.push(`${money(fin.total)} in finished jobs not billed${unpriced ? ` (+${unpriced} not priced)` : ''}`);
    } else {
      parts.push(`${fin.count} finished ${fin.count === 1 ? 'job' : 'jobs'} not billed, not priced`);
    }
  }
  return parts;
});

function dayLabel(iso) {
  return iso ? formatDate(iso, { options: { weekday: 'short', month: 'short', day: 'numeric' } }) : '—';
}

onMounted(async () => {
  await auth.loadPermissions();
  if (!auth.hasPermission('accounting.read')) return;
  cc.days.value = CASH_CALENDAR_DEFAULT_DAYS;
  await cc.load({ quiet: true });
});
</script>

<style scoped>
.cash-calendar-card { margin-bottom: 1rem; border-left: 4px solid var(--p-primary-color); }
.ccc-title { display: flex; justify-content: space-between; align-items: baseline; gap: 0.75rem; flex-wrap: wrap; }
.ccc-link { font-size: 0.85rem; font-weight: 500; }
.ccc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: 0.75rem; }
.ccc-label { color: var(--p-text-muted-color); font-size: 0.8rem; }
.ccc-value { font-size: 1.25rem; font-weight: 600; }
.ccc-sub { color: var(--p-text-muted-color); font-size: 0.75rem; }
.ccc-bad .ccc-value { color: var(--p-red-600, #dc2626); }
.ccc-outside { margin: 0.75rem 0 0; font-size: 0.85rem; color: var(--color-warning-500); font-weight: 600; }
</style>

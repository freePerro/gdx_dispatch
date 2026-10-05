<template>
  <section id="cash-calendar" class="cash-calendar panel" data-testid="cash-calendar">
    <header class="cc-head">
      <h2 class="cc-heading">Cash calendar</h2>
      <div class="cc-controls">
        <SelectButton
          :modelValue="days"
          :options="dayOptions"
          optionLabel="label"
          optionValue="value"
          :allowEmpty="false"
          aria-label="How many days to show"
          data-testid="cc-days"
          @update:modelValue="setDays"
        />
        <Button
          v-if="canEdit"
          label="Floor & accounts"
          icon="pi pi-sliders-h"
          severity="secondary"
          size="small"
          data-testid="cc-settings"
          @click="openSettings"
        />
      </div>
    </header>

    <div v-if="loading && !calendar" class="cc-loading"><ProgressSpinner style="width: 2rem; height: 2rem" /></div>
    <Message v-else-if="error" severity="error" :closable="false">{{ error }}</Message>

    <template v-else-if="calendar">
      <!-- Setup nudges: said once, plainly, with the way to fix them. -->
      <div v-if="calendar.floor === null || !calendar.accounts.chosen || !calendar.accounts.included.length" class="cc-nudges">
        <Message v-if="calendar.floor === null" severity="warn" :closable="false" data-testid="cc-no-floor">
          No cash floor is set, so no day is flagged as tight.
          <a v-if="canEdit" href="#" @click.prevent="openSettings">Set a floor</a>
          <span v-else>An owner or admin can set one.</span>
        </Message>
        <Message
          v-if="calendar.accounts.chosen && !calendar.accounts.included.length"
          severity="warn"
          :closable="false"
          data-testid="cc-chosen-no-balance"
        >
          None of the chosen operating accounts has a synced balance yet, so the calendar starts from $0.
          <router-link to="/bank-feeds">Check Bank Feeds</router-link>
        </Message>
        <Message v-if="!calendar.accounts.chosen" severity="info" :closable="false" data-testid="cc-default-accounts">
          <template v-if="calendar.accounts.included.length">Starting from {{ includedNames }} by default.</template>
          <template v-else>No bank account has a synced balance yet, so the calendar starts from $0.</template>
          <a v-if="canEdit" href="#" @click.prevent="openSettings">Choose your operating accounts</a>
        </Message>
      </div>

      <div class="cc-summary">
        <div class="cc-tile" data-testid="cc-start">
          <div class="cc-tile-label">In the bank now</div>
          <div class="cc-tile-value">{{ money(calendar.starting_balance) }}</div>
          <div class="cc-tile-sub">
            {{ calendar.accounts.included.length }}
            {{ calendar.accounts.included.length === 1 ? 'account' : 'accounts' }}
            <template v-if="calendar.balance_as_of"> · synced {{ formatDate(calendar.balance_as_of) }}</template>
          </div>
        </div>
        <div class="cc-tile" :class="{ 'cc-bad': belowFloor(summary.lowest_balance) }" data-testid="cc-lowest">
          <div class="cc-tile-label">Lowest point</div>
          <div class="cc-tile-value">{{ money(summary.lowest_balance) }}</div>
          <div class="cc-tile-sub">on {{ dayLabel(summary.lowest_date) }}</div>
        </div>
        <div class="cc-tile" :class="{ 'cc-bad': summary.first_below_floor }" data-testid="cc-first-below">
          <div class="cc-tile-label">
            {{ calendar.floor === null ? 'Below a floor' : `First day under ${money(calendar.floor)}` }}
          </div>
          <div class="cc-tile-value">
            <template v-if="calendar.floor === null">No floor set</template>
            <template v-else-if="summary.first_below_floor">{{ dayLabel(summary.first_below_floor) }}</template>
            <template v-else>{{ bills.total > 0 ? 'None among dated items' : 'None' }}</template>
          </div>
          <div class="cc-tile-sub cc-warn-text" v-if="bills.total > 0" data-testid="cc-bills-outside">
            Plus {{ money(bills.total) }} in bills with no future due date, not counted
          </div>
          <div class="cc-tile-sub" v-if="calendar.floor !== null">
            If nothing comes in:
            {{ summary.first_below_floor_if_nothing_comes_in ? dayLabel(summary.first_below_floor_if_nothing_comes_in) : 'none' }}
          </div>
        </div>
        <div class="cc-tile" data-testid="cc-totals">
          <div class="cc-tile-label">Next {{ calendar.days }} days</div>
          <div class="cc-tile-value cc-inout">
            <span class="cc-in">+{{ money(summary.total_in) }}</span>
            <span class="cc-out">−{{ money(summary.total_out) }}</span>
          </div>
          <div class="cc-tile-sub">Ends at {{ money(summary.ending_balance) }}</div>
        </div>
      </div>

      <div class="cc-table-wrap">
        <table class="cc-table" data-testid="cc-rows">
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col">What</th>
              <th scope="col" class="num cc-inout-col">In</th>
              <th scope="col" class="num cc-inout-col">Out</th>
              <th scope="col" class="num">Balance after</th>
              <th scope="col" class="num cc-worst-col">If nothing comes in</th>
            </tr>
          </thead>
          <tbody v-if="calendar.rows.length">
            <template v-for="week in weeks" :key="week.key">
              <tr class="cc-week"><th colspan="6" scope="rowgroup" class="cc-week-cell">{{ week.label }}</th></tr>
              <tr
                v-for="(r, i) in week.rows"
                :key="week.key + i"
                :class="{ 'cc-row-bad': belowFloor(r.balance_after) }"
                data-testid="cc-row"
              >
                <td class="cc-date">{{ dayLabel(r.date) }}</td>
                <td>
                  <router-link v-if="linkFor(r)" :to="linkFor(r)">{{ r.label }}</router-link>
                  <span v-else>{{ r.label }}</span>
                  <Tag
                    v-if="r.certainty === 'expected'"
                    value="Estimate"
                    severity="secondary"
                    class="cc-tag"
                    v-tooltip.top="r.detail || 'Projected, not a due date'"
                  />
                  <div class="cc-amt-mobile" :class="r.direction === 'in' ? 'cc-in' : 'cc-out'">
                    {{ r.direction === 'in' ? '+' : '−' }}{{ money(r.amount) }}
                  </div>
                </td>
                <td class="num cc-in cc-inout-col">{{ r.direction === 'in' ? money(r.amount) : '' }}</td>
                <td class="num cc-out cc-inout-col">{{ r.direction === 'out' ? money(r.amount) : '' }}</td>
                <td class="num cc-bal">{{ money(r.balance_after) }}</td>
                <td class="num cc-worst-col cc-muted">{{ money(r.balance_if_nothing_comes_in) }}</td>
              </tr>
            </template>
          </tbody>
          <tbody v-else>
            <tr><td colspan="6" class="cc-empty">Nothing dated in the next {{ calendar.days }} days.</td></tr>
          </tbody>
        </table>
      </div>

      <div class="cc-unscheduled">
        <div class="cc-un" data-testid="cc-customers-past-due">
          <h3>Customers past due · {{ money(pastDue.total) }}</h3>
          <p class="cc-muted">
            {{ pastDue.count }} {{ pastDue.count === 1 ? 'invoice' : 'invoices' }} with no date to plan on,
            so not in the balance above.
          </p>
          <ul v-if="pastDue.items.length">
            <li v-for="it in shown(pastDue.items)" :key="it.link.id">
              <router-link :to="linkFor(it)">{{ it.label }}</router-link>
              <span class="cc-muted">{{ it.due_date ? `due ${formatDate(it.due_date)}` : 'no due date' }}</span>
              <span class="num">{{ money(it.amount) }}</span>
            </li>
          </ul>
          <div class="cc-more">
            <a v-if="!showAll && pastDue.items.length > PREVIEW" href="#" data-testid="cc-show-more" @click.prevent="showAll = true">Show {{ pastDue.items.length - PREVIEW }} more</a>
            <router-link v-if="pastDue.count > PREVIEW" to="/billing">All {{ pastDue.count }} in Billing</router-link>
          </div>
        </div>
        <div class="cc-un" data-testid="cc-finished-unbilled">
          <h3>Finished jobs not billed · {{ money(finished.total) }}</h3>
          <p class="cc-muted">
            {{ finished.count }} completed {{ finished.count === 1 ? 'job' : 'jobs' }} not invoiced yet<template
              v-if="finished.unpriced"> ({{ finished.unpriced }} not priced)</template>, so not in the balance above.
          </p>
          <ul v-if="finished.items.length">
            <li v-for="it in shown(finished.items)" :key="it.link.id">
              <router-link :to="linkFor(it)">{{ it.label }}</router-link>
              <span class="cc-muted">{{ it.due_date ? `done ${formatDate(it.due_date)}` : 'no date' }}</span>
              <span class="num">{{ it.amount === null ? 'not priced yet' : money(it.amount) }}</span>
            </li>
          </ul>
          <div class="cc-more">
            <a v-if="!showAll && finished.items.length > PREVIEW" href="#" @click.prevent="showAll = true">Show {{ finished.items.length - PREVIEW }} more</a>
            <router-link v-if="finished.count > PREVIEW" to="/billing">All {{ finished.count }} in Billing</router-link>
          </div>
        </div>
        <div class="cc-un" data-testid="cc-bills-unscheduled">
          <h3>Bills past due or undated · {{ money(bills.total) }}</h3>
          <p class="cc-muted">
            {{ bills.count }} open {{ bills.count === 1 ? 'bill' : 'bills' }} with no future due date,
            so not in the balance above.
          </p>
          <ul v-if="bills.items.length">
            <li v-for="it in shown(bills.items)" :key="it.link.id">
              <router-link :to="linkFor(it)">{{ it.label }}</router-link>
              <span class="cc-muted">{{ it.due_date ? `due ${formatDate(it.due_date)}` : 'no due date' }}</span>
              <span class="num">{{ money(it.amount) }}</span>
            </li>
          </ul>
          <div class="cc-more">
            <a v-if="!showAll && bills.items.length > PREVIEW" href="#" @click.prevent="showAll = true">Show {{ bills.items.length - PREVIEW }} more</a>
            <router-link v-if="bills.count > PREVIEW" to="/vendor-bills">All {{ bills.count }} in Vendor Bills</router-link>
          </div>
        </div>
      </div>
      <p v-if="jobNote" class="cc-muted cc-note" data-testid="cc-jobs-note">{{ jobNote }}</p>
    </template>

    <Dialog v-model:visible="settingsOpen" header="Cash floor and operating accounts" modal :style="{ width: 'min(34rem, 95vw)' }">
      <div class="cc-form">
        <label class="cc-field">
          <span>Warn me when the balance would go under</span>
          <InputNumber v-model="draftFloor" mode="currency" currency="USD" locale="en-US" :min="0" placeholder="No floor" data-testid="cc-floor-input" />
          <small class="cc-muted">Leave empty for no floor.</small>
        </label>
        <fieldset class="cc-field">
          <legend>Operating accounts (the starting balance)</legend>
          <label v-for="a in allAccounts" :key="a.id" class="cc-acct">
            <Checkbox v-model="draftAccounts" :value="a.id" :inputId="`cc-acct-${a.id}`" />
            <span>{{ a.name }}</span>
            <span class="cc-muted num">{{ a.balance === null ? 'no balance' : money(a.balance) }}</span>
          </label>
          <small class="cc-muted">None ticked means the default: every synced account with a balance of zero or more.</small>
        </fieldset>
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" @click="settingsOpen = false" />
        <Button label="Save" icon="pi pi-check" :loading="saving" data-testid="cc-settings-save" @click="saveSettings" />
      </template>
    </Dialog>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import Button from 'primevue/button';
import Checkbox from 'primevue/checkbox';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import Message from 'primevue/message';
import ProgressSpinner from 'primevue/progressspinner';
import SelectButton from 'primevue/selectbutton';
import Tag from 'primevue/tag';
import { CASH_CALENDAR_DAY_OPTIONS, useCashCalendar } from '../../composables/useCashCalendar';
import { formatDate, formatMoney as money } from '../../composables/useFormatters';
import { normalizeRole } from '../../constants/roles';
import { useAuthStore } from '../../stores/auth';

const cc = useCashCalendar();
const { days, calendar, loading, error, saving } = cc;
const auth = useAuthStore();

// PUT /api/forecast/settings is owner/admin only; don't offer what 403s.
const canEdit = computed(() => ['owner', 'admin'].includes(normalizeRole(auth.role)));

const dayOptions = CASH_CALENDAR_DAY_OPTIONS.map((n) => ({ label: `${n} days`, value: n }));
const summary = computed(() => calendar.value?.summary || {});
const EMPTY = { count: 0, total: 0, items: [] };
// The past-due lists sit under the calendar; five each keeps the answer on
// one screen, the rest are a click away.
const PREVIEW = 5;
const showAll = ref(false);
const shown = (items) => (showAll.value ? items : items.slice(0, PREVIEW));
const pastDue = computed(() => calendar.value?.unscheduled?.customer_invoices_past_due || EMPTY);
const bills = computed(() => calendar.value?.unscheduled?.vendor_bills_past_due_or_undated || EMPTY);
const finished = computed(() => calendar.value?.unscheduled?.finished_jobs_not_billed || EMPTY);
const includedNames = computed(() => {
  const names = (calendar.value?.accounts?.included || []).map((a) => a.name);
  return names.length ? names.join(', ') : 'no accounts';
});
const allAccounts = computed(() => {
  const acc = calendar.value?.accounts || { included: [], excluded: [] };
  return [...acc.included, ...acc.excluded].sort((a, b) => a.name.localeCompare(b.name));
});

const jobNote = computed(() => {
  const n = calendar.value?.notes || {};
  const parts = [];
  const plural = (k, one, many) => `${k} scheduled ${k === 1 ? one : many}`;
  if (n.jobs_without_accepted_estimate) parts.push(`${plural(n.jobs_without_accepted_estimate, 'job has', 'jobs have')} no accepted estimate`);
  if (n.jobs_already_invoiced) parts.push(`${plural(n.jobs_already_invoiced, 'job is', 'jobs are')} already fully invoiced`);
  return parts.length ? `${parts.join('; ')}, so not counted as money in from the job.` : '';
});

function belowFloor(value) {
  const floor = calendar.value?.floor;
  return floor !== null && floor !== undefined && value !== null && value !== undefined && value < floor;
}

function dayLabel(iso) {
  return iso ? formatDate(iso, { options: { weekday: 'short', month: 'short', day: 'numeric' } }) : '—';
}

function mondayOf(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  dt.setDate(dt.getDate() - ((dt.getDay() + 6) % 7));
  return dt;
}

const weeks = computed(() => {
  const out = [];
  for (const r of calendar.value?.rows || []) {
    const monday = mondayOf(r.date);
    const key = monday.toDateString();
    let wk = out[out.length - 1];
    if (!wk || wk.key !== key) {
      wk = { key, label: `Week of ${formatDate(monday, { options: { month: 'short', day: 'numeric' } })}`, rows: [] };
      out.push(wk);
    }
    wk.rows.push(r);
  }
  return out;
});

const LINKS = {
  invoice: (id) => `/billing/${id}`,
  job: (id) => `/jobs/${id}`,
  vendor_bill: (id) => `/vendor-bills/${id}`,
  recurring_stream: () => '/forecasting/recurring',
};
function linkFor(item) {
  const link = item?.link;
  return link && LINKS[link.kind] ? LINKS[link.kind](link.id) : null;
}

async function setDays(n) {
  if (n && n !== days.value) await cc.setDays(n);
}

const settingsOpen = ref(false);
const draftFloor = ref(null);
const draftAccounts = ref([]);
function openSettings() {
  draftFloor.value = calendar.value?.floor ?? null;
  draftAccounts.value = calendar.value?.accounts?.chosen
    ? calendar.value.accounts.included.map((a) => a.id)
    : [];
  settingsOpen.value = true;
}
async function saveSettings() {
  await cc.saveChoices({ floor: draftFloor.value, accountIds: draftAccounts.value });
  settingsOpen.value = false;
}

onMounted(() => cc.load());
</script>

<style scoped>
.cash-calendar { margin-bottom: 1.25rem; }
.cc-heading { font-size: 1.05rem; font-weight: 600; margin: 0; }
.cc-head { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; justify-content: space-between; }
.cc-controls { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.cc-loading { display: flex; justify-content: center; padding: 1.5rem; }
.cc-nudges { display: grid; gap: 0.5rem; margin: 0.75rem 0; }
.cc-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
  gap: 0.75rem;
  margin: 0.75rem 0 1rem;
}
.cc-tile {
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  padding: 0.65rem 0.85rem;
}
.cc-tile.cc-bad { border-color: var(--p-red-500, #ef4444); }
.cc-tile.cc-bad .cc-tile-value { color: var(--p-red-600, #dc2626); }
.cc-tile-label { color: var(--p-text-muted-color); font-size: 0.8rem; }
.cc-tile-value { font-size: 1.35rem; font-weight: 600; margin-top: 0.15rem; }
.cc-tile-sub { color: var(--p-text-muted-color); font-size: 0.75rem; margin-top: 0.2rem; }
.cc-inout { display: flex; gap: 0.75rem; flex-wrap: wrap; font-size: 1.05rem; }
.cc-in { color: var(--p-green-600, #16a34a); }
.cc-out { color: var(--p-red-600, #dc2626); }
.cc-table-wrap { overflow-x: auto; }
.cc-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.cc-table th, .cc-table td { padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--p-content-border-color); text-align: left; }
.cc-table thead th { color: var(--p-text-muted-color); font-weight: 600; font-size: 0.8rem; white-space: nowrap; }
.cc-table .num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.cc-week th { background: var(--p-content-hover-background); font-size: 0.8rem; color: var(--p-text-muted-color); }
.cc-date { white-space: nowrap; }
.cc-bal { font-weight: 600; }
.cc-row-bad .cc-bal { color: var(--p-red-600, #dc2626); }
.cc-tag { margin-left: 0.4rem; font-size: 0.7rem; }
.cc-muted { color: var(--p-text-muted-color); }
.cc-empty { text-align: center; color: var(--p-text-muted-color); padding: 1rem; }
.cc-unscheduled { display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 1rem; margin-top: 1rem; }
.cc-un h3 { font-size: 0.95rem; margin: 0 0 0.25rem; }
.cc-un p { font-size: 0.8rem; margin: 0 0 0.4rem; }
.cc-un ul { list-style: none; padding: 0; margin: 0 0 0.4rem; font-size: 0.85rem; }
.cc-un li { display: flex; flex-wrap: wrap; column-gap: 0.25rem; align-items: baseline; padding: 0.25rem 0; }
.cc-un li a { flex-basis: 100%; }
.cc-un li .num { margin-left: auto; }
.cc-note { font-size: 0.8rem; margin-top: 0.75rem; }
.cc-warn-text { color: var(--color-warning-500); font-weight: 600; }
.cc-more { display: flex; gap: 1rem; font-size: 0.85rem; }
.cc-form { display: grid; gap: 1rem; }
.cc-field { display: grid; gap: 0.35rem; border: 0; padding: 0; margin: 0; }
.cc-field legend { margin-bottom: 0.35rem; }
.cc-acct { display: flex; gap: 0.5rem; align-items: center; }
.cc-acct .num { margin-left: auto; }
.cc-controls :deep(.p-togglebutton) { white-space: nowrap; }
.cc-amt-mobile { display: none; font-size: 0.8rem; font-variant-numeric: tabular-nums; }
@media (max-width: 640px) {
  .cc-controls { width: 100%; }
  .cc-controls :deep(.p-selectbutton) { display: flex; width: 100%; }
  .cc-controls :deep(.p-togglebutton) { flex: 1 1 0; padding-inline: 0.25rem; font-size: 0.9rem; }
  .cc-inout-col { display: none; }
  .cc-amt-mobile { display: block; }
  .cc-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.5rem; }
  .cc-tile { padding: 0.5rem 0.6rem; }
  .cc-tile-value { font-size: 1.1rem; }
  .cc-worst-col { display: none; }
  .cc-table { font-size: 0.82rem; }
  .cc-table th, .cc-table td { padding: 0.35rem 0.4rem; }
}
</style>

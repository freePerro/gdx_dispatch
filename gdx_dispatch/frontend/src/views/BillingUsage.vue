<!--
  SS-24 Slice F — BillingUsage.vue

  Per-tenant usage dashboard showing current-period counters + plan limits
  for each metered event_type. Reads GET /api/billing/usage.

  TODO: mount in gdx/frontend/src/router/index.js at path
    /admin/billing-usage once SS-24 integration lands. Gate behind
    tenant-admin capability check (role: admin/owner).
  TODO: backend router (gdx/routers/billing_usage.py) not
    yet mounted in gdx/main.py — the SFC degrades gracefully on 404/500.
-->
<template>
    <section class="billing-usage view-card" data-testid="billing-usage-view">
    <header class="header view-heading-row">
      <Button
        icon="pi pi-arrow-left"
        aria-label="Back"
        text
        severity="secondary"
        size="small"
        class="back-button"
        @click="$router.back()"
      />
      <h1 class="view-heading">Billing Usage</h1>
      <p class="muted" data-testid="billing-usage-period">
        Period:
        <select
          v-model="periodKind"
          data-testid="billing-usage-period-select"
          @change="load"
        >
          <option value="hour">Current hour</option>
          <option value="day">Current day</option>
          <option value="month">Current month</option>
        </select>
        <span v-if="summary.period_start" class="muted">
          (since {{ formatDate(summary.period_start) }})
        </span>
      </p>
    </header>

    <div v-if="loading" class="muted" data-testid="billing-usage-loading">
      Loading…
    </div>

    <p
      v-else-if="errorMessage"
      class="error"
      role="alert"
      data-testid="billing-usage-error"
    >
      {{ errorMessage }}
    </p>

    <section v-else data-testid="billing-usage-content">
      <div class="plan-card" data-testid="billing-usage-plan">
        <strong>Plan:</strong>
        <span data-testid="billing-usage-plan-code">{{ summary.plan_code || 'free' }}</span>
      </div>

      <table
        v-if="summary.usage && summary.usage.length"
        data-testid="billing-usage-table"
      >
        <thead>
          <tr>
            <th>Event type</th>
            <th>Quantity</th>
            <th>Limit</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in summary.usage"
            :key="row.event_type"
            :data-testid="`billing-usage-row-${row.event_type}`"
          >
            <td><code>{{ row.event_type }}</code></td>
            <td :data-testid="`billing-usage-qty-${row.event_type}`">
              {{ row.quantity }}
            </td>
            <td>
              <template v-if="row.limit === null">—</template>
              <template v-else>{{ row.limit }}</template>
            </td>
            <td>
              <span
                v-if="row.over_limit"
                class="over-limit"
                :data-testid="`billing-usage-over-${row.event_type}`"
              >
                Over limit
              </span>
              <span
                v-else-if="row.limit && row.quantity / row.limit > 0.8"
                class="near-limit"
              >
                Near limit
              </span>
              <span v-else class="ok">OK</span>
            </td>
          </tr>
        </tbody>
      </table>

      <p v-else class="muted" data-testid="billing-usage-empty">
        No metered events this period.
      </p>

      <section
        v-if="summary.overages_this_period && summary.overages_this_period.length"
        class="overages"
        data-testid="billing-usage-overages"
      >
        <h3>Overages this period</h3>
        <ul>
          <li
            v-for="o in summary.overages_this_period"
            :key="o.event_type + o.detected_at"
            :data-testid="`billing-usage-overage-${o.event_type}`"
          >
            <code>{{ o.event_type }}</code>:
            observed <strong>{{ o.observed_quantity }}</strong>
            (limit {{ o.limit_value }})
            at {{ formatDate(o.detected_at) }}
          </li>
        </ul>
      </section>
    </section>
    </section>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import Button from 'primevue/button'
import { createApiClient } from '../composables/useApi'

const api = createApiClient()
const USAGE_ENDPOINT = '/api/billing/usage'

const loading = ref(false)
const errorMessage = ref('')
const periodKind = ref('month')
const summary = ref({
  tenant_id: '',
  plan_code: 'free',
  period_kind: 'month',
  period_start: '',
  usage: [],
  overages_this_period: [],
})

function formatDate(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

async function load() {
  loading.value = true
  errorMessage.value = ''
  try {
    summary.value = await api.get(`${USAGE_ENDPOINT}?period_kind=${periodKind.value}`)
  } catch (err) {
    errorMessage.value = `Failed to load usage: ${err.message}`
  } finally {
    loading.value = false
  }
}

onMounted(load)

defineExpose({ load })
</script>

<style scoped>
.billing-usage {
  max-width: 900px;
  margin: 0 auto;
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.header h2 {
  margin: 0 0 0.25rem 0;
}
.muted {
  color: #666;
}
.error {
  color: #b00020;
}
.plan-card {
  padding: 0.5rem 0.75rem;
  background: #f4f4f6;
  border-radius: 4px;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th,
td {
  text-align: left;
  padding: 0.4rem 0.5rem;
  border-bottom: 1px solid #eee;
}
.over-limit {
  color: #b00020;
  font-weight: 600;
}
.near-limit {
  color: #b08000;
}
.ok {
  color: #2a7f2a;
}
.overages {
  margin-top: 1rem;
  padding: 0.75rem;
  background: #fff4f4;
  border: 1px solid #e6baba;
  border-radius: 4px;
}
</style>

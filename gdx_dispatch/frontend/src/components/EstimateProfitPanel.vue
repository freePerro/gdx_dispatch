<!--
  EstimateProfitPanel — Sprint 1.0.5 Phase 4.

  Side panel showing per-line + total profit/margin, computed from the
  EstimateLine snapshot fields the engine wrote at create.

  Visibility rule (per Doug 2026-04-25): hidden when user.role === 'tech'.
  Visible to dispatcher / admin / owner.

  Volume discount: read from /api/pricing-engine/settings on mount.
-->
<template>
  <aside v-if="visible" class="profit-panel" data-testid="estimate-profit-panel">
    <Card>
      <template #title>
        <div class="title-row">
          <span><i class="pi pi-chart-line" /> Profit</span>
          <Button v-if="!collapsed" icon="pi pi-chevron-right" text size="small"
            aria-label="Collapse" data-testid="profit-panel-collapse"
            @click="collapsed = true" />
          <Button v-else icon="pi pi-chevron-left" text size="small"
            aria-label="Expand" data-testid="profit-panel-expand"
            @click="collapsed = false" />
        </div>
      </template>
      <template v-if="!collapsed" #content>
        <div v-if="!hasAnyEngineLines" class="empty-state">
          <p class="muted">
            No engine-priced lines on this estimate yet.
          </p>
          <p class="hint">
            Lines created with cost + pricing category get tracked here.
            Manually-priced lines (legacy) show only their unit price.
          </p>
        </div>

        <div v-else>
          <!-- Per-line breakdown -->
          <div class="lines">
            <div v-for="line in engineLines" :key="line.id" class="line-row" :data-testid="`profit-line-${line.id}`">
              <div class="line-desc" :title="line.description">{{ line.description }}</div>
              <div class="line-numbers">
                <div><span class="muted">Cost</span> {{ currency(line.cost_snapshot * line.quantity) }}</div>
                <div><span class="muted">Sell</span> {{ currency(line.unit_price * line.quantity) }}</div>
                <div class="profit-row">
                  <span class="muted">Profit</span>
                  <strong>{{ currency((line.unit_price - line.cost_snapshot) * line.quantity) }}</strong>
                  <small>({{ percent(effectiveMargin(line)) }})</small>
                </div>
                <div v-if="line.pricing_source" class="source-badge" :class="`source-${line.pricing_source}`">
                  {{ sourceLabel(line.pricing_source) }}
                </div>
              </div>
            </div>
          </div>

          <Divider />

          <!-- Totals -->
          <div class="totals" data-testid="profit-totals">
            <div class="row"><span class="muted">Subtotal cost</span><strong>{{ currency(totals.subtotalCost) }}</strong></div>
            <div class="row"><span class="muted">Subtotal sell</span><strong>{{ currency(totals.subtotalSellPre) }}</strong></div>
            <div v-if="totals.volumeDiscountAmount > 0" class="row discount">
              <span class="muted">Volume discount ({{ percent(totals.volumeDiscountPct) }})</span>
              <strong>−{{ currency(totals.volumeDiscountAmount) }}</strong>
            </div>
            <div v-if="totals.volumeDiscountAmount > 0" class="row">
              <span class="muted">After discount</span>
              <strong>{{ currency(totals.subtotalSell) }}</strong>
            </div>
            <Divider class="thin" />
            <div class="row big">
              <span>Net profit</span>
              <strong :class="{ negative: totals.profit < 0 }">{{ currency(totals.profit) }}</strong>
            </div>
            <div class="row">
              <span class="muted">Blended margin</span>
              <strong>{{ percent(totals.blendedMargin) }}</strong>
            </div>
            <p v-if="hasManualLines" class="warn-note">
              ⚠ Estimate has {{ manualLineCount }} manually-priced line{{ manualLineCount === 1 ? '' : 's' }} (no cost data) — totals above exclude them.
            </p>
          </div>
        </div>
      </template>
    </Card>
  </aside>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useAuthStore } from '../stores/auth';
import { isTechnician } from '../constants/roles';
import { useApi } from '../composables/useApi';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Divider from 'primevue/divider';

const props = defineProps({
  // Array of estimate line objects from /api/estimates/{id}
  lines: { type: Array, required: true },
  // Sprint 1.0.6 — customer's trailing-365-day paid volume. Drives the
  // volume-discount tier lookup. Defaults to 0 (no discount) when unknown
  // (e.g. anonymous quotes or the customer detail isn't loaded yet).
  customerRollingVolume: { type: Number, default: 0 },
});

const auth = useAuthStore();
const api = useApi();
const collapsed = ref(false);
const settings = ref({ volume_discount_enabled: false, volume_tiers: [] });

// Visibility: hide from techs. Default visible if role is unknown so we
// don't accidentally hide from admin/owner due to a missing field.
const visible = computed(() => !isTechnician(auth.user?.role || auth.user?.user_role));

const engineLines = computed(() =>
  props.lines.filter(l => l.cost_snapshot != null && l.margin_pct_snapshot != null)
);

const hasAnyEngineLines = computed(() => engineLines.value.length > 0);
const manualLineCount = computed(() => props.lines.length - engineLines.value.length);
const hasManualLines = computed(() => manualLineCount.value > 0);

function effectiveMargin(line) {
  // Override beats snapshot
  if (line.margin_pct_override != null) return line.margin_pct_override;
  return line.margin_pct_snapshot;
}

const totals = computed(() => {
  let subtotalCost = 0;
  let subtotalSellPre = 0;
  for (const l of engineLines.value) {
    subtotalCost += l.cost_snapshot * l.quantity;
    subtotalSellPre += l.unit_price * l.quantity;
  }
  // Volume discount: find first matching tier keyed on customer rolling
  // 12mo paid volume (Sprint 1.0.6). Master toggle still gates; per-class
  // gate is enforced server-side, so client may over-show by tier value
  // for a class admin disabled — server is the authority on persisted price.
  let volumeDiscountPct = 0;
  if (
    settings.value.volume_discount_enabled
    && settings.value.volume_tiers?.length
    && props.customerRollingVolume > 0
  ) {
    const v = props.customerRollingVolume;
    const matches = settings.value.volume_tiers.filter(t =>
      v >= t.volume_min_12mo &&
      (t.volume_max_12mo == null || v < t.volume_max_12mo)
    );
    if (matches.length === 1) volumeDiscountPct = matches[0].discount_pct;
    // 0 or 2+ matches → 0 (server engine fail-louds on overlaps; client just shows 0)
  }
  const volumeDiscountAmount = subtotalSellPre * volumeDiscountPct;
  const subtotalSell = subtotalSellPre - volumeDiscountAmount;
  const profit = subtotalSell - subtotalCost;
  const blendedMargin = subtotalSell > 0 ? profit / subtotalSell : 0;
  return { subtotalCost, subtotalSellPre, volumeDiscountPct, volumeDiscountAmount, subtotalSell, profit, blendedMargin };
});

function currency(v) {
  if (v == null || isNaN(v)) return '$0.00';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(v);
}

function percent(v) {
  if (v == null || isNaN(v)) return '0%';
  return `${(v * 100).toFixed(1)}%`;
}

function sourceLabel(src) {
  return ({
    tier: 'Standard tier',
    wholesale_tier: 'Wholesale tier',
    customer_override: 'Customer override',
    line_override: 'Line override',
    labor_matrix: 'Labor',
  })[src] || src;
}

onMounted(async () => {
  if (!visible.value) return;
  try {
    settings.value = await api.get('/api/pricing-engine/settings');
  } catch (e) {
    // Non-fatal — admin endpoint may 403 for non-admin viewers.
    // Without settings, panel still works but volume discount = 0.
    console.info('profit-panel: settings unavailable (probably non-admin viewer)', e?.message);
  }
});
</script>

<style scoped>
.profit-panel { width: 320px; min-width: 280px; }
.title-row { display: flex; justify-content: space-between; align-items: center; }
.empty-state { text-align: center; padding: 12px 0; }
.empty-state .hint { font-size: 0.85em; color: var(--p-text-muted-color); margin-top: 6px; }
.muted { color: var(--p-text-muted-color); font-size: 0.85em; }
.lines { display: flex; flex-direction: column; gap: 8px; }
.line-row { padding: 6px 0; border-bottom: 1px solid var(--surface-border); }
.line-row:last-child { border-bottom: none; }
.line-desc { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
.line-numbers { display: grid; grid-template-columns: 1fr; gap: 2px; font-size: 0.9em; }
.profit-row { display: flex; gap: 6px; align-items: baseline; }
.profit-row strong { color: var(--p-green-600); }
.profit-row small { color: var(--p-text-muted-color); }
.source-badge {
  display: inline-block;
  font-size: 0.75em;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--p-content-border-color);
  color: var(--p-text-muted-color);
  margin-top: 2px;
  width: fit-content;
}
.source-badge.source-line_override { background: var(--orange-100); color: var(--orange-800); }
.source-badge.source-customer_override { background: var(--purple-100); color: var(--purple-800); }
.source-badge.source-wholesale_tier { background: var(--p-blue-100); color: var(--p-blue-700); }
.source-badge.source-labor_matrix { background: var(--teal-100); color: var(--teal-800); }
.totals .row { display: flex; justify-content: space-between; padding: 4px 0; }
.totals .row.big { font-size: 1.1em; padding-top: 8px; }
.totals .row.discount strong { color: var(--orange-600); }
.totals .row strong.negative { color: var(--p-red-600); }
.warn-note { font-size: 0.8em; color: var(--orange-700); margin-top: 8px; }
.thin { margin: 4px 0; }
</style>

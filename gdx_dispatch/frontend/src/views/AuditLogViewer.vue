<!--
  SS-28 slice F — tenant-admin Audit Log Viewer.

  INTEGRATION TODO: mount in gdx/frontend/src/router/index.js at path
    /admin/audit-log once SS-28 integration lands. Gate route behind
    tenant-admin capability check (same gate as TenantAdminApiKeys).
  INTEGRATION TODO: backend /api/admin/audit-log (see
    gdx/routers/consumer_audit.py) is expected to return the shape
    { total, offset, limit, rows[], chain_integrity: {valid, break_at} }.

  Plain-HTML controls + a fetch call. Avoids PrimeVue / router /
  toast deps so the view is standalone-testable and so integration
  with the app's layout wrapper happens at mount time, not inline.
-->
<template>
    <section class="audit-log-viewer view-card" data-testid="audit-log-viewer">
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
      <h1 class="view-heading">Audit Log</h1>
      <p class="muted">
        Every platform-consumer API call for your tenant, newest first.
        Rows are tamper-evident — an integrity breach shows a red badge.
      </p>
    </header>

    <!-- Chain integrity badge — compliance signal at a glance. -->
    <div
      v-if="chainIntegrity"
      class="chain-badge"
      :class="chainIntegrity.valid ? 'chain-ok' : 'chain-broken'"
      data-testid="chain-integrity-badge"
    >
      <span v-if="chainIntegrity.valid">Chain intact</span>
      <span v-else>
        Chain BROKEN at row {{ chainIntegrity.break_at }} — escalate to ops
      </span>
    </div>

    <!-- Filter controls -->
    <form
      class="filters"
      data-testid="audit-filters"
      @submit.prevent="reload"
    >
      <label>
        Action
        <input
          v-model="filters.action"
          type="text"
          data-testid="filter-action"
        />
      </label>
      <label>
        Resource type
        <input
          v-model="filters.resource_type"
          type="text"
          data-testid="filter-resource-type"
        />
      </label>
      <label>
        Principal identity
        <input
          v-model="filters.principal_identity_id"
          type="text"
          data-testid="filter-principal"
        />
      </label>
      <label>
        Since
        <input
          v-model="filters.since"
          type="datetime-local"
          data-testid="filter-since"
        />
      </label>
      <label>
        Until
        <input
          v-model="filters.until"
          type="datetime-local"
          data-testid="filter-until"
        />
      </label>
      <button type="submit" data-testid="apply-filters">Apply</button>
      <button
        type="button"
        data-testid="export-csv"
        :disabled="rows.length === 0"
        @click="exportCsv"
      >
        Export CSV
      </button>
    </form>

    <!-- Status / loading -->
    <div v-if="loading" data-testid="loading">Loading…</div>
    <div v-else-if="error" class="error" data-testid="error">{{ error }}</div>

    <!-- Table -->
    <table v-else class="audit-table" data-testid="audit-table">
      <thead>
        <tr>
          <th>When</th>
          <th>Action</th>
          <th>Resource</th>
          <th>Principal</th>
          <th>Result</th>
          <th>IP</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="row.id"
          :data-testid="`audit-row-${row.id}`"
        >
          <td>{{ row.created_at }}</td>
          <td>{{ row.action }}</td>
          <td>{{ row.resource_type }}:{{ row.resource_id }}</td>
          <td>{{ row.principal_identity_id }}</td>
          <td :class="`result-${row.result}`">{{ row.result }}</td>
          <td>{{ row.ip_address }}</td>
        </tr>
        <tr v-if="rows.length === 0">
          <td colspan="6" class="empty" data-testid="empty-state">
            No audit rows match.
          </td>
        </tr>
      </tbody>
    </table>

    <!-- Pagination — hidden when no rows so the contradictory
         "Page 1 of N (M rows)" line doesn't appear next to "No rows
         match" when filters narrow the result to zero. -->
    <nav v-if="rows.length > 0" class="pager" data-testid="pager">
      <button
        type="button"
        data-testid="prev-page"
        :disabled="offset === 0"
        @click="prevPage"
      >
        Prev
      </button>
      <span
        >Page {{ pageNumber }} of {{ pageCount }} ({{ total }} rows)</span
      >
      <button
        type="button"
        data-testid="next-page"
        :disabled="offset + limit >= total"
        @click="nextPage"
      >
        Next
      </button>
    </nav>
    </section>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import Button from "primevue/button";
import { createApiClient } from "../composables/useApi";

const props = defineProps({
  // Injectable for tests. In production the route uses the useApi
  // composable; tests pass a plain fetch-like fn to stub it cleanly
  // without pulling the whole composable stack.
  fetchFn: {
    type: Function,
    default: null,
  },
});

let _api = null;
function _getApi() {
  if (!_api) _api = createApiClient();
  return _api;
}

const rows = ref([]);
const total = ref(0);
const offset = ref(0);
const limit = ref(50);
const chainIntegrity = ref(null);
const loading = ref(false);
const error = ref("");

const filters = ref({
  action: "",
  resource_type: "",
  principal_identity_id: "",
  since: "",
  until: "",
});

const pageNumber = computed(() =>
  Math.floor(offset.value / limit.value) + 1
);
const pageCount = computed(() =>
  Math.max(1, Math.ceil(total.value / limit.value))
);

function buildQuery() {
  const p = new URLSearchParams();
  p.set("limit", String(limit.value));
  p.set("offset", String(offset.value));
  for (const [k, v] of Object.entries(filters.value)) {
    if (v) p.set(k, v);
  }
  return p.toString();
}

async function reload() {
  loading.value = true;
  error.value = "";
  try {
    const url = `/api/admin/audit-log?${buildQuery()}`;
    const data = props.fetchFn
      ? await props.fetchFn(url)
      : await _getApi().get(url);
    // Backend returns `items` (admin_ops.py:402) with the platform audit-log
    // schema (event_type / actor_id / entity_type / entity_id), but this
    // viewer was authored against the SS-28 design (action / principal_identity_id /
    // resource_type / resource_id). Two-shape compatibility shim so the table
    // renders against either wire format without a backend rename. S111 2026-05-09.
    const raw = data.items || data.rows || [];
    rows.value = raw.map((r) => ({
      ...r,
      action: r.action || r.event_type || '',
      resource_type: r.resource_type || r.entity_type || '',
      resource_id: r.resource_id || r.entity_id || '',
      principal_identity_id: r.principal_identity_id || r.actor_id || '',
      result: r.result || (r.payload && r.payload.result) || 'ok',
      ip_address: r.ip_address || (r.payload && r.payload.ip_address) || '',
    }));
    total.value = data.total || 0;
    chainIntegrity.value = data.chain_integrity || null;
  } catch (e) {
    error.value = e?.message || "Failed to load audit log";
  } finally {
    loading.value = false;
  }
}

function nextPage() {
  offset.value += limit.value;
  reload();
}

function prevPage() {
  offset.value = Math.max(0, offset.value - limit.value);
  reload();
}

function exportCsv() {
  // Client-side CSV of the CURRENT page. Export of the full result set
  // is deferred to backend (INTEGRATION TODO — /api/admin/audit-log.csv).
  const headers = [
    "id",
    "created_at",
    "action",
    "resource_type",
    "resource_id",
    "principal_identity_id",
    "result",
    "ip_address",
    "user_agent",
  ];
  const lines = [headers.join(",")];
  for (const r of rows.value) {
    lines.push(
      headers
        .map((h) => {
          const v = r[h] ?? "";
          // RFC 4180-ish quoting.
          const s = String(v).replace(/"/g, '""');
          return /[",\n]/.test(s) ? `"${s}"` : s;
        })
        .join(",")
    );
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "audit-log.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

onMounted(() => {
  reload();
});

defineExpose({ reload, exportCsv, rows, total, chainIntegrity });
</script>

<style scoped>
.audit-log-viewer {
  padding: 1rem;
}
.chain-badge {
  padding: 0.5rem 0.75rem;
  border-radius: 4px;
  margin-bottom: 0.75rem;
  font-weight: 600;
}
.chain-ok {
  background: #e6f4ea;
  color: #1e7e34;
}
.chain-broken {
  background: #fde8e8;
  color: #a61b1b;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-bottom: 1rem;
}
.filters label {
  display: flex;
  flex-direction: column;
  font-size: 0.85rem;
}
.audit-table {
  width: 100%;
  border-collapse: collapse;
}
.audit-table th,
.audit-table td {
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid #eee;
  font-size: 0.88rem;
}
.result-ok {
  color: #1e7e34;
}
.result-denied {
  color: #a61b1b;
}
.result-error {
  color: #b45309;
}
.empty {
  text-align: center;
  color: #888;
  padding: 1rem;
}
.pager {
  margin-top: 1rem;
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
.error {
  color: #a61b1b;
}
.muted {
  color: #666;
}
</style>

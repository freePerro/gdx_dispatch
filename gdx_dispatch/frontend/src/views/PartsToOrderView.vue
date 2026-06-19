<template>
    <section class="parts-view view-card">
      <div class="page-header">
        <h2>Parts to Order <Tag v-if="parts.length" :value="String(parts.length)" severity="warn" rounded /></h2>
      </div>

      <DataTable
      responsiveLayout="scroll" :value="parts" :loading="loading" stripedRows sortField="supplier" :sortOrder="1"
        :paginator="true" :rows="20" data-testid="parts-table">
        <template #empty>
          <EmptyState icon="pi pi-box" title="No parts to order" message="When technicians flag jobs as needing parts, they'll appear here." />
        </template>
        <Column field="part_name" header="Part" sortable />
        <Column field="quantity" header="Qty" sortable style="width: 5rem" />
        <Column field="supplier" header="Supplier" sortable />
        <Column field="urgency" header="Urgency" sortable>
          <template #body="{ data }">
            <Tag :value="data.urgency" :severity="urgSeverity(data.urgency)" />
          </template>
        </Column>
        <Column field="job_title" header="Job" />
        <Column field="customer_name" header="Customer" />
        <Column field="status" header="Status">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column header="Actions" style="width: 14rem">
          <template #body="{ data }">
            <Button v-if="data.status === 'needed'" label="Mark Ordered" icon="pi pi-shopping-cart" size="small"
              severity="info" @click="updateStatus(data.id, 'ordered')" />
            <Button v-if="data.status === 'ordered'" label="Mark Received" icon="pi pi-check" size="small"
              severity="success" @click="updateStatus(data.id, 'received')" />
          </template>
        </Column>
      </DataTable>
    </section>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from "vue";
import { useToast } from "primevue/usetoast";
import EmptyState from "../components/EmptyState.vue";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import DataTable from "primevue/datatable";
import Column from "primevue/column";
import Button from "primevue/button";
import Tag from "primevue/tag";

const api = useApi();
const toast = useToast();
const parts = ref([]);
const loading = ref(false);

// Phase 1.3 C5 (in-app fallback) — surface NEW critical parts since the
// last refresh with a toast + audible ping, polled at 30s. Real Web Push
// arrives in Sprint 1.5.
const POLL_MS = 30000;
const audibleCritical = ref(true);
const seenIds = new Set();
let pollTimer = null;
let audioCtx = null;
let userGestureSeen = false;

function urgSeverity(u) {
  return u === "critical" ? "danger" : u === "urgent" ? "warn" : "secondary";
}
function statusSeverity(s) {
  return s === "needed" ? "warn" : s === "ordered" ? "info" : "success";
}

function primeAudioOnGesture() {
  // Chrome/Safari refuse to start an AudioContext until a user gesture has
  // fired on the document. Build (or resume) the context on the FIRST
  // pointerdown/keydown, then drop the listeners — every later ping uses
  // the already-warm context.
  if (userGestureSeen) return;
  userGestureSeen = true;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!audioCtx) audioCtx = new Ctx();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch {
    /* no-op — audio is best-effort */
  }
}

function playPing() {
  if (!audibleCritical.value) return;
  try {
    if (!audioCtx || !userGestureSeen) {
      // No gesture yet → silently no-op. The toast still fires; the next
      // ping after the dispatcher clicks anywhere will be audible.
      return;
    }
    if (audioCtx.state === "suspended") audioCtx.resume();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.45);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.5);
  } catch {
    /* no-op — audio is best-effort */
  }
}

async function loadDispatchConfig() {
  try {
    const r = await api.get("/api/parts-needed/dispatch-config");
    audibleCritical.value = !!r?.audible_critical;
  } catch {
    audibleCritical.value = true;
  }
}

async function loadParts({ alertOnNewCritical = false } = {}) {
  loading.value = true;
  try {
    const r = await api.get("/api/parts-needed/pending");
    const next = Array.isArray(r) ? r : [];
    if (alertOnNewCritical) {
      const newCriticals = next.filter(
        (p) => p.urgency === "critical" && p.status === "needed" && !seenIds.has(p.id),
      );
      if (newCriticals.length) {
        toast.add({
          severity: "error",
          summary:
            newCriticals.length === 1
              ? "Critical part flagged"
              : `${newCriticals.length} critical parts flagged`,
          detail: newCriticals
            .slice(0, 3)
            .map((p) => `${p.part_name}${p.customer_name ? " — " + p.customer_name : ""}`)
            .join("; "),
          life: 8000,
        });
        playPing();
      }
    }
    parts.value = next;
    next.forEach((p) => seenIds.add(p.id));
  } catch {
    parts.value = [];
  } finally {
    loading.value = false;
  }
}

async function updateStatus(partId, status) {
  try {
    await api.patch(`/api/parts-needed/${partId}/status`, { status });
    toast.add({ severity: "success", summary: "Updated", detail: `Part marked as ${status}`, life: 3000 });
    await loadParts();
  } catch {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to update", life: 4000 });
  }
}

onMounted(async () => {
  await loadDispatchConfig();
  await loadParts();  // initial load — seeds seenIds, no ping
  pollTimer = setInterval(() => loadParts({ alertOnNewCritical: true }), POLL_MS);
  // Latch the AudioContext to the first user gesture so playPing can
  // make sound. The listeners self-remove after firing once.
  window.addEventListener("pointerdown", primeAudioOnGesture, { once: true });
  window.addEventListener("keydown", primeAudioOnGesture, { once: true });
});

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer);
  window.removeEventListener("pointerdown", primeAudioOnGesture);
  window.removeEventListener("keydown", primeAudioOnGesture);
  if (audioCtx) {
    try { audioCtx.close(); } catch { /* no-op */ }
  }
});
</script>

<style scoped>
.parts-view { padding: 1.5rem; }
.page-header { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem; }
.page-header h2 { margin: 0; display: flex; align-items: center; gap: 0.5rem; }
</style>

<template>
  <!-- Captured doors as a by-size list. A job can carry several doors (the
       Swenstad job has two, 14x12 and 10x10); each is its own row, labelled by
       size, and clicks open to reveal that door's full build spec. -->
  <div class="door-spec-list" data-testid="door-spec-list">
    <div v-for="(door, i) in doors" :key="door.line_id || i" class="door-item">
      <button
        type="button"
        class="door-toggle"
        :aria-expanded="isOpen(door, i) ? 'true' : 'false'"
        :data-testid="`door-toggle-${i}`"
        @click="toggle(door, i)"
      >
        <i class="pi door-chevron" :class="isOpen(door, i) ? 'pi-chevron-down' : 'pi-chevron-right'" />
        <span class="door-size">{{ doorSize(door) }}</span>
        <span v-if="doorSub(door)" class="door-sub">{{ doorSub(door) }}</span>
        <span v-if="door.quantity > 1" class="door-qty">×{{ door.quantity }}</span>
      </button>

      <div v-if="isOpen(door, i)" class="door-body" :data-testid="`door-body-${i}`">
        <dl class="door-grid">
          <div v-for="(val, key) in doorSpecs(door)" :key="key" class="door-row">
            <dt>{{ key }}</dt>
            <dd>{{ fmtVal(val) }}</dd>
          </div>
        </dl>
        <div v-if="door.windows && door.windows.length" class="door-windows">
          <div class="door-windows-head">Windows / sections</div>
          <ul>
            <li v-for="(w, wi) in door.windows" :key="wi">{{ fmtVal(w) }}</li>
          </ul>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, watch } from "vue";

const props = defineProps({
  doors: { type: Array, default: () => [] },
});

// Per-door open/closed, keyed by the door's stable line_id (NOT array index) so a
// background refetch — the mobile view refreshes when the offline queue drains —
// re-seeds the same doors without collapsing whichever one the tech had open. A
// single door opens by default (nothing to pick between); with several, all
// start collapsed so the size list is the entry point.
const open = reactive({});
function keyOf(door, i) {
  return door.line_id || `i${i}`;
}
function isOpen(door, i) {
  return !!open[keyOf(door, i)];
}
function toggle(door, i) {
  const k = keyOf(door, i);
  open[k] = !open[k];
}
watch(
  () => props.doors,
  (list) => {
    const live = new Set((list || []).map((d, i) => keyOf(d, i)));
    Object.keys(open).forEach((k) => { if (!live.has(k)) delete open[k]; });
    (list || []).forEach((d, i) => {
      const k = keyOf(d, i);
      if (!(k in open)) open[k] = list.length === 1;  // preserve existing state
    });
  },
  { immediate: true, deep: false },
);

// The size headline. Prefer the captured Size; else build it from W×H; else a
// generic label — never the order number (that's not a size).
function doorSize(door) {
  const id = door.identity || {};
  if (id.Size) return id.Size;
  if (id.Width && id.Height) return `${id.Width}" × ${id.Height}"`;
  return "Door";
}

// Secondary label under the size — model + colour, whatever's present.
function doorSub(door) {
  const id = door.identity || {};
  return [id.Model, id.Color].filter(Boolean).join(" · ");
}

// The spec grid for the expanded door: identity + build detail, merged in that
// order. Not shown here: Model/Color (in the header); Price (CHI's cost — kept
// in the domain data for the PO panel, hidden on the install view); Date Created
// (capture metadata, not a door spec).
const HIDDEN = ["Model", "Color", "Price", "Date Created"];
function doorSpecs(door) {
  const out = { ...(door.identity || {}), ...(door.installer || {}) };
  HIDDEN.forEach((k) => delete out[k]);
  return out;
}

function fmtVal(val) {
  if (val == null) return "—";
  if (Array.isArray(val)) return val.map(fmtVal).join("; ");
  if (typeof val === "object") {
    return Object.entries(val).map(([k, v]) => `${k}: ${v}`).join(", ");
  }
  return String(val);
}
</script>

<style scoped>
/* Themed via PrimeVue vars so it reads in light + dark. */
.door-spec-list { display: flex; flex-direction: column; gap: 0.4rem; }
.door-item {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem; overflow: hidden;
  background: var(--p-content-background, #fff);
}
.door-toggle {
  width: 100%; display: flex; align-items: baseline; gap: 0.5rem;
  padding: 0.6rem 0.75rem; min-height: 44px;
  background: transparent; border: 0; cursor: pointer; text-align: left;
  font: inherit; color: var(--p-text-color, #111827);
}
.door-toggle:hover { background: var(--p-content-hover-background, #f3f4f6); }
.door-chevron { font-size: 0.75rem; color: var(--p-text-muted-color, #9ca3af); align-self: center; }
.door-size { font-weight: 700; font-size: 1rem; }
.door-sub { color: var(--p-text-muted-color, #6b7280); font-size: 0.85rem; }
.door-qty { margin-left: auto; font-weight: 600; color: var(--p-text-muted-color, #6b7280); }
.door-body {
  padding: 0.25rem 0.85rem 0.75rem;
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}
.door-grid { margin: 0.5rem 0 0; display: grid; grid-template-columns: minmax(7rem, auto) 1fr; gap: 0.15rem 0.75rem; }
.door-row { display: contents; }
.door-row dt {
  font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.02em;
  color: var(--p-text-muted-color, #6b7280);
}
.door-row dd { margin: 0; font-size: 0.9rem; color: var(--p-text-color, #111827); word-break: break-word; }
.door-windows { margin-top: 0.6rem; }
.door-windows-head {
  font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
  color: var(--p-text-muted-color, #9ca3af); margin-bottom: 0.2rem;
}
.door-windows ul { margin: 0; padding-left: 1.1rem; }
.door-windows li { font-size: 0.85rem; color: var(--p-text-color, #111827); }
</style>

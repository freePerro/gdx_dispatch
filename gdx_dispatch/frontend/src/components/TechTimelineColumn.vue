<!--
  Sprint dispatch-timeline (2026-05-21) — vertical hour-axis layout per
  tech column. Top: "Unscheduled today" tray for date-only jobs. Below:
  hour ticks down the left, each job rendered as a positioned block
  whose top = start time and height = scheduled_duration_hours.

  Doug decisions (SCOPE.md):
    1. Date-only / no-time jobs go to the tray, NOT auto-9am.
    2. Drop math snaps to 15-min increments.
    3. Overflow places the block but draws the bottom overhang in red.

  This component owns: hour-axis math, overlap layout, overflow render,
  drop coordinate → snapped ISO conversion. It does NOT own: the PATCH
  to /api/jobs (parent does), the duration prompt (parent gate), or the
  tech-header capacity bar (still in DispatchView above the column body).
-->
<template>
  <div class="tech-timeline" :class="{ 'tech-timeline--off': isOffToday }">
    <div v-if="isOffToday" class="tech-timeline-off">Off today</div>

    <template v-else>
      <!-- Unscheduled tray -->
      <div
        class="tech-timeline-tray"
        :class="{ 'tech-timeline-tray--drag-over': trayDragOver }"
        :data-testid="`tech-timeline-tray-${tech.id}`"
        @dragover.prevent="trayDragOver = true"
        @dragleave="trayDragOver = false"
        @drop.prevent.stop="onDropTray"
      >
        <div class="tray-label">Unscheduled today</div>
        <div v-if="trayJobs.length" class="tray-chips">
          <div
            v-for="job in trayJobs"
            :key="job.id"
            class="tray-chip"
            draggable="true"
            :data-testid="`tray-job-${job.id}`"
            @dragstart="onJobDragStart(job, $event)"
            @dragend="emit('job-drag-end')"
            @click="$emit('open-drawer', job)"
          >
            <span class="tray-customer">{{ displayCustomer(job) }}</span>
            <span class="tray-dur">{{ formatDuration(job.effective_duration_hours) }}</span>
          </div>
        </div>
        <div v-else class="tray-empty">Drop here to keep without a time</div>
      </div>

      <!-- Timeline body — hour axis on the left, positioned blocks on the right -->
      <div
        class="tech-timeline-body"
        :style="{ height: bodyHeightPx + 'px' }"
        :class="{ 'tech-timeline-body--drag-over': bodyDragOver }"
        :data-testid="`tech-timeline-body-${tech.id}`"
        @dragover.prevent="onBodyDragOver"
        @dragleave="bodyDragOver = false"
        @drop.prevent.stop="onDropBody"
      >
        <!-- Hour grid (full-width lines + labels in the gutter) -->
        <div
          v-for="hour in hourTicks"
          :key="hour"
          class="hour-tick"
          :style="{ top: ((hour - shiftStartHours) * pxPerHour) + 'px' }"
        >
          <span class="hour-label">{{ formatHourLabel(hour) }}</span>
        </div>
        <!-- 15-min minor ticks (start after the gutter) -->
        <div
          v-for="m in minorTicks"
          :key="`m${m}`"
          class="minor-tick"
          :style="{ top: ((m - shiftStartHours) * pxPerHour) + 'px' }"
        />

        <!-- "Now" line, only when selectedDate is today -->
        <div
          v-if="showNowLine"
          class="now-line"
          :style="{ top: nowLineY + 'px' }"
          data-testid="now-line"
        />

        <!-- Block layer — sits right of the gutter; absolute-positioned
             blocks place themselves with leftPct/widthPct of this layer. -->
        <div class="block-layer">
          <div
            v-for="block in jobBlocks"
            :key="block.id"
            class="job-block"
            :class="{
              'job-block--overflow': block.overflowPx > 0,
              'job-block--overlap': block.overlapCount > 1,
            }"
            :style="{
              top: block.topPx + 'px',
              height: block.heightPx + 'px',
              left: block.leftPct + '%',
              width: block.widthPct + '%',
            }"
            :data-testid="`timeline-job-${block.id}`"
            draggable="true"
            @dragstart="onJobDragStart(block.job, $event)"
            @dragend="emit('job-drag-end')"
            @click="$emit('open-drawer', block.job)"
          >
            <div class="block-customer">{{ displayCustomer(block.job) }}</div>
            <div class="block-meta">
              {{ formatTime(block.startDate) }} · {{ formatDuration(block.durationHours) }}
            </div>
            <div v-if="block.overflowPx > 0" class="block-overflow">
              +{{ formatDuration(block.overflowHours) }} over
            </div>
          </div>
        </div>

        <div v-if="!jobBlocks.length && !trayJobs.length" class="empty-hint">
          Drop a job on the timeline
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount, ref } from 'vue';

const props = defineProps({
  tech: { type: Object, required: true },
  jobs: { type: Array, default: () => [] },
  selectedDate: { type: Date, required: true },
  pxPerHour: { type: Number, default: 48 },
});

const emit = defineEmits(['open-drawer', 'job-drag-start', 'job-drag-end', 'place', 'place-tray']);

const bodyDragOver = ref(false);
const trayDragOver = ref(false);
const nowTick = ref(Date.now());

// Re-render the "now" line every minute. Cheap setInterval; cleared on
// unmount so the timer doesn't leak when the dispatch view tears down.
let nowTimer = null;
onMounted(() => {
  nowTimer = setInterval(() => { nowTick.value = Date.now(); }, 60_000);
});
onBeforeUnmount(() => { if (nowTimer) clearInterval(nowTimer); });

function parseHHMM(s) {
  if (!s || typeof s !== 'string') return null;
  const m = /^(\d{1,2}):(\d{2})/.exec(s);
  if (!m) return null;
  return Number(m[1]) + Number(m[2]) / 60;
}

const shiftStartHours = computed(() => parseHHMM(props.tech.effective_shift_start) ?? 8);
const shiftEndHours = computed(() => parseHHMM(props.tech.effective_shift_end) ?? 17);
const shiftLengthHours = computed(() => Math.max(1, shiftEndHours.value - shiftStartHours.value));
const bodyHeightPx = computed(() => shiftLengthHours.value * props.pxPerHour);
const isOffToday = computed(() => Boolean(props.tech.isOffToday));

const hourTicks = computed(() => {
  const start = Math.floor(shiftStartHours.value);
  const end = Math.ceil(shiftEndHours.value);
  const out = [];
  for (let h = start; h <= end; h++) out.push(h);
  return out;
});

const minorTicks = computed(() => {
  const start = shiftStartHours.value;
  const end = shiftEndHours.value;
  const out = [];
  for (let q = Math.ceil(start * 4); q < end * 4; q++) {
    if (q % 4 === 0) continue;
    out.push(q / 4);
  }
  return out;
});

// Jobs without a real start time go to the tray. "Real" = scheduled_at
// has a non-midnight time component on the selected date.
function isDateOnly(job) {
  if (!job.scheduled_at) return true;
  const d = new Date(job.scheduled_at);
  if (Number.isNaN(d.getTime())) return true;
  return d.getHours() === 0 && d.getMinutes() === 0 && d.getSeconds() === 0;
}

const trayJobs = computed(() => (props.jobs || []).filter(isDateOnly));
const timelineJobs = computed(() => (props.jobs || []).filter((j) => !isDateOnly(j)));

// Compute positioned blocks with overlap-aware side-by-side layout.
const jobBlocks = computed(() => {
  const blocks = timelineJobs.value.map((job) => {
    const start = new Date(job.scheduled_at);
    const startFloat = start.getHours() + start.getMinutes() / 60;
    const duration = Math.max(0.25, Number(job.scheduled_duration_hours)
                                   || Number(job.effective_duration_hours)
                                   || 1);
    const endFloat = startFloat + duration;
    const topPx = (startFloat - shiftStartHours.value) * props.pxPerHour;
    const wantedHeightPx = duration * props.pxPerHour;
    const maxHeightPx = bodyHeightPx.value - topPx;
    const heightPx = Math.max(20, Math.min(wantedHeightPx, Math.max(20, maxHeightPx)));
    const overflowHours = Math.max(0, endFloat - shiftEndHours.value);
    return {
      id: job.id,
      job,
      startDate: start,
      startFloat,
      endFloat,
      durationHours: duration,
      topPx,
      heightPx,
      overflowHours,
      overflowPx: overflowHours * props.pxPerHour,
      _cluster: null,
      leftPct: 0,
      widthPct: 100,
      overlapCount: 1,
    };
  });

  // Cluster overlapping blocks (sweep line). Sort by start, walk: if the
  // current block's start < running cluster max-end, it joins the
  // cluster; else flush + start a new one.
  blocks.sort((a, b) => a.startFloat - b.startFloat || a.endFloat - b.endFloat);
  let cluster = [];
  let clusterMaxEnd = -Infinity;
  const clusters = [];
  for (const b of blocks) {
    if (b.startFloat < clusterMaxEnd - 0.001) {
      cluster.push(b);
      clusterMaxEnd = Math.max(clusterMaxEnd, b.endFloat);
    } else {
      if (cluster.length) clusters.push(cluster);
      cluster = [b];
      clusterMaxEnd = b.endFloat;
    }
  }
  if (cluster.length) clusters.push(cluster);

  for (const c of clusters) {
    const n = c.length;
    c.forEach((b, i) => {
      b.leftPct = (i / n) * 100;
      b.widthPct = 100 / n;
      b.overlapCount = n;
    });
  }
  return blocks;
});

// "Now" line only on today's date, and only when "now" is inside the
// tech's shift window (don't draw a line above or below the body).
const showNowLine = computed(() => {
  const sel = props.selectedDate;
  const now = new Date(nowTick.value);
  if (!sel) return false;
  if (sel.getFullYear() !== now.getFullYear()
      || sel.getMonth() !== now.getMonth()
      || sel.getDate() !== now.getDate()) return false;
  const f = now.getHours() + now.getMinutes() / 60;
  return f >= shiftStartHours.value && f <= shiftEndHours.value;
});

const nowLineY = computed(() => {
  const now = new Date(nowTick.value);
  const f = now.getHours() + now.getMinutes() / 60;
  return (f - shiftStartHours.value) * props.pxPerHour;
});

function formatHourLabel(h) {
  const hh = Math.floor(h);
  const period = hh >= 12 ? 'pm' : 'am';
  const display = hh % 12 === 0 ? 12 : hh % 12;
  return `${display}${period}`;
}
function formatTime(date) {
  if (!date) return '';
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}
function formatDuration(value) {
  if (value == null || !Number.isFinite(Number(value))) return '?h';
  const n = Number(value);
  return `${(Math.round(n * 100) / 100).toString().replace(/\.?0+$/, '')}h`;
}
function displayCustomer(job) {
  if (typeof job.customer === 'object') return job.customer?.name || job.title || 'Job';
  return job.customer_name || job.customer || job.title || 'Job';
}

// Drop coordinate → snapped ISO timestamp on selectedDate.
function dropYToISO(clientY, bodyEl) {
  const rect = bodyEl.getBoundingClientRect();
  const y = Math.max(0, clientY - rect.top);
  const hoursFromTop = y / props.pxPerHour;
  const timeFloat = shiftStartHours.value + hoursFromTop;
  // Snap to 15-min: round to nearest 0.25
  const snapped = Math.round(timeFloat * 4) / 4;
  const hours = Math.floor(snapped);
  const mins = Math.round((snapped - hours) * 60);
  const base = props.selectedDate;
  const d = new Date(base.getFullYear(), base.getMonth(), base.getDate(), hours, mins, 0, 0);
  return d.toISOString();
}

function onJobDragStart(job, event) {
  if (event?.dataTransfer) {
    event.dataTransfer.setData('text/plain', String(job.id));
    event.dataTransfer.effectAllowed = 'move';
  }
  emit('job-drag-start', job, event);
}

function onBodyDragOver(event) {
  bodyDragOver.value = true;
  if (event?.dataTransfer) event.dataTransfer.dropEffect = 'move';
}

function onDropBody(event) {
  bodyDragOver.value = false;
  const transferId = event?.dataTransfer?.getData('text/plain');
  if (!transferId) return;
  const startISO = dropYToISO(event.clientY, event.currentTarget);
  emit('place', { jobId: transferId, techId: String(props.tech.id), startISO });
}

function onDropTray(event) {
  trayDragOver.value = false;
  const transferId = event?.dataTransfer?.getData('text/plain');
  if (!transferId) return;
  emit('place-tray', { jobId: transferId, techId: String(props.tech.id) });
}

// Exported for vitest — pure math, no DOM coupling required.
defineExpose({ dropYToISO });
</script>

<style scoped>
.tech-timeline {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.tech-timeline--off { opacity: 0.55; }
.tech-timeline-off {
  padding: 0.75rem 1rem;
  text-align: center;
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
  border: 1px dashed var(--p-content-border-color, #d1d5db);
  border-radius: var(--p-border-radius, 6px);
}

.tech-timeline-tray {
  border: 1px dashed var(--p-content-border-color, #d1d5db);
  border-radius: var(--p-border-radius, 6px);
  padding: 0.4rem 0.5rem;
  background: var(--p-surface-100, #f3f4f6);
  font-size: 0.75rem;
}
.tech-timeline-tray--drag-over {
  border-color: var(--p-primary-color, #2563eb);
  background: var(--p-primary-100, #dbeafe);
}
.tray-label {
  font-size: 0.7rem;
  font-weight: 600;
  color: var(--p-text-muted-color, #6b7280);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 0.25rem;
}
.tray-chips { display: flex; flex-wrap: wrap; gap: 0.3rem; }
.tray-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.2rem 0.5rem;
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #d1d5db);
  border-radius: 999px;
  cursor: grab;
  font-size: 0.75rem;
  max-width: 100%;
}
.tray-chip:active { cursor: grabbing; }
.tray-customer { font-weight: 500; }
.tray-dur { color: var(--p-text-muted-color, #6b7280); font-size: 0.7rem; }
.tray-empty {
  font-size: 0.7rem;
  color: var(--p-text-muted-color, #9ca3af);
  font-style: italic;
}

.tech-timeline-body {
  position: relative;
  border: 1px solid var(--p-content-border-color, #d1d5db);
  border-radius: var(--p-border-radius, 6px);
  background: var(--p-content-background, #fff);
  overflow: visible;
}
.tech-timeline-body--drag-over {
  background: var(--p-primary-50, #eff6ff);
}

/* 40px gutter on the left holds the hour labels; the block layer fills
   the rest. Keep the constants in sync between CSS + drop math (the
   drop coordinate-to-time conversion uses the body rect including the
   gutter — dropping ON the gutter is still a valid time, just at the
   start of the hour). */
.hour-tick {
  position: absolute;
  left: 0;
  right: 0;
  height: 1px;
  background: var(--p-surface-300, #e5e7eb);
}
.hour-label {
  position: absolute;
  left: 4px;
  top: 2px;
  font-size: 0.65rem;
  color: var(--p-text-muted-color, #6b7280);
  background: var(--p-content-background, #fff);
  padding: 0 2px;
}
.minor-tick {
  position: absolute;
  left: 40px;
  right: 0;
  height: 1px;
  background: var(--p-surface-200, #f3f4f6);
}

.now-line {
  position: absolute;
  left: 40px;
  right: 0;
  height: 0;
  border-top: 1.5px dashed var(--p-red-500, #ef4444);
  z-index: 3;
  pointer-events: none;
}

.block-layer {
  position: absolute;
  left: 40px;
  right: 4px;
  top: 0;
  bottom: 0;
}

.job-block {
  position: absolute;
  min-height: 20px;
  padding: 0.25rem 0.4rem;
  border-radius: 4px;
  background: var(--p-primary-100, #dbeafe);
  border: 1px solid var(--p-primary-300, #93c5fd);
  font-size: 0.72rem;
  color: var(--p-text-color, #111827);
  cursor: grab;
  overflow: hidden;
  z-index: 2;
  transition: left 0.1s, width 0.1s;
  box-sizing: border-box;
}
.job-block:active { cursor: grabbing; }
.job-block--overlap {
  outline: 2px solid var(--p-red-500, #ef4444);
  outline-offset: -2px;
}
.job-block--overflow {
  border-bottom-color: var(--p-red-500, #ef4444);
  border-bottom-width: 2px;
}
.block-customer { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.block-meta { font-size: 0.65rem; color: var(--p-text-muted-color, #6b7280); }
.block-overflow {
  position: absolute;
  bottom: 2px;
  right: 4px;
  font-size: 0.65rem;
  color: var(--p-red-500, #ef4444);
  font-weight: 600;
}
.empty-hint {
  position: absolute;
  left: 40px;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  text-align: center;
  font-size: 0.75rem;
  color: var(--p-text-muted-color, #9ca3af);
  font-style: italic;
}

</style>

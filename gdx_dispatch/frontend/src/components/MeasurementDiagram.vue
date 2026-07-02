<template>
  <!-- A labeled garage-opening diagram with the measurement inputs placed on the
       dimension arrows. Opt-in from a plugin create form via `diagram:"garage_opening"`;
       the named fields (opening_w / opening_h / ceiling) are bound to the parent's
       form state so the plugin still POSTs them normally. -->
  <div class="measure-diagram">
    <svg viewBox="0 0 560 360" class="measure-diagram__svg" role="img"
         aria-label="Garage door opening measurement diagram">
      <defs>
        <marker id="md-arw" markerWidth="9" markerHeight="9" refX="4.5" refY="4.5" orient="auto">
          <path d="M1,1 L8,4.5 L1,8" fill="none" class="md-arrow" stroke-width="1.4" />
        </marker>
      </defs>
      <!-- garage wall face -->
      <rect x="120" y="34" width="410" height="276" rx="4" class="md-wall" />
      <!-- ceiling (dashed) + floor -->
      <line x1="120" y1="60" x2="530" y2="60" class="md-line" stroke-dasharray="5 4" />
      <text x="128" y="54" class="md-note">ceiling</text>
      <line x1="120" y1="300" x2="530" y2="300" class="md-floor" stroke-width="2" />
      <text x="128" y="316" class="md-note">floor</text>
      <!-- door opening -->
      <rect x="240" y="140" width="210" height="160" class="md-opening" stroke-width="2" />
      <text x="345" y="228" text-anchor="middle" class="md-op">door opening</text>
      <text x="345" y="122" text-anchor="middle" class="md-note">head&shy;room</text>
      <!-- dimension arrows -->
      <!-- (A) opening width, along the bottom of the opening -->
      <line x1="240" y1="326" x2="450" y2="326" class="md-arrow" marker-start="url(#md-arw)" marker-end="url(#md-arw)" />
      <!-- (B) opening height, right of the opening -->
      <line x1="478" y1="140" x2="478" y2="300" class="md-arrow" marker-start="url(#md-arw)" marker-end="url(#md-arw)" />
      <!-- (C) floor-to-ceiling, far left -->
      <line x1="92" y1="60" x2="92" y2="300" class="md-arrow" marker-start="url(#md-arw)" marker-end="url(#md-arw)" />
      <line x1="92" y1="60" x2="120" y2="60" class="md-line" />
    </svg>

    <!-- inputs placed over the arrows (percentages of the 560x360 box) -->
    <div v-for="m in spots" :key="m.name" class="measure-diagram__spot"
         :style="{ left: m.left, top: m.top }">
      <label :for="`md-${m.name}`">{{ m.label }}</label>
      <InputNumber :inputId="`md-${m.name}`" v-model="state[m.name]" :min="1"
                   suffix=" in" :useGrouping="false" size="small" class="measure-diagram__in" />
    </div>
  </div>
</template>

<script setup>
import InputNumber from 'primevue/inputnumber';

// `state` is the parent's reactive form object; binding v-model to state[name]
// mutates it in place so the create() call sends these fields unchanged.
const props = defineProps({ state: { type: Object, required: true } });

const spots = [
  { name: 'opening_w', label: 'Opening width', left: '38%', top: '90%' },
  { name: 'opening_h', label: 'Opening height', left: '83%', top: '58%' },
  { name: 'ceiling', label: 'Floor→ceiling', left: '1%', top: '46%' },
];
const state = props.state;
</script>

<style scoped>
.measure-diagram {
  position: relative;
  max-width: 560px;
  margin: 0.25rem 0 0.75rem;
}
.measure-diagram__svg { width: 100%; height: auto; display: block; }
/* Theme-driven colors: the diagram sits partly on the page background, so every
   fill/stroke follows the app tokens rather than assuming a light page. */
.md-wall { fill: var(--surface-elevated); stroke: var(--border-strong); }
.md-line { stroke: var(--border-strong); }
.md-floor { stroke: var(--text-muted); }
.md-opening { fill: var(--interactive-primary-soft); stroke: var(--interactive-primary); }
.md-arrow { stroke: var(--text-secondary); }
.md-note { font: 11px sans-serif; fill: var(--text-muted); }
.md-op { font: 12px sans-serif; fill: var(--interactive-primary); }
.measure-diagram__spot {
  position: absolute;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}
.measure-diagram__spot label { font-size: 11px; color: var(--text-secondary); white-space: nowrap; }
.measure-diagram__in :deep(input) { width: 5.5rem; text-align: center; }
</style>

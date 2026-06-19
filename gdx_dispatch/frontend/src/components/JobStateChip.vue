<script setup>
/**
 * JobStateChip — the ONE component every surface uses to render a job's
 * state (Slice 4 Wave 0b). Reads the authoritative `display_state` via
 * the shared `jobDisplayState` util — no surface re-derives from
 * `job.status` / `job.lifecycle_stage` anymore.
 *
 * Pass the whole job object. Fallback is handled in the util, so this
 * component is dumb on purpose: label + severity + optional icon.
 */
import { computed } from 'vue'
import Tag from 'primevue/tag'
import { jobDisplayState } from '../utils/jobDisplayState'

const props = defineProps({
  job: { type: Object, default: null },
  // Hide the leading status icon (some dense tables don't want it).
  showIcon: { type: Boolean, default: true },
})

const state = computed(() => jobDisplayState(props.job))
</script>

<template>
  <Tag
    :value="state.label"
    :severity="state.severity"
    :icon="showIcon && state.icon ? state.icon : undefined"
    :data-stage="state.stage"
    :data-type="state.type"
    :data-unverified="state.unverified ? 'true' : undefined"
    :title="state.unverified ? 'State not yet confirmed against billing — refresh to sync' : undefined"
    class="job-state-chip"
    :class="{ 'job-state-chip--unverified': state.unverified }"
  />
</template>

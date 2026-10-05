<!--
  Pick when a text should go: the next 8 or 9 AM, or any date and time.
  Used beside Send in the SMS views and in SmsLinkDialog
  (server: modules/phone_com/scheduled.py). Emits `schedule` with a Date; the
  caller posts it. Times are the browser's own — the server stores UTC.
-->
<template>
  <div class="send-later" data-testid="send-later">
    <div class="send-later-presets">
      <Button
        v-for="p in quick"
        :key="p.label"
        :label="p.label"
        size="small"
        :outlined="!isChosen(p.at)"
        :severity="isChosen(p.at) ? undefined : 'secondary'"
        data-testid="send-later-preset"
        @click="chosen = p.at"
      />
    </div>
    <!-- showOnFocus=false for the reason TimeEntryDialog records: inside a
         dialog a showTime panel that opens on focus covers the buttons below,
         and Escape closes the whole dialog. Type it, or open from the icon. -->
    <label class="send-later-label" :for="pickerId">Or pick a date and time</label>
    <DatePicker
      v-model="chosen"
      :input-id="pickerId"
      showTime
      showIcon
      :showOnFocus="false"
      hourFormat="12"
      :minDate="minDate"
      :maxDate="maxDate"
      :stepMinute="5"
      class="send-later-picker"
      data-testid="send-later-picker"
    />
    <small v-if="hint" class="send-later-hint" data-testid="send-later-hint">{{ hint }}</small>
    <div class="send-later-actions">
      <Button label="Back" text size="small" data-testid="send-later-back" @click="emit('cancel')" />
      <Button
        :label="chosen ? `Schedule · ${whenLabel(chosen)}` : 'Pick a time'"
        icon="pi pi-clock"
        size="small"
        :disabled="!chosen || busy"
        :loading="busy"
        data-testid="send-later-confirm"
        @click="emit('schedule', chosen)"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import DatePicker from 'primevue/datepicker'
import { presets, whenLabel } from '../utils/sendLater'

defineProps({
  busy: { type: Boolean, default: false },
  // Shown under the picker, e.g. "The balance is re-read when it sends."
  hint: { type: String, default: '' },
})
const emit = defineEmits(['schedule', 'cancel'])

const pickerId = `send-later-${Math.random().toString(36).slice(2, 8)}`
const quick = presets()
const chosen = ref(quick[0].at)
const minDate = new Date()
const maxDate = computed(() => {
  const d = new Date()
  d.setDate(d.getDate() + 30)
  return d
})

function isChosen(at) {
  return chosen.value instanceof Date && chosen.value.getTime() === at.getTime()
}
</script>

<style scoped>
.send-later {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.75rem;
  border: 1px dashed var(--p-content-border-color);
  border-radius: 8px;
}
.send-later-presets {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}
.send-later-label {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
.send-later-picker {
  width: 100%;
}
.send-later-hint {
  color: var(--p-text-muted-color);
}
.send-later-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.4rem;
  flex-wrap: wrap;
}
</style>

<!--
  Texts waiting to send — and the last week's that did not go — with Cancel
  on the ones still waiting. Shown at the foot of an SMS thread and inside the
  invoice / estimate text dialog (server: modules/phone_com/scheduled.py). Sent
  texts are not listed: they appear in the thread as real messages.
-->
<template>
  <ul v-if="items.length" class="sched-list" data-testid="scheduled-list">
    <li
      v-for="item in items"
      :key="item.id"
      :class="['sched-item', `sched-${item.status}`]"
      data-testid="scheduled-item"
    >
      <div class="sched-head">
        <i :class="['pi', item.status === 'scheduled' || item.status === 'sending' ? 'pi-clock' : 'pi-exclamation-triangle']" />
        <strong class="sched-when">{{ scheduledStatusLabel(item) }}</strong>
        <Button
          v-if="item.status === 'scheduled'"
          label="Cancel"
          text
          size="small"
          severity="danger"
          :loading="canceling === item.id"
          data-testid="scheduled-cancel"
          @click="cancel(item)"
        />
      </div>
      <div class="sched-body">{{ item.body || defaultText }}</div>
      <div class="sched-meta">
        <span v-if="item.status !== 'scheduled' && item.status !== 'sending'">
          Was due {{ formatDateTime(item.send_at) }}.
        </span>
        <span v-if="item.error_message"> {{ item.error_message }}</span>
        <span v-if="item.created_by_name"> Scheduled by {{ item.created_by_name }}.</span>
      </div>
    </li>
  </ul>
</template>

<script setup>
import { ref } from 'vue'
import Button from 'primevue/button'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { formatDateTime } from '../composables/useFormatters'
import { scheduledStatusLabel } from '../utils/sendLater'

defineProps({
  items: { type: Array, default: () => [] },
  // What a row with no stored body sends: the document's default wording,
  // rebuilt when it goes.
  defaultText: { type: String, default: 'The standard text, rebuilt with current details when it sends.' },
})
const emit = defineEmits(['changed'])

const api = useApi()
const toast = useToast()
const canceling = ref(null)

async function cancel(item) {
  canceling.value = item.id
  try {
    await api.post(`/api/phone-com/scheduled/${item.id}/cancel`, {}, { suppressErrorToast: true })
    toast.add({ severity: 'info', summary: 'Scheduled text canceled', life: 4000 })
  } catch (err) {
    toast.add({ severity: 'warn', summary: 'Not canceled', detail: err?.message || '', life: 6000 })
  } finally {
    canceling.value = null
    emit('changed')
  }
}
</script>

<style scoped>
.sched-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.sched-item {
  border: 1px dashed var(--interactive-primary, #2563eb);
  border-radius: 8px;
  padding: 0.5rem 0.75rem;
  background: var(--p-content-background);
}
.sched-skipped,
.sched-failed,
.sched-unknown {
  border-color: var(--p-orange-400, #fb923c);
}
.sched-head {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.sched-when {
  flex: 1;
  font-size: 0.9rem;
}
.sched-body {
  white-space: pre-wrap;
  margin: 0.25rem 0;
}
.sched-meta {
  font-size: 0.75rem;
  color: var(--p-text-muted-color);
}
</style>

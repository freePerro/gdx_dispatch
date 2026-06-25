<script setup>
// Sprint tech_mobile Phase 4.1 — Per-job chat panel.
//
// Flat thread, quick-action chips, REST polling on a 5s cadence while
// the dialog is open (lightweight v1 — WebSocket upgrade reserved).
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { isTechnician } from '../constants/roles'

const props = defineProps({
  visible: { type: Boolean, default: false },
  job: { type: Object, default: null },
})
const emit = defineEmits(['update:visible'])

const api = useApi()
const toast = useToast()

const open = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

const messages = ref([])
const quickActions = ref({})
const loading = ref(false)
const sending = ref(false)
const draft = ref('')
const lastFetchedAt = ref(null)
const scrollEl = ref(null)
let pollTimer = null

async function fetchMessages(initial = false) {
  if (!props.job?.id) return
  if (loading.value) return
  loading.value = true
  try {
    const url = initial || !lastFetchedAt.value
      ? `/api/mobile/jobs/${props.job.id}/chat`
      : `/api/mobile/jobs/${props.job.id}/chat?since=${encodeURIComponent(lastFetchedAt.value)}`
    const data = await api.get(url)
    if (initial) {
      messages.value = data.messages || []
    } else {
      // Merge new messages, dedup by id
      const ids = new Set(messages.value.map(m => m.id))
      for (const m of data.messages || []) {
        if (!ids.has(m.id)) messages.value.push(m)
      }
    }
    quickActions.value = data.quick_actions || {}
    if (messages.value.length) {
      lastFetchedAt.value = messages.value[messages.value.length - 1].created_at
    } else {
      lastFetchedAt.value = new Date().toISOString()
    }
    nextTick(scrollToBottom)
  } catch (e) {
    if (initial) toast.add({ severity: 'error', summary: 'Could not load chat', detail: e.message, life: 4000 })
  } finally {
    loading.value = false
  }
}

function scrollToBottom() {
  if (scrollEl.value) {
    scrollEl.value.scrollTop = scrollEl.value.scrollHeight
  }
}

async function sendText() {
  const body = draft.value.trim()
  if (!body || !props.job?.id) return
  sending.value = true
  try {
    const msg = await api.post(`/api/mobile/jobs/${props.job.id}/chat`, {
      kind: 'text', body,
    })
    messages.value.push(msg)
    draft.value = ''
    nextTick(scrollToBottom)
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Send failed', detail: e.message, life: 4000 })
  } finally {
    sending.value = false
  }
}

async function sendQuick(slug) {
  if (!props.job?.id) return
  sending.value = true
  try {
    const msg = await api.post(`/api/mobile/jobs/${props.job.id}/chat`, {
      kind: 'quick_action', quick_action: slug,
    })
    messages.value.push(msg)
    nextTick(scrollToBottom)
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Send failed', detail: e.message, life: 4000 })
  } finally {
    sending.value = false
  }
}

function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(() => fetchMessages(false), 5000)
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

watch(() => props.visible, (v) => {
  if (v) {
    messages.value = []
    lastFetchedAt.value = null
    fetchMessages(true)
    startPolling()
  } else {
    stopPolling()
  }
})

onUnmounted(stopPolling)

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
function isMine(m) {
  // Best-effort: tech messages render right-aligned. For dispatchers we
  // lean on sender_role; for techs we mark our own outgoing role.
  return isTechnician(m.sender_role)
}
</script>

<template>
  <Dialog
    v-model:visible="open"
    :header="`Dispatch chat${job?.title ? ' — ' + job.title : ''}`"
    modal
    :style="{ width: '96vw', maxWidth: '500px' }"
  >
    <div ref="scrollEl" class="chat-thread">
      <div v-if="loading && messages.length === 0" class="chat-empty muted">
        Loading…
      </div>
      <div v-else-if="messages.length === 0" class="chat-empty muted">
        No messages yet. Tap a quick-action below or type to start.
      </div>
      <div
        v-for="m in messages"
        :key="m.id"
        class="chat-msg"
        :class="{ 'is-mine': isMine(m), 'is-quick': m.kind === 'quick_action' }"
      >
        <div class="chat-msg-meta">
          <strong>{{ m.sender_name || m.sender_role }}</strong>
          <span class="muted">{{ fmtTime(m.created_at) }}</span>
        </div>
        <div class="chat-msg-body">{{ m.body }}</div>
        <Tag v-if="m.kind === 'quick_action'" :value="m.quick_action" severity="info" />
      </div>
    </div>

    <div class="quick-row">
      <Button
        v-for="(label, slug) in quickActions"
        :key="slug"
        :label="label"
        size="small"
        outlined
        :loading="sending"
        @click="sendQuick(slug)"
      />
    </div>

    <template #footer>
      <div class="compose-row">
        <InputText
          v-model="draft"
          placeholder="Message dispatch…"
          class="compose-input"
          @keyup.enter="sendText"
        />
        <Button
          icon="pi pi-send"
          :loading="sending"
          :disabled="!draft.trim()"
          @click="sendText"
        />
      </div>
    </template>
  </Dialog>
</template>

<style scoped>
.chat-thread {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  max-height: 50vh;
  min-height: 200px;
  overflow-y: auto;
  padding: 0.4rem;
  background: var(--p-highlight-background, #f9fafb);
  border-radius: 0.5rem;
  margin-bottom: 0.6rem;
}
.chat-empty { padding: 1rem; text-align: center; }

.chat-msg {
  max-width: 80%;
  padding: 0.5rem 0.75rem;
  border-radius: 0.6rem;
  background: white;
  border: 1px solid var(--p-content-border-color);
}
.chat-msg.is-mine { align-self: flex-end; background: #eff6ff; border-color: #bfdbfe; }
.chat-msg.is-quick { background: #fef3c7; border-color: #fde68a; }
.chat-msg-meta { display: flex; justify-content: space-between; gap: 0.5rem; font-size: 0.7rem; margin-bottom: 0.15rem; }
.chat-msg-body { font-size: 0.9rem; line-height: 1.3; }

.quick-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.4rem;
}

.compose-row { display: flex; gap: 0.4rem; width: 100%; }
.compose-input { flex: 1; }

.muted { color: var(--p-text-muted-color, #6b7280); }
</style>

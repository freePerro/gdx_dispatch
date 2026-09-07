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
  // Stamp read receipts for the tech messages shown here. Only the dispatch
  // board sets it, and only because the server already accepted that caller as
  // a dispatcher when it served the thread list — a better signal than
  // re-deriving the role here, which would duplicate the backend's own
  // (non-aliasing) role check and get out of step with it.
  markRead: { type: Boolean, default: false },
})
const emit = defineEmits(['update:visible', 'read'])

const api = useApi()
const toast = useToast()

// Message ids we have already POSTed, so the 5s poll doesn't re-stamp them.
const stamped = new Set()

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
    stampReadReceipts()
  } catch (e) {
    if (initial) toast.add({ severity: 'error', summary: 'Could not load chat', detail: e.message, life: 4000 })
  } finally {
    loading.value = false
  }
}

/**
 * Stamp read receipts for the tech messages this dispatcher is now looking at.
 *
 * `GET /api/mobile/dispatch/threads` counts a thread's unread as the tech-sent
 * messages with `read_at IS NULL`, and `MobileDispatchView` renders that as the
 * "N new" badge and sorts unread-first. The only writer of `read_at` is
 * `POST /api/mobile/chat/{id}/read` — and nothing called it, so once a tech
 * wrote to a thread its badge stayed lit forever (#641).
 *
 * Failures are deliberately silent: a read receipt is not worth a toast, and
 * dropping the id from `stamped` lets the next poll retry it.
 */
async function stampReadReceipts() {
  if (!props.markRead) return
  // The 5s poll also lands here. Only stamp while someone is actually looking:
  // a dialog left open in a background tab must not mark a tech's new message
  // read on their behalf.
  if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
  const pending = messages.value.filter(
    (m) => m.id && !m.read_at && isTechnician(m.sender_role) && !stamped.has(m.id),
  )
  if (!pending.length) return
  const results = await Promise.allSettled(
    pending.map(async (m) => {
      stamped.add(m.id)
      try {
        const updated = await api.post(
          `/api/mobile/chat/${m.id}/read`,
          {},
          { suppressErrorToast: true },
        )
        m.read_at = updated?.read_at || new Date().toISOString()
      } catch (e) {
        stamped.delete(m.id)
        throw e
      }
    }),
  )
  // Only tell the parent when something actually changed server-side — an
  // all-failed round must not trigger a thread-list refetch every 5s.
  if (results.some((r) => r.status === 'fulfilled')) emit('read')
}

function scrollToBottom() {
  if (scrollEl.value) {
    scrollEl.value.scrollTop = scrollEl.value.scrollHeight
  }
}

// 2026-07-01 UX audit: chat sends are offline-queued. A queued send isn't
// pushed into the thread (there's no server message row yet) — it appears
// via the normal poll after the queue drains on reconnect.
async function sendText() {
  const body = draft.value.trim()
  if (!body || !props.job?.id) return
  sending.value = true
  try {
    const msg = await api.postQueued(`/api/mobile/jobs/${props.job.id}/chat`, {
      kind: 'text', body,
    }, { actionType: 'job.chat', resourceId: String(props.job.id) })
    if (msg?.queued) {
      toast.add({ severity: 'warn', summary: 'Queued', detail: 'No signal — the message sends when you reconnect.', life: 3500 })
    } else {
      messages.value.push(msg)
    }
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
    const msg = await api.postQueued(`/api/mobile/jobs/${props.job.id}/chat`, {
      kind: 'quick_action', quick_action: slug,
    }, { actionType: 'job.chat', resourceId: String(props.job.id) })
    if (msg?.queued) {
      toast.add({ severity: 'warn', summary: 'Queued', detail: 'No signal — the message sends when you reconnect.', life: 3500 })
    } else {
      messages.value.push(msg)
    }
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
    // The board reuses one dialog instance across every thread of a shift;
    // without this the set grows all day and carries ids across jobs.
    stamped.clear()
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
          v-tooltip="'Send'"
          aria-label="Send"
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
  background: var(--p-content-background, var(--surface-panel));
  border: 1px solid var(--p-content-border-color);
}
.chat-msg.is-mine { align-self: flex-end; background: var(--interactive-primary-soft); border-color: var(--border-strong); }
.chat-msg.is-quick { background: var(--color-warning-bg); border-color: var(--color-warning-border); }
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

<template>
    <section class="mobile-sms">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>SMS</h1>
          <Button
            v-tooltip="'Refresh'"
            icon="pi pi-refresh"
            aria-label="Refresh"
            text
            size="small"
            :loading="loading"
            @click="fetchThreads"
            data-test="ms-refresh"
          />
        </div>
      </header>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading && !threads.length" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading conversations…</span>
      </div>
      <div v-else-if="!threads.length" class="state-msg">
        <i class="pi pi-comment empty-icon" />
        <div class="empty-title">No conversations yet</div>
        <div class="empty-help">Texts to the business line appear here within about 10 minutes.</div>
      </div>

      <ol v-else class="card-list">
        <li
          v-for="t in threads"
          :key="t.thread_key"
          class="thread-card"
          :class="{ unread: t.unread_count > 0 }"
          @click="openThread(t)"
          data-test="ms-thread-row"
        >
          <div class="thread-row">
            <span class="thread-who">{{ t.customer_name || t.other_party_number || '—' }}</span>
            <span
              v-if="t.unread_count > 0"
              class="unread-badge"
              data-test="ms-thread-unread"
            >{{ t.unread_count }}</span>
            <span class="thread-when">{{ fmtAgo(t.last_message_at) }}</span>
          </div>
          <div class="thread-preview">
            <i :class="['pi', t.last_message_direction === 'in' ? 'pi-arrow-down-left' : 'pi-arrow-up-right']" />
            {{ t.last_message_body || '(no body)' }}
          </div>
        </li>
      </ol>

      <!-- Conversation: full-screen bottom sheet, matching MobileInboxView -->
      <Dialog
        v-model:visible="threadOpen"
        :header="selectedThread ? (selectedThread.customer_name || selectedThread.other_party_number) : 'Conversation'"
        modal
        :style="{ width: '100vw', height: '100dvh' }"
        :breakpoints="{ '768px': '100vw' }"
        position="bottom"
        @hide="closeThread"
      >
        <div v-if="threadLoading" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
        </div>
        <div v-else class="convo-body">
          <div class="bubbles" ref="bubblesEl">
            <div
              v-for="m in threadMessages"
              :key="m.id"
              :class="['msg-bubble', m.direction === 'in' ? 'msg-in' : 'msg-out']"
              data-test="ms-bubble"
            >
              <div v-if="m.body" class="msg-body">{{ m.body }}</div>
              <div v-if="m.attachments && m.attachments.length" class="msg-media">
                <a
                  v-for="(att, i) in m.attachments"
                  :key="i"
                  :href="mediaBlob(m.id, i) || undefined"
                  target="_blank"
                  rel="noopener"
                >
                  <img
                    v-if="mediaBlob(m.id, i)"
                    :src="mediaBlob(m.id, i)"
                    class="msg-media-img"
                    alt="MMS attachment"
                  />
                  <span v-else class="media-fallback">📎 attachment</span>
                </a>
              </div>
              <div class="msg-meta">
                {{ formatDateTime(m.sent_at) }}
                <span v-if="m.delivery_status"> · {{ m.delivery_status }}</span>
              </div>
            </div>
          </div>

          <div class="compose-row">
            <Textarea
              v-model="composeBody"
              rows="2"
              auto-resize
              placeholder="Type a reply…"
              class="compose-input"
              data-test="ms-compose-body"
            />
            <Button
              icon="pi pi-send"
              aria-label="Send"
              :disabled="!composeBody.trim() || sending"
              :loading="sending"
              @click="sendReply"
              data-test="ms-compose-send"
            />
          </div>
          <div
            v-if="composeStatus"
            :class="['status-line', composeStatus.ok ? 'status-ok' : 'error-banner']"
          >
            {{ composeStatus.message }}
          </div>
        </div>
      </Dialog>
    </section>
</template>

<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Textarea from 'primevue/textarea'
import { useApi } from '../composables/useApi'
import { formatDateTime } from '../composables/useFormatters'
import { useSmsUnreadStore } from '../stores/smsUnread'

const api = useApi()
const smsUnread = useSmsUnreadStore()

const threads = ref([])
const loading = ref(false)
const error = ref(null)

const threadOpen = ref(false)
const selectedThread = ref(null)
const threadMessages = ref([])
const threadLoading = ref(false)
const bubblesEl = ref(null)

const composeBody = ref('')
const composeStatus = ref(null)
const sending = ref(false)

// MMS attachments arrive as authed API urls — <img src> can't carry the
// Bearer header, so fetch each as a blob (same pattern as the desktop view).
const mediaBlobs = ref({})

function mediaKey(id, idx) {
  return `${id}:${idx}`
}
function mediaBlob(id, idx) {
  return mediaBlobs.value[mediaKey(id, idx)] || null
}

function _authHeaders() {
  const tok = sessionStorage.getItem('gdx_access_token')
    || localStorage.getItem('gdx_access_token')
    || localStorage.getItem('auth_token')
    || ''
  return tok ? { Authorization: `Bearer ${tok}` } : {}
}

async function _loadThreadMedia(messages) {
  for (const m of messages) {
    const atts = m.attachments || []
    for (let i = 0; i < atts.length; i += 1) {
      const key = mediaKey(m.id, i)
      if (mediaBlobs.value[key]) continue
      try {
        const r = await fetch(`/api/phone-com/messages/${m.id}/media/${i}`, {
          headers: _authHeaders(),
        })
        if (!r.ok) continue
        mediaBlobs.value[key] = URL.createObjectURL(await r.blob())
      } catch { /* leave the 📎 fallback */ }
    }
  }
}

function _revokeMedia() {
  for (const url of Object.values(mediaBlobs.value)) {
    if (url && url.startsWith('blob:')) URL.revokeObjectURL(url)
  }
  mediaBlobs.value = {}
}

function fmtAgo(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const mins = Math.floor((Date.now() - d.getTime()) / 60_000)
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h`
  const days = Math.floor(hrs / 24)
  if (days < 7) return `${days}d`
  return d.toLocaleDateString()
}

const fetchThreads = async () => {
  loading.value = true
  error.value = null
  try {
    const r = await api.get('/api/phone-com/messages/threads?per_page=100')
    threads.value = r.items
  } catch (err) {
    error.value = err.message || 'Failed to load conversations'
  } finally {
    loading.value = false
  }
}

const _scrollToLatest = async () => {
  await nextTick()
  const el = bubblesEl.value
  if (el) el.scrollTop = el.scrollHeight
}

const openThread = async (thread) => {
  selectedThread.value = thread
  threadMessages.value = []
  _revokeMedia()
  threadOpen.value = true
  threadLoading.value = true
  try {
    const r = await api.get(
      `/api/phone-com/messages/threads/${encodeURIComponent(thread.thread_key)}?per_page=500`,
    )
    threadMessages.value = r.items
    _loadThreadMedia(r.items) // fire-and-forget; bubbles fill in as blobs resolve
    // The server stamps the whole thread read on open — reflect it locally
    // so the row badge drops without a refetch.
    if (thread.unread_count) thread.unread_count = 0
    smsUnread.fetchCount()
    _scrollToLatest()
  } catch (err) {
    error.value = err.message || 'Failed to load conversation'
    threadOpen.value = false
  } finally {
    threadLoading.value = false
  }
}

const sendReply = async () => {
  if (!selectedThread.value || !composeBody.value.trim()) return
  sending.value = true
  composeStatus.value = null
  try {
    const r = await api.post('/api/phone-com/messages', {
      to: selectedThread.value.other_party_number,
      body: composeBody.value.trim(),
      customer_id: selectedThread.value.customer_id || undefined,
    })
    composeStatus.value = { ok: true, message: `Sent · ${r.delivery_status || 'queued'}` }
    composeBody.value = ''
    // Reload the conversation so the sent bubble appears.
    const rr = await api.get(
      `/api/phone-com/messages/threads/${encodeURIComponent(selectedThread.value.thread_key)}?per_page=500`,
    )
    threadMessages.value = rr.items
    _scrollToLatest()
  } catch (err) {
    composeStatus.value = { ok: false, message: err.message || 'Send failed' }
  } finally {
    sending.value = false
  }
}

const closeThread = () => {
  selectedThread.value = null
  threadMessages.value = []
  composeBody.value = ''
  composeStatus.value = null
  _revokeMedia()
}

// Silent 60s refresh, matching the desktop view and the badge poll cadence.
const _refreshThreadsSilently = async () => {
  try {
    const r = await api.get('/api/phone-com/messages/threads?per_page=100')
    threads.value = r.items
  } catch { /* background refresh is best-effort */ }
}
let _threadsTimer = null
onMounted(() => {
  fetchThreads()
  _threadsTimer = setInterval(() => {
    if (!loading.value) _refreshThreadsSilently()
  }, 60000)
})
onUnmounted(() => {
  if (_threadsTimer) clearInterval(_threadsTimer)
  _revokeMedia()
})
</script>

<style scoped>
.mobile-sms {
  padding: var(--space-3);
}

.mobile-page-head {
  margin-bottom: var(--space-3);
}
.head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.mobile-page-head h1 {
  margin: 0;
  font-size: 1.35rem;
}

.state-msg {
  display: grid;
  place-items: center;
  gap: var(--space-2);
  padding: var(--space-6) var(--space-3);
  color: var(--text-muted);
  text-align: center;
}
.empty-icon {
  font-size: 2rem;
}
.empty-title {
  font-weight: 600;
  color: var(--text-primary);
}
.empty-help {
  font-size: 0.85rem;
}

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: var(--space-2);
}
.thread-card {
  background: var(--surface-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 0.625rem;
  padding: var(--space-3);
  cursor: pointer;
}
.thread-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}
.thread-who {
  flex: 1 1 auto;
  font-weight: 500;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.thread-card.unread .thread-who {
  font-weight: 700;
}
.unread-badge {
  flex: 0 0 auto;
  background: var(--interactive-primary);
  color: #fff;
  border-radius: 999px;
  min-width: 1.25rem;
  padding: 0.05rem 0.4rem;
  font-size: 0.7rem;
  font-weight: 700;
  text-align: center;
}
.thread-when {
  flex: 0 0 auto;
  font-size: 0.75rem;
  color: var(--text-muted);
}
.thread-preview {
  margin-top: 0.25rem;
  font-size: 0.85rem;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.thread-card.unread .thread-preview {
  color: var(--text-primary);
}

.convo-body {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: var(--space-2);
}
.bubbles {
  flex: 1 1 auto;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-bottom: var(--space-2);
}
.msg-bubble {
  max-width: 85%;
  padding: var(--space-2) var(--space-3);
  border-radius: 0.875rem;
  font-size: 0.9rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.msg-in {
  align-self: flex-start;
  background: var(--surface-elevated);
  border: 1px solid var(--border-subtle);
}
.msg-out {
  align-self: flex-end;
  background: var(--interactive-primary);
  color: #fff;
}
.msg-meta {
  margin-top: 0.25rem;
  font-size: 0.7rem;
  opacity: 0.75;
}
.msg-media-img {
  max-width: 100%;
  border-radius: 0.5rem;
  display: block;
  margin-top: 0.25rem;
}
.media-fallback {
  font-size: 0.85rem;
}

.compose-row {
  display: flex;
  align-items: flex-end;
  gap: var(--space-2);
}
.compose-input {
  flex: 1 1 auto;
  min-width: 0;
}

.error-banner {
  background: var(--p-red-50, #fef2f2);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid var(--p-red-200, #fecaca);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  margin-bottom: 0.5rem;
  font-size: 0.85rem;
}
.status-line {
  font-size: 0.85rem;
}
.status-ok {
  color: var(--p-green-600, #16a34a);
}
</style>

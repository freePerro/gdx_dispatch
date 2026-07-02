<template>
    <section class="phone-com-messages view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Phone.com — Messages</h1>
        </template>
        <template #end>
          <Button label="Refresh" icon="pi pi-refresh" severity="secondary" @click="fetchThreads" />
        </template>
      </Toolbar>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div class="msgs-layout">
        <div class="thread-list">
          <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>
          <div v-else-if="threads.length === 0" class="empty-message">
            No conversations yet.
          </div>
          <Card
            v-for="t in threads"
            :key="t.thread_key"
            :class="['thread-card', { active: selectedThread?.thread_key === t.thread_key }]"
            @click="openThread(t)"
            data-test="pc-thread-row"
          >
            <template #content>
              <div class="thread-row-top">
                <span class="thread-name">{{ t.customer_name || t.other_party_number || '—' }}</span>
                <span class="thread-when text-muted">{{ formatDateTime(t.last_message_at) }}</span>
              </div>
              <div class="thread-preview text-muted">
                <i :class="['pi', t.last_message_direction === 'in' ? 'pi-arrow-down-left' : 'pi-arrow-up-right']" />
                {{ t.last_message_body || '(no body)' }}
              </div>
            </template>
          </Card>
        </div>

        <div class="thread-pane" v-if="selectedThread">
          <Card>
            <template #title>
              <div class="pane-header">
                <span>{{ selectedThread.customer_name || selectedThread.other_party_number }}</span>
                <div class="pane-header-actions">
                  <Button
                    label="Mark read"
                    icon="pi pi-check"
                    size="small"
                    severity="secondary"
                    :loading="markReadLoading"
                    @click="markThreadRead"
                    data-test="pc-mark-thread-read"
                  />
                  <Button v-tooltip="'Close'" icon="pi pi-times" text rounded severity="secondary" aria-label="Close" @click="closeThread" />
                </div>
              </div>
            </template>
            <template #content>
              <div class="pane-body">
                <div v-if="threadLoading" class="spinner-wrap"><ProgressSpinner /></div>
                <div
                  v-else
                  v-for="m in threadMessages"
                  :key="m.id"
                  :class="['msg-bubble', m.direction === 'in' ? 'msg-in' : 'msg-out']"
                >
                  <div v-if="m.body" class="msg-body">{{ m.body }}</div>
                  <div v-if="m.attachments && m.attachments.length" class="msg-media">
                    <a
                      v-for="(att, i) in m.attachments"
                      :key="i"
                      :href="mediaBlob(m.id, i) || undefined"
                      target="_blank"
                      rel="noopener"
                      data-test="pc-msg-media"
                    >
                      <img
                        v-if="mediaBlob(m.id, i)"
                        :src="mediaBlob(m.id, i)"
                        class="msg-media-img"
                        alt="MMS attachment"
                      />
                      <span v-else class="text-muted">📎 attachment</span>
                    </a>
                  </div>
                  <div class="msg-meta text-muted">
                    {{ formatDateTime(m.sent_at) }}
                    <span v-if="m.delivery_status"> · {{ m.delivery_status }}</span>
                  </div>
                </div>
              </div>

              <div class="compose-row">
                <Textarea
                  v-model="composeBody"
                  rows="2"
                  placeholder="Type a reply…"
                  class="compose-input"
                  data-test="pc-compose-body"
                />
                <Button
                  label="Send"
                  icon="pi pi-send"
                  :disabled="!composeBody.trim()"
                  @click="sendReply"
                  data-test="pc-compose-send"
                />
              </div>
              <div
                v-if="composeStatus"
                :class="['status-line', composeStatus.ok ? 'status-ok' : 'error-banner']"
              >
                {{ composeStatus.message }}
              </div>
            </template>
          </Card>
        </div>
        <div v-else class="empty-pane text-muted">
          Select a conversation to view messages.
        </div>
      </div>
    </section>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import { formatDateTime } from '../composables/useFormatters'

import Toolbar from 'primevue/toolbar'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Textarea from 'primevue/textarea'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()

const threads = ref([])
const total = ref(0)
const loading = ref(false)
const error = ref(null)

const selectedThread = ref(null)
const threadMessages = ref([])
const threadLoading = ref(false)
const composeBody = ref('')
const composeStatus = ref(null)
const markReadLoading = ref(false)

// MMS media is auth'd; like the call-audio proxy it can't ride an <img src>
// header, so fetch each attachment as a blob (Bearer + same-origin cookie)
// and bind an object URL. Keyed `${messageId}:${idx}`.
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
    for (let i = 0; i < atts.length; i++) {
      const key = mediaKey(m.id, i)
      if (mediaBlobs.value[key]) continue
      try {
        const r = await fetch(`/api/phone-com/messages/${m.id}/media/${i}`, {
          headers: _authHeaders(),
        })
        if (!r.ok) continue
        mediaBlobs.value[key] = URL.createObjectURL(await r.blob())
      } catch (_e) {
        /* leave unset — the 📎 fallback shows */
      }
    }
  }
}

function _revokeMedia() {
  for (const url of Object.values(mediaBlobs.value)) {
    if (url && url.startsWith('blob:')) URL.revokeObjectURL(url)
  }
  mediaBlobs.value = {}
}

const fetchThreads = async () => {
  loading.value = true
  error.value = null
  try {
    const r = await api.get('/api/phone-com/messages/threads?per_page=100')
    threads.value = r.items
    total.value = r.total
  } catch (err) {
    error.value = err.message || 'Failed to load threads'
  } finally {
    loading.value = false
  }
}

const openThread = async (thread) => {
  selectedThread.value = thread
  threadMessages.value = []
  _revokeMedia()
  threadLoading.value = true
  try {
    const r = await api.get(
      `/api/phone-com/messages/threads/${encodeURIComponent(thread.thread_key)}?per_page=500`,
    )
    threadMessages.value = r.items
    _loadThreadMedia(r.items)  // fire-and-forget; bubbles render as blobs resolve
  } catch (err) {
    error.value = err.message || 'Failed to load thread'
  } finally {
    threadLoading.value = false
  }
}

const sendReply = async () => {
  if (!selectedThread.value || !composeBody.value.trim()) return
  composeStatus.value = null
  try {
    const r = await api.post('/api/phone-com/messages', {
      to: selectedThread.value.other_party_number,
      body: composeBody.value.trim(),
      customer_id: selectedThread.value.customer_id || undefined,
    })
    composeStatus.value = {
      ok: true,
      message: `Sent · ${r.delivery_status || 'queued'}`,
    }
    composeBody.value = ''
    await openThread(selectedThread.value)
  } catch (err) {
    composeStatus.value = {
      ok: false,
      message: err.message || 'Send failed',
    }
  }
}

const markThreadRead = async () => {
  if (!selectedThread.value) return
  markReadLoading.value = true
  composeStatus.value = null
  try {
    await api.post(
      `/api/phone-com/messages/threads/${encodeURIComponent(selectedThread.value.thread_key)}/mark-read`,
    )
    composeStatus.value = { ok: true, message: 'Marked read on Phone.com.' }
  } catch (err) {
    composeStatus.value = { ok: false, message: err.message || 'Mark-read failed' }
  } finally {
    markReadLoading.value = false
  }
}

const closeThread = () => {
  selectedThread.value = null
  threadMessages.value = []
  composeBody.value = ''
  composeStatus.value = null
  _revokeMedia()
}

onMounted(fetchThreads)
</script>

<style scoped>
.phone-com-messages {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.view-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
}
.msgs-layout {
  display: grid;
  grid-template-columns: minmax(280px, 360px) 1fr;
  gap: 1rem;
  align-items: start;
}
.thread-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  max-height: 70vh;
  overflow-y: auto;
}
.thread-card {
  cursor: pointer;
  transition: background 0.1s ease;
}
.thread-card.active {
  outline: 2px solid var(--p-primary-color);
}
.thread-card :deep(.p-card-body) {
  padding: 0.65rem 0.85rem;
}
.thread-row-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 500;
  margin-bottom: 0.2rem;
}
.thread-when {
  font-size: 0.8rem;
}
.thread-preview {
  font-size: 0.85rem;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.thread-pane {
  display: flex;
  flex-direction: column;
}
.pane-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.pane-header-actions {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.pane-body {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  max-height: 55vh;
  overflow-y: auto;
  padding: 0.5rem 0;
}
.msg-bubble {
  padding: 0.5rem 0.75rem;
  border-radius: 8px;
  max-width: 80%;
}
.msg-in {
  background: var(--p-content-hover-background);
  align-self: flex-start;
}
.msg-out {
  background: var(--p-primary-50, #eff6ff);
  align-self: flex-end;
}
.msg-meta {
  font-size: 0.75rem;
  margin-top: 0.2rem;
}
.msg-media {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin: 0.25rem 0;
}
.msg-media-img {
  max-width: 180px;
  max-height: 180px;
  border-radius: 6px;
  display: block;
}
.compose-row {
  display: flex;
  gap: 0.5rem;
  align-items: flex-start;
  margin-top: 0.75rem;
}
.compose-input {
  flex: 1;
}
.status-line {
  margin-top: 0.5rem;
  padding: 0.4rem 0.6rem;
  border-radius: 6px;
}
.status-ok {
  background: var(--p-green-50, #f0fdf4);
  color: var(--p-green-700, #15803d);
  border: 1px solid var(--p-green-200, #bbf7d0);
}
.error-banner {
  background: var(--p-red-50, #fef2f2);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid var(--p-red-200, #fecaca);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}
.empty-message,
.empty-pane {
  text-align: center;
  padding: 2rem 1rem;
  color: var(--p-text-muted-color);
}
.text-muted {
  color: var(--p-text-muted-color);
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 1.5rem;
}

/* Mobile: the desktop side-by-side thread list + pane layout becomes
   a single column. Selecting a thread fills the viewport, with a back
   button replacing the close X for one-handed thumb use. */
@media (max-width: 768px) {
  .msgs-layout {
    grid-template-columns: 1fr;
  }
  .thread-list {
    max-height: none;
  }
  .thread-pane {
    position: fixed;
    inset: 0;
    z-index: 20;
    background: var(--p-content-background, #fff);
    padding: 0.75rem;
    overflow-y: auto;
    padding-bottom: calc(5rem + env(safe-area-inset-bottom));
  }
  .pane-body {
    max-height: none;
  }
}
</style>

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
        <OfficeAlertsToggle compact />
      </header>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading && !threads.length" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading conversations…</span>
      </div>
      <div v-else-if="!threads.length" class="state-msg">
        <i class="pi pi-comment empty-icon" />
        <div class="empty-title">No conversations yet</div>
        <div class="empty-help">Texts to the business line appear here within about a minute.</div>
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
        modal
        :style="{ width: '100vw', height: '100dvh' }"
        :breakpoints="{ '768px': '100vw' }"
        position="bottom"
        aria-labelledby="ms-thread-title"
        @hide="closeThread"
      >
        <!-- The header was the :header prop, which can only be a string, so the
             customer's name sat at the top of the conversation as dead text.
             It is the route to the record now, which needs the #header slot
             (the close icon lives in .p-dialog-header-icons and is unaffected —
             same shape as UsersView's lockout dialog).

             `aria-labelledby` above is load-bearing and pairs with the id below.
             PrimeVue derives the dialog's accessible name from an id it puts on
             the :header prop's own <span>; taking the slot removes that span, so
             without this pair the modal announces with NO name at all. Pointing
             it at our own element restores it. (Verified against primevue 4.x by
             mounting the real Dialog both ways — the stubbed Dialog in this
             repo's specs cannot see it.)

             Falls back to the phone number when the thread has no customer:
             an SMS from an unknown number has nothing to link to. Guarded on
             not-deleted because /api/customers/{id} 404s on a soft-deleted
             record, and the name still shows either way. -->
        <template #header>
          <router-link
            v-if="selectedThread?.customer_id && selectedThread?.customer_name && !selectedThread?.customer_deleted"
            id="ms-thread-title"
            :to="`/mobile/customers/${selectedThread.customer_id}`"
            class="thread-title customer-link"
            data-test="ms-customer-link"
          >{{ selectedThread.customer_name }}</router-link>
          <span v-else id="ms-thread-title" class="thread-title">
            {{ selectedThread ? (selectedThread.customer_name || selectedThread.other_party_number) : 'Conversation' }}
          </span>
        </template>
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
            <ScheduledTextsList
              v-if="scheduledItems.length"
              class="thread-scheduled"
              :items="scheduledItems"
              @changed="loadScheduled"
            />
          </div>

          <div v-if="photos.length" class="photo-strip" data-test="ms-compose-photos">
            <div v-for="(p, i) in photos" :key="i" class="photo-chip">
              <img v-if="p.preview" :src="p.preview" alt="" class="photo-thumb" />
              <span v-else class="photo-name">{{ p.file.name }}</span>
              <Button
                icon="pi pi-times"
                text
                rounded
                size="small"
                severity="secondary"
                :aria-label="`Remove ${p.file.name}`"
                @click="removePhoto(i)"
              />
            </div>
            <span v-if="photos.length > 1" class="photo-note">Each photo goes as its own text</span>
          </div>
          <div class="compose-row">
            <!-- One picker: on Android and iOS an image picker already offers
                 the camera beside the gallery, so a separate camera button
                 would only take width from the message box. -->
            <input
              ref="photoInput"
              type="file"
              :accept="SMS_PHOTO_ACCEPT"
              multiple
              hidden
              data-test="ms-compose-photo-input"
              @change="onPhotoPick"
            />
            <Button
              icon="pi pi-camera"
              aria-label="Attach a photo"
              severity="secondary"
              outlined
              :disabled="sending"
              @click="photoInput?.click()"
              data-test="ms-compose-attach"
            />
            <Textarea
              v-model="composeBody"
              rows="2"
              auto-resize
              :placeholder="photos.length ? 'Add a message…' : 'Type a reply…'"
              class="compose-input"
              data-test="ms-compose-body"
            />
            <Button
              v-tooltip="'Send later'"
              icon="pi pi-clock"
              aria-label="Send later"
              severity="secondary"
              outlined
              :disabled="!composeBody.trim() || sending || photos.length > 0"
              @click="laterOpen = !laterOpen"
              data-test="ms-compose-later"
            />
            <Button
              icon="pi pi-send"
              aria-label="Send"
              :disabled="(!composeBody.trim() && !photos.length) || sending"
              :loading="sending"
              @click="sendReply"
              data-test="ms-compose-send"
            />
          </div>
          <div v-if="photoProgress" class="quiet-hint" data-test="ms-photo-progress">{{ photoProgress }}</div>
          <div v-if="photoError" class="status-line error-banner" data-test="ms-photo-error">{{ photoError }}</div>
          <div
            v-if="quiet && composeBody.trim() && !laterOpen"
            class="quiet-hint"
            data-test="ms-quiet-hint"
          >
            <i class="pi pi-moon" /> It is late — the clock button holds this reply until the morning.
          </div>
          <SendLaterPanel
            v-if="laterOpen"
            :busy="scheduling"
            @schedule="scheduleReply"
            @cancel="laterOpen = false"
          />
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
import SendLaterPanel from '../components/SendLaterPanel.vue'
import ScheduledTextsList from '../components/ScheduledTextsList.vue'
import OfficeAlertsToggle from '../components/OfficeAlertsToggle.vue'
import { isQuietHours, whenLabel } from '../utils/sendLater'
import { useSmsPhotos, SMS_PHOTO_ACCEPT } from '../composables/useSmsPhotos'

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

// Picture texts (MMS) — one photo per text; see composables/useSmsPhotos.js.
const {
  photos, photoError, progress: photoProgress,
  add: addPhotos, remove: removePhoto, clear: clearPhotos, sendAll: sendPhotos,
} = useSmsPhotos(api)
const photoInput = ref(null)

function onPhotoPick(e) {
  addPhotos(e.target.files)
  e.target.value = '' // picking the same photo again must fire change again
}

// Send later — the server side is modules/phone_com/scheduled.py.
const laterOpen = ref(false)
const scheduling = ref(false)
const scheduledItems = ref([])
const quiet = ref(isQuietHours())

const loadScheduled = async () => {
  const number = selectedThread.value?.other_party_number
  if (!number) { scheduledItems.value = []; return }
  try {
    const r = await api.get(`/api/phone-com/scheduled?to=${encodeURIComponent(number)}`, { suppressErrorToast: true })
    scheduledItems.value = r.items || []
  } catch {
    scheduledItems.value = []
  }
}

const scheduleReply = async (at) => {
  if (!selectedThread.value || !composeBody.value.trim()) return
  scheduling.value = true
  composeStatus.value = null
  try {
    const r = await api.post('/api/phone-com/messages/schedule', {
      to: selectedThread.value.other_party_number,
      body: composeBody.value.trim(),
      customer_id: selectedThread.value.customer_id || undefined,
      send_at: at.toISOString(),
    }, { suppressErrorToast: true })
    composeStatus.value = { ok: true, message: `Scheduled · sends ${whenLabel(new Date(r.send_at))}` }
    composeBody.value = ''
    laterOpen.value = false
    await loadScheduled()
    _scrollToLatest()
  } catch (err) {
    composeStatus.value = { ok: false, message: err.message || 'Could not schedule the text' }
  } finally {
    scheduling.value = false
  }
}

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
  // A photo picked for one customer must never ride along to the next.
  if (selectedThread.value?.thread_key !== thread.thread_key) clearPhotos()
  selectedThread.value = thread
  threadMessages.value = []
  _revokeMedia()
  threadOpen.value = true
  threadLoading.value = true
  quiet.value = isQuietHours()
  laterOpen.value = false
  loadScheduled()
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

const _reloadThread = async (thread) => {
  const rr = await api.get(
    `/api/phone-com/messages/threads/${encodeURIComponent(thread.thread_key)}?per_page=500`,
  )
  if (selectedThread.value?.thread_key !== thread.thread_key) return
  threadMessages.value = rr.items
  _loadThreadMedia(rr.items)
  _scrollToLatest()
}

const sendReply = async () => {
  if (!selectedThread.value || sending.value) return
  if (!composeBody.value.trim() && !photos.value.length) return
  // The thread is fixed when Send is pressed. The user may close it or open
  // another while this uploads; nothing below may then write into that one.
  const thread = selectedThread.value
  const stillOpen = () => selectedThread.value?.thread_key === thread.thread_key
  sending.value = true
  composeStatus.value = null
  try {
    if (photos.value.length) {
      const r = await sendPhotos({
        to: thread.other_party_number,
        body: composeBody.value,
        customerId: thread.customer_id,
      })
      if (!stillOpen()) return
      if (r.wordsSent) composeBody.value = ''
      composeStatus.value = r.error
        ? { ok: false, message: r.sent ? `${r.sent} sent, then: ${r.error}` : r.error }
        : { ok: true, message: r.sent > 1 ? `Sent ${r.sent} photos` : 'Photo sent' }
      // Always reload: a send Phone.com did not confirm is in the thread as
      // "unknown", and the error says to check there before sending again.
      await _reloadThread(thread).catch(() => {})
      return
    }
    const r = await api.post('/api/phone-com/messages', {
      to: thread.other_party_number,
      body: composeBody.value.trim(),
      customer_id: thread.customer_id || undefined,
    })
    if (!stillOpen()) return
    composeStatus.value = { ok: true, message: `Sent · ${r.delivery_status || 'queued'}` }
    composeBody.value = ''
    // Reload the conversation so the sent bubble appears.
    await _reloadThread(thread)
  } catch (err) {
    if (!stillOpen()) return
    composeStatus.value = { ok: false, message: err.message || 'Send failed' }
    await _reloadThread(thread).catch(() => {})
  } finally {
    sending.value = false
  }
}

const closeThread = () => {
  selectedThread.value = null
  threadMessages.value = []
  scheduledItems.value = []
  laterOpen.value = false
  composeBody.value = ''
  composeStatus.value = null
  clearPhotos()
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
  clearPhotos()
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
/* Replaces what PrimeVue's own .p-dialog-title gave the :header prop — the
   #header slot renders raw, so the conversation title has to carry its own
   weight and size or it shrinks to body text. */
.thread-title {
  font-weight: 600;
  font-size: 1.05rem;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 44px tap floor, as on the other mobile customer links. The header row is
   already taller than this, so the target costs no layout.
   inline-BLOCK, not inline-flex: an inline-flex box puts its text in an
   anonymous flex item, where the `text-overflow: ellipsis` above stops
   applying — a long business name would then overflow the header instead of
   truncating. line-height carries the height that align-items would have.
   Underlined rather than coloured alone: the primary token's contrast on a
   light background is low app-wide (this repo records 2.53:1 for it in
   main.js), a separately-owned theming issue this change matches rather than
   re-themes. */
.thread-title.customer-link {
  display: inline-block;
  line-height: 44px;
  color: var(--p-primary-color);
  text-decoration: underline;
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
.photo-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding-top: 6px;
}
.photo-chip {
  position: relative;
}
.photo-chip .p-button {
  position: absolute;
  top: -10px;
  right: -10px;
  background: var(--p-content-background);
}
.photo-thumb {
  width: 64px;
  height: 64px;
  object-fit: cover;
  border-radius: 6px;
  display: block;
}
.photo-name {
  display: block;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.8rem;
  padding: 0.5rem 1.2rem 0.5rem 0.4rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
}
.photo-note {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
.quiet-hint {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
.thread-scheduled {
  align-self: flex-end;
  width: 100%;
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

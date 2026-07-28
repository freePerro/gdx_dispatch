<template>
    <section class="mobile-inbox">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>Inbox</h1>
          <div class="head-actions">
            <Button label="Compose" icon="pi pi-pencil" size="small" @click="startCompose" data-test="mi-compose" />
            <Button v-tooltip="'Refresh'" icon="pi pi-refresh" aria-label="Refresh" text size="small" :loading="loading" @click="fetchMessages" data-test="mi-refresh" />
          </div>
        </div>
      </header>

      <div class="mi-search">
        <i class="pi pi-search" aria-hidden="true" />
        <input
          v-model="searchTerm"
          type="search"
          class="mi-search-input"
          placeholder="Search subject, sender, preview…"
          aria-label="Search mail"
          data-test="mi-search"
          @input="onSearchInput"
        />
        <button v-if="activeSearch" class="mi-search-clear" data-test="mi-search-clear" aria-label="Clear search" @click="clearSearch">✕</button>
      </div>
      <!-- Same disclosure the desktop makes. The tech in the truck is the one
           most likely to search for an address that lives only in the body —
           zero results must not read as "that email isn't in GDX". -->
      <p v-if="activeSearch" class="mi-search-note" data-test="mi-search-note">
        Searching subject, sender and preview text — not full message bodies.
      </p>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading && !messages.length" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading inbox…</span>
      </div>
      <div v-else-if="!messages.length" class="state-msg">
        <i class="pi pi-inbox empty-icon" />
        <div class="empty-title">Inbox is empty</div>
        <div class="empty-help">Tap refresh above, or compose a new message.</div>
      </div>

      <ol v-else class="card-list">
        <li
          v-for="m in messages"
          :key="m.id"
          class="msg-card"
          :class="{ unread: !m.is_read }"
          @click="openMessage(m)"
          data-test="mi-msg-row"
        >
          <div class="msg-row">
            <span class="msg-from">{{ m.from_address || m.from_name || '—' }}</span>
            <span class="msg-when">{{ fmtAgo(m.received_at || m.sent_at) }}</span>
          </div>
          <div class="msg-subject">{{ m.subject || '(no subject)' }}</div>
          <div class="msg-preview">{{ m.body_preview || '' }}</div>
          <div v-if="m.linked_customer_id || m.linked_job_id" class="msg-links" data-test="mi-row-links">
            <span v-if="m.linked_customer_id" class="link-chip">{{ m.linked_customer_name || 'Customer' }}</span>
            <span v-if="m.linked_job_id" class="link-chip job">{{ m.linked_job_label || 'Job' }}</span>
          </div>
        </li>
      </ol>
      <button
        v-if="hasMoreMessages"
        class="mi-loadmore"
        data-test="mi-load-more"
        :disabled="loading"
        @click="loadMoreMessages"
      >
        {{ loading ? 'Loading…' : 'Load more' }}
      </button>

      <!-- Detail / reply -->
      <Dialog
        v-model:visible="detailOpen"
        :header="detail?.subject || 'Message'"
        modal
        :style="{ width: '100vw', height: '100dvh' }"
        :breakpoints="{ '768px': '100vw' }"
        position="bottom"
      >
        <div v-if="detailLoading" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
        </div>
        <div v-else-if="detail" class="detail-body">
          <div class="detail-meta">
            <div><strong>From:</strong> {{ detail.from_address || detail.from_name || '—' }}</div>
            <div v-if="detail.to_addresses?.length || detail.to_address"><strong>To:</strong> {{ detail.to_addresses?.join(', ') || detail.to_address }}</div>
            <div class="muted">{{ fmtFull(detail.received_at || detail.sent_at) }}</div>
          </div>
          <EmailBodyFrame
            :html="bodyData.body_html || bodyData.body_preview || detail.body_preview || ''"
            :content-type="bodyFrameType"
            :loading="bodyLoading"
            :note="bodyNote"
          />
          <EmailAttachments
            v-if="detail.has_attachments"
            :message-id="detail.id"
            :has-attachments="detail.has_attachments"
            :linked-job-id="detail.linked_job_id"
            :linked-job-label="detail.linked_job_label"
          />

          <div v-if="detail.linked_customer_id || detail.linked_job_id" class="msg-links" data-test="mi-detail-links">
            <span v-if="detail.linked_customer_id" class="link-chip">{{ detail.linked_customer_name || 'Customer' }}</span>
            <span v-if="detail.linked_job_id" class="link-chip job">{{ detail.linked_job_label || 'Job' }}</span>
          </div>

          <!-- 1.3 the rest of the conversation, server-resolved -->
          <div v-if="threadOthers.length" class="thread-strip" data-test="mi-thread">
            <div class="thread-title">Conversation · {{ thread.length }} messages</div>
            <button
              v-for="t in threadOthers"
              :key="t.id"
              class="thread-row"
              data-test="mi-thread-row"
              @click="openMessage(t)"
            >
              <span class="thread-subject">{{ t.subject || '(no subject)' }}</span>
              <span class="thread-when">{{ fmtAgo(t.received_at || t.sent_at) }}</span>
            </button>
          </div>

          <div v-if="composeMode === 'reply'" class="reply-block">
            <h3>Reply</h3>
            <Textarea v-model="replyBody" rows="6" autoResize class="w-full" placeholder="Type your reply…" data-test="mi-reply-body" />
            <div class="reply-actions">
              <Button label="Cancel" severity="secondary" text @click="composeMode = null" />
              <Button label="Draft with AI" icon="pi pi-sparkles" severity="secondary" outlined :loading="aiDrafting" @click="draftWithAi" data-test="mi-ai-draft" />
              <Button label="Send reply" icon="pi pi-send" :loading="replySaving" :disabled="!replyBody.trim()" @click="sendReply" data-test="mi-reply-send" />
            </div>
          </div>

          <div v-if="composeMode === 'forward'" class="reply-block">
            <h3>Forward</h3>
            <InputText v-model="forwardForm.to" class="w-full" placeholder="recipient@example.com" data-test="mi-forward-to" />
            <Textarea v-model="forwardForm.comment" rows="4" autoResize class="w-full" placeholder="Optional note…" data-test="mi-forward-comment" />
            <p class="muted">The original attachments go with it.</p>
            <div class="reply-actions">
              <Button label="Cancel" severity="secondary" text @click="composeMode = null" />
              <Button label="Forward" icon="pi pi-share-alt" :loading="forwardSending" :disabled="!forwardForm.to.trim()" @click="sendForward" data-test="mi-forward-send" />
            </div>
          </div>
        </div>
        <template #footer>
          <Button v-if="detail && composeMode !== 'reply'" label="Reply" icon="pi pi-reply" @click="startReply" data-test="mi-reply-open" />
          <!-- Owner-only: Graph's forward resolves the message id against the
               caller's own mailbox, so the server 403s a non-owner. Same gate
               as the personal toggle. -->
          <Button v-if="detail && detail.viewer_is_owner && composeMode !== 'forward'" label="Forward" icon="pi pi-share-alt" severity="secondary" text @click="startForward" data-test="mi-forward-open" />
          <Button v-if="detail" label="Task" icon="pi pi-check-square" severity="secondary" text :loading="taskSaving" @click="createTaskFromEmail" data-test="mi-create-task" />
          <Button v-if="detail && !detail.is_read" label="Mark unread later" icon="pi pi-eye-slash" severity="secondary" text @click="markUnread" data-test="mi-mark-unread" />
          <!-- Owner-only privacy override; server 403s non-owners. -->
          <Button
            v-if="detail && detail.viewer_is_owner"
            :label="detail.is_personal ? 'Make shared' : 'Make personal'"
            :icon="detail.is_personal ? 'pi pi-lock-open' : 'pi pi-lock'"
            severity="secondary"
            text
            :loading="personalSaving"
            data-test="mi-personal-toggle"
            @click="togglePersonal"
          />
          <Button label="Close" severity="secondary" @click="closeDetail" />
        </template>
      </Dialog>

      <!-- Compose new -->
      <Dialog
        v-model:visible="composeOpen"
        header="New message"
        modal
        :style="{ width: '100vw', height: '100dvh' }"
        :breakpoints="{ '768px': '100vw' }"
        position="bottom"
      >
        <div class="form-stack">
          <div>
            <label>To</label>
            <InputText v-model="composeForm.to" type="email" class="w-full" placeholder="recipient@example.com" data-test="mi-compose-to" />
          </div>
          <div>
            <label>Cc</label>
            <InputText v-model="composeForm.cc" type="email" class="w-full" placeholder="optional" data-test="mi-compose-cc" />
          </div>
          <div>
            <label>Subject</label>
            <InputText v-model="composeForm.subject" class="w-full" data-test="mi-compose-subject" />
          </div>
          <div>
            <label>Body</label>
            <Textarea v-model="composeForm.body" rows="10" autoResize class="w-full" data-test="mi-compose-body" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="composeOpen = false" />
          <Button
            label="Send"
            icon="pi pi-send"
            :loading="composeSaving"
            :disabled="!composeForm.to.trim() || !composeForm.subject.trim() || !composeForm.body.trim()"
            @click="sendCompose"
            data-test="mi-compose-send"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import EmailBodyFrame from '../components/EmailBodyFrame.vue'
import EmailAttachments from '../components/EmailAttachments.vue'

const api = useApi()
const toast = useToast()

const messages = ref([])
const loading = ref(false)
const error = ref(null)

const detailOpen = ref(false)
const detail = ref(null)
const detailLoading = ref(false)

const composeMode = ref(null)  // null | 'reply'
const replyBody = ref('')
const replySaving = ref(false)

const composeOpen = ref(false)
const composeForm = ref({ to: '', cc: '', subject: '', body: '' })
const composeSaving = ref(false)

// D1 — full body is live-fetched into bodyData; EmailBodyFrame renders it in
// a sandboxed iframe. (Replaces a v-html that would have become a live XSS
// vector the moment body_html was populated.)
const bodyData = ref({})
const bodyLoading = ref(false)
const activeMsgId = ref(null)
const _BODY_NOTES = {
  reconnect_required: 'Showing preview — reconnect this mailbox to load the full message.',
  message_gone: 'Showing preview — this message is no longer in the mailbox.',
  no_remote_copy: 'Showing preview — this message has no server copy.',
  no_account_owner: 'Showing preview — no connected mailbox owns this message.',
  graph_error: 'Showing preview — could not reach the mail server.',
  empty_body: '',
}
const bodyNote = computed(() =>
  bodyData.value.fetched === false ? (_BODY_NOTES[bodyData.value.reason] ?? '') : '',
)
// Honor server content_type — a fetched plain-text body still fills body_html,
// so "has body_html ? html : text" would render text as HTML.
const bodyFrameType = computed(() =>
  bodyData.value.fetched && bodyData.value.content_type
    ? bodyData.value.content_type
    : 'text',
)

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

function fmtFull(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

const MSG_PAGE_SIZE = 50
const msgOffset = ref(0)
const hasMoreMessages = ref(false)

// 1.1 — server-side search over the whole mailbox (not the loaded page).
const searchTerm = ref('')
const activeSearch = ref('')
let _searchTimer = null

function onSearchInput() {
  clearTimeout(_searchTimer)
  _searchTimer = setTimeout(() => {
    activeSearch.value = searchTerm.value.trim()
    fetchMessages()
  }, 350)
}

function clearSearch() {
  clearTimeout(_searchTimer)
  searchTerm.value = ''
  activeSearch.value = ''
  fetchMessages()
}

async function fetchMessages({ append = false } = {}) {
  loading.value = true
  error.value = null
  try {
    // Page over RAW rows (offset); the server filters visibility after the
    // window, so has_more/next_offset drive load-more until all mail is
    // reachable (D7).
    const offset = append ? msgOffset.value : 0
    let url = `/api/outlook/messages?limit=${MSG_PAGE_SIZE}&offset=${offset}`
    if (activeSearch.value) url += `&q=${encodeURIComponent(activeSearch.value)}`
    const r = await api.get(url)
    const items = Array.isArray(r) ? r : (r?.items || [])
    if (append) {
      const seen = new Set(messages.value.map((m) => m.id))
      messages.value = [...messages.value, ...items.filter((m) => !seen.has(m.id))]
    } else {
      messages.value = items
    }
    msgOffset.value = (r && typeof r.next_offset === 'number') ? r.next_offset : offset + MSG_PAGE_SIZE
    hasMoreMessages.value = !!(r && r.has_more)
  } catch (err) {
    error.value = err.message || 'Failed to load inbox'
  } finally {
    loading.value = false
  }
}

function loadMoreMessages() {
  return fetchMessages({ append: true })
}

async function openMessage(m) {
  detail.value = null
  bodyData.value = {}
  // Clear the previous message's conversation too. Without this, tapping B
  // shows A's thread under B's header for the whole round-trip — and if B's
  // metadata fetch fails, loadThread never runs and A's siblings stay there
  // indefinitely, one tap away from navigating into the wrong thread.
  thread.value = []
  activeMsgId.value = m.id
  detailOpen.value = true
  detailLoading.value = true
  composeMode.value = null
  replyBody.value = ''
  try {
    const meta = await api.get(`/api/outlook/messages/${m.id}`)
    // Race guard: a fast re-tap must not let an earlier message's metadata
    // land in the pane the user has moved to.
    if (activeMsgId.value !== m.id) return
    detail.value = meta
    // Mark as read on open (best-effort)
    if (!m.is_read) {
      try {
        await api.patch(`/api/outlook/messages/${m.id}/read`, { is_read: true })
        m.is_read = true
      } catch { /* ignore */ }
    }
  } catch (err) {
    if (activeMsgId.value === m.id) error.value = err.message || 'Failed to load message'
  } finally {
    if (activeMsgId.value === m.id) detailLoading.value = false
  }
  if (detail.value && activeMsgId.value === m.id) {
    loadBody(m.id)
    loadThread(m.id)
  }
}

// ── 1.3 conversation ─────────────────────────────────────────────────
const thread = ref([])
const threadOthers = computed(() => thread.value.filter((t) => t.id !== activeMsgId.value))

async function loadThread(id) {
  try {
    const rows = await api.get(`/api/outlook/messages/${id}/thread`, { suppressErrorToast: true })
    if (activeMsgId.value === id) thread.value = Array.isArray(rows) ? rows : []
  } catch {
    if (activeMsgId.value === id) thread.value = []
  }
}

// ── 1.4 forward (native Graph forward — keeps the attachments) ───────
const forwardForm = ref({ to: '', comment: '' })
const forwardSending = ref(false)

function startForward() {
  forwardForm.value = { to: '', comment: '' }
  composeMode.value = 'forward'
}

async function sendForward() {
  if (!detail.value) return
  const to = splitAddrs(forwardForm.value.to)
  if (!to.length) {
    toast.add({ severity: 'error', summary: 'A recipient is required', life: 3000 })
    return
  }
  forwardSending.value = true
  try {
    await api.post(`/api/outlook/messages/${detail.value.id}/forward`, {
      to,
      comment: forwardForm.value.comment || '',
    })
    toast.add({ severity: 'success', summary: 'Forwarded', life: 2500 })
    composeMode.value = null
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Forward failed', detail: err.message, life: 4000 })
  } finally {
    forwardSending.value = false
  }
}

// ── P2.2 email → follow-up task ──────────────────────────────────────
const taskSaving = ref(false)

async function createTaskFromEmail() {
  if (!detail.value) return
  taskSaving.value = true
  try {
    await api.post(`/api/outlook/messages/${detail.value.id}/create-task`, {})
    toast.add({ severity: 'success', summary: 'Follow-up task created', life: 2500 })
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not create task', detail: err.message, life: 4000 })
  } finally {
    taskSaving.value = false
  }
}

// ── P2.4 AI-suggested reply ──────────────────────────────────────────
const aiDrafting = ref(false)

async function draftWithAi() {
  if (!detail.value) return
  aiDrafting.value = true
  try {
    const r = await api.post(`/api/outlook/messages/${detail.value.id}/ai-draft`, {})
    if (r?.draft_text) replyBody.value = `${r.draft_text}${quotedTail()}`
    // Say when it's the canned fallback. Silently inserting boilerplate under
    // a sparkles button lets a tech send a generic acknowledgement believing
    // it was written for this message.
    if (r?.source === 'fallback') {
      toast.add({
        severity: 'info',
        summary: 'No AI configured',
        detail: 'Inserted a standard reply — edit before sending.',
        life: 4000,
      })
    }
  } catch (err) {
    toast.add({ severity: 'error', summary: 'AI draft failed', detail: err.message, life: 3500 })
  } finally {
    aiDrafting.value = false
  }
}

async function loadBody(id) {
  bodyLoading.value = true
  try {
    const data = await api.get(`/api/outlook/messages/${id}/body`)
    if (activeMsgId.value === id) bodyData.value = data || {}
  } catch {
    if (activeMsgId.value === id) bodyData.value = { fetched: false, reason: 'graph_error' }
  } finally {
    bodyLoading.value = false
  }
}

function closeDetail() {
  detailOpen.value = false
  detail.value = null
  bodyData.value = {}
  thread.value = []
  activeMsgId.value = null
  composeMode.value = null
  replyBody.value = ''
}

function quotedTail() {
  const d = detail.value
  if (!d) return ''
  return `\n\n---\nOn ${fmtFull(d.sent_at || d.received_at)}, ${d.from_address || ''} wrote:\n${(d.body_preview || '').split('\n').map(l => `> ${l}`).join('\n')}`
}

function startReply() {
  if (!detail.value) return
  composeMode.value = 'reply'
  replyBody.value = quotedTail()
}

// Split a comma/semicolon address string into the array SendMailIn wants.
// Mirrors the desktop InboxView helper.
function splitAddrs(s) {
  return (s || '').split(/[,;]/).map((x) => x.trim()).filter(Boolean)
}

// body_html is HTML: escape the user's plaintext so "cost < $500 & up" reaches
// the recipient intact (not swallowed as a bogus tag), THEN turn newlines into
// <br>. Order matters — escape & before < >.
function plaintextToHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\n/g, '<br>')
}

async function sendReply() {
  if (!detail.value || !replyBody.value.trim()) return
  const to = splitAddrs(detail.value.from_address)
  if (!to.length) {
    toast.add({ severity: 'error', summary: 'No reply address', life: 3000 })
    return
  }
  replySaving.value = true
  try {
    const subj = detail.value.subject || ''
    const replySubj = subj.toLowerCase().startsWith('re:') ? subj : `Re: ${subj}`
    // SendMailIn is extra=forbid: `to` must be a list, body field is
    // `body_html` (not `body`). Sending strings/`body` here was the 422.
    await api.post('/api/outlook/send', {
      to,
      subject: replySubj,
      body_html: plaintextToHtml(replyBody.value),
      in_reply_to: detail.value.id,
    })
    toast.add({ severity: 'success', summary: 'Reply sent', life: 2500 })
    composeMode.value = null
    replyBody.value = ''
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Send failed', detail: err.message, life: 4000 })
  } finally {
    replySaving.value = false
  }
}

const personalSaving = ref(false)

async function togglePersonal() {
  if (!detail.value) return
  personalSaving.value = true
  try {
    detail.value = await api.post(
      `/api/outlook/messages/${detail.value.id}/personal`,
      { is_personal: !detail.value.is_personal },
    )
  } catch (err) {
    error.value = err.message || 'Failed to update message privacy'
  } finally {
    personalSaving.value = false
  }
}

async function markUnread() {
  if (!detail.value) return
  try {
    await api.patch(`/api/outlook/messages/${detail.value.id}/read`, { is_read: false })
    detail.value.is_read = false
    const row = messages.value.find((m) => m.id === detail.value.id)
    if (row) row.is_read = false
    toast.add({ severity: 'success', summary: 'Marked unread', life: 2000 })
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Failed', detail: err.message, life: 3000 })
  }
}

function startCompose() {
  composeForm.value = { to: '', cc: '', subject: '', body: '' }
  composeOpen.value = true
}

async function sendCompose() {
  const form = composeForm.value
  const to = splitAddrs(form.to)
  // Guard on the SPLIT result, not raw .trim(): a "to" of only separators
  // (";") is truthy-trimmed but yields [], which SendMailIn (to min_length=1)
  // would 422 on.
  if (!to.length || !form.subject.trim() || !form.body.trim()) {
    toast.add({ severity: 'error', summary: 'A recipient, subject, and body are required', life: 3000 })
    return
  }
  composeSaving.value = true
  try {
    // Build EXACTLY the SendMailIn shape — never spread composeForm, whose
    // string `to`/`cc` + `body` field all trip extra=forbid (the 422).
    const payload = {
      to,
      subject: form.subject,
      body_html: plaintextToHtml(form.body),
    }
    const cc = splitAddrs(form.cc)
    if (cc.length) payload.cc = cc
    await api.post('/api/outlook/send', payload)
    toast.add({ severity: 'success', summary: 'Sent', life: 2500 })
    composeOpen.value = false
    composeForm.value = { to: '', cc: '', subject: '', body: '' }
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Send failed', detail: err.message, life: 4000 })
  } finally {
    composeSaving.value = false
  }
}

onMounted(fetchMessages)
</script>

<style scoped>
.mobile-inbox {
  padding: 0.75rem 0.75rem calc(5rem + env(safe-area-inset-bottom));
  max-width: 800px;
  margin: 0 auto;
}

.mobile-page-head {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  margin-bottom: 0.75rem;
}

.head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.mobile-page-head h1 {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}

.head-actions {
  display: flex;
  gap: 0.4rem;
  align-items: center;
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

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.msg-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.75rem 0.85rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.msg-card.unread {
  border-left: 3px solid var(--p-primary-color, #2563eb);
  font-weight: 500;
}

.msg-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.85rem;
  gap: 0.5rem;
}

.msg-from {
  font-weight: 600;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-when {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.78rem;
  flex-shrink: 0;
}

.msg-subject {
  font-size: 0.95rem;
}

.msg-card.unread .msg-subject {
  font-weight: 700;
}

.mi-loadmore {
  width: 100%;
  padding: 0.8rem;
  border: none;
  border-top: 1px solid var(--p-content-border-color);
  background: var(--p-content-background);
  color: var(--p-primary-color);
  font-size: 0.9rem;
  cursor: pointer;
}
.mi-loadmore:disabled {
  opacity: 0.6;
}
.msg-preview {
  font-size: 0.82rem;
  color: var(--p-text-muted-color, #6b7280);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.detail-body {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.detail-meta {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.85rem;
  padding-bottom: 0.6rem;
  border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
}

.reply-block {
  margin-top: 1rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}

.reply-block h3 {
  margin: 0 0 0.5rem;
  font-size: 1rem;
}

.reply-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.4rem;
  margin-top: 0.5rem;
}

.muted {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.78rem;
}

.state-msg {
  text-align: center;
  padding: 2.5rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.4rem;
}

.empty-icon {
  font-size: 2rem;
  opacity: 0.5;
}

.empty-title {
  font-size: 1.05rem;
  font-weight: 600;
}

.empty-help {
  font-size: 0.85rem;
}

.form-stack {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.form-stack label {
  display: block;
  font-size: 0.85rem;
  font-weight: 500;
  margin-bottom: 0.2rem;
}

.w-full {
  width: 100%;
}

/* ── search ── */
.mi-search {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.45rem 0.7rem;
  margin-bottom: 0.6rem;
}
.mi-search .pi-search { color: var(--p-text-muted-color, #6b7280); }
.mi-search-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  outline: none;
  font-size: 1rem; /* 16px — anything smaller makes iOS zoom on focus */
  color: var(--p-text-color, #1e293b);
}
.mi-search-clear {
  border: none;
  background: transparent;
  color: var(--p-text-muted-color, #6b7280);
  font-size: 1rem;
  /* 44px touch target — this sits next to a text field on a phone */
  min-width: 44px;
  min-height: 44px;
}
.mi-search-note {
  margin: -0.35rem 0 0.6rem;
  font-size: 0.75rem;
  color: var(--p-text-muted-color, #6b7280);
}

/* ── link chips ── */
.msg-links {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
  margin-top: 0.15rem;
}
.link-chip {
  font-size: 0.7rem;
  padding: 0.05rem 0.45rem;
  border-radius: 10px;
  border: 1px solid var(--p-primary-color, #2563eb);
  color: var(--p-primary-color, #2563eb);
  max-width: 12rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.link-chip.job {
  border-color: var(--p-content-border-color, #e5e7eb);
  color: var(--p-text-muted-color, #6b7280);
}

/* ── conversation strip ── */
.thread-strip {
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
  padding-top: 0.5rem;
}
.thread-title {
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--p-text-muted-color, #6b7280);
  margin-bottom: 0.25rem;
}
.thread-row {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  width: 100%;
  min-height: 44px;
  align-items: center;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--p-content-border-color, #f1f5f9);
  text-align: left;
  color: var(--p-text-color, #1e293b);
  font-size: 0.85rem;
}
.thread-subject { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.thread-when { flex-shrink: 0; font-size: 0.75rem; color: var(--p-text-muted-color, #6b7280); }
</style>

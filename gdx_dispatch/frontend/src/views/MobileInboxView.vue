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
        </li>
      </ol>

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
          <div class="detail-body-text" v-html="detailBodyHtml" />

          <div v-if="composeMode === 'reply'" class="reply-block">
            <h3>Reply</h3>
            <Textarea v-model="replyBody" rows="6" autoResize class="w-full" placeholder="Type your reply…" data-test="mi-reply-body" />
            <div class="reply-actions">
              <Button label="Cancel" severity="secondary" text @click="composeMode = null" />
              <Button label="Send reply" icon="pi pi-send" :loading="replySaving" :disabled="!replyBody.trim()" @click="sendReply" data-test="mi-reply-send" />
            </div>
          </div>
        </div>
        <template #footer>
          <Button v-if="detail && composeMode !== 'reply'" label="Reply" icon="pi pi-reply" @click="startReply" data-test="mi-reply-open" />
          <Button v-if="detail && !detail.is_read" label="Mark unread later" icon="pi pi-eye-slash" severity="secondary" text @click="markUnread" data-test="mi-mark-unread" />
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
            :disabled="!composeForm.to.trim() || !composeForm.subject.trim()"
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

const detailBodyHtml = computed(() => {
  if (!detail.value) return ''
  const html = detail.value.body_html || detail.value.body || detail.value.body_preview || ''
  if (detail.value.body_html) return html
  // Plain text fallback — escape and preserve newlines
  return String(html)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\n/g, '<br/>')
})

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

async function fetchMessages() {
  loading.value = true
  error.value = null
  try {
    const r = await api.get('/api/outlook/messages?limit=100')
    messages.value = Array.isArray(r) ? r : r?.items || []
  } catch (err) {
    error.value = err.message || 'Failed to load inbox'
  } finally {
    loading.value = false
  }
}

async function openMessage(m) {
  detail.value = null
  detailOpen.value = true
  detailLoading.value = true
  composeMode.value = null
  replyBody.value = ''
  try {
    detail.value = await api.get(`/api/outlook/messages/${m.id}`)
    // Mark as read on open (best-effort)
    if (!m.is_read) {
      try {
        await api.patch(`/api/outlook/messages/${m.id}/read`, { is_read: true })
        m.is_read = true
      } catch { /* ignore */ }
    }
  } catch (err) {
    error.value = err.message || 'Failed to load message'
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detailOpen.value = false
  detail.value = null
  composeMode.value = null
  replyBody.value = ''
}

function startReply() {
  if (!detail.value) return
  composeMode.value = 'reply'
  const subj = detail.value.subject || ''
  const quoted = `\n\n---\nOn ${fmtFull(detail.value.sent_at || detail.value.received_at)}, ${detail.value.from_address || ''} wrote:\n${(detail.value.body_preview || '').split('\n').map(l => `> ${l}`).join('\n')}`
  replyBody.value = quoted
}

async function sendReply() {
  if (!detail.value || !replyBody.value.trim()) return
  replySaving.value = true
  try {
    const subj = detail.value.subject || ''
    const replySubj = subj.toLowerCase().startsWith('re:') ? subj : `Re: ${subj}`
    await api.post('/api/outlook/send', {
      to: detail.value.from_address || detail.value.from_name,
      subject: replySubj,
      body: replyBody.value,
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
  if (!composeForm.value.to.trim() || !composeForm.value.subject.trim()) return
  composeSaving.value = true
  try {
    await api.post('/api/outlook/send', { ...composeForm.value })
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

.detail-body-text {
  font-size: 0.95rem;
  line-height: 1.5;
  word-break: break-word;
}

.detail-body-text :deep(img) {
  max-width: 100%;
  height: auto;
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
</style>

<!--
  EmailTimeline — the Email tab on a customer or a job (P2.1).

  Reads the by-customer / by-job endpoints that have existed since Phase 5 but
  had no caller: the tagger (D3, wired in v1.19.0) is what finally puts rows
  behind them. Every row is already visibility-filtered server-side, so this
  component renders whatever it is handed.

  Bodies are live-fetched on expand through the same sandboxed EmailBodyFrame
  the inbox uses — never v-html, because this is attacker-controlled HTML.
-->
<template>
  <div class="email-timeline" data-test="email-timeline">
    <div v-if="loading" class="tl-state">
      <i class="pi pi-spin pi-spinner" /> Loading email…
    </div>
    <div v-else-if="error" class="tl-state tl-error" data-test="email-timeline-error">{{ error }}</div>
    <div v-else-if="!messages.length" class="tl-state" data-test="email-timeline-empty">
      <i class="pi pi-inbox" />
      <div>No email linked to this {{ scopeLabel }} yet.</div>
      <div class="tl-hint">
        Mail links automatically when the sender matches a customer, or when a
        subject carries a job marker. You can also link a message by hand from
        the Inbox.
      </div>
    </div>

    <ul v-else class="tl-list">
      <li v-for="m in messages" :key="m.id" class="tl-item" :class="{ open: openId === m.id }">
        <button class="tl-head" data-test="email-timeline-row" @click="toggle(m)">
          <span class="tl-dir" :class="m.direction">{{ m.direction === 'outbound' ? 'Sent' : 'Received' }}</span>
          <span class="tl-who">{{ m.direction === 'outbound' ? (m.to_addresses || []).join(', ') : m.from_address }}</span>
          <span class="tl-subject">{{ m.subject || '(no subject)' }}</span>
          <span v-if="m.has_attachments" class="tl-clip"><i class="pi pi-paperclip" /></span>
          <span class="tl-when">{{ fmtDate(m.received_at || m.sent_at) }}</span>
        </button>

        <div v-if="openId === m.id" class="tl-body">
          <EmailBodyFrame
            :html="bodyData.body_html || bodyData.body_preview || m.body_preview || ''"
            :content-type="bodyFrameType"
            :loading="bodyLoading"
            :note="bodyNote"
          />
          <EmailAttachments
            v-if="m.has_attachments"
            :message-id="m.id"
            :has-attachments="m.has_attachments"
            :linked-job-id="m.linked_job_id"
            :linked-job-label="m.linked_job_label"
          />
          <div class="tl-actions">
            <router-link class="tl-link" to="/inbox">Open in Inbox →</router-link>
          </div>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useApi } from '../composables/useApi'
import { formatDateTime as fmtDate } from '../composables/useFormatters'
import EmailBodyFrame from './EmailBodyFrame.vue'
import EmailAttachments from './EmailAttachments.vue'

const props = defineProps({
  customerId: { type: [String, Number], default: null },
  jobId: { type: [String, Number], default: null },
})

const api = useApi()
const messages = ref([])
const loading = ref(false)
const error = ref(null)
const openId = ref(null)
const bodyData = ref({})
const bodyLoading = ref(false)

const scopeLabel = computed(() => (props.jobId ? 'job' : 'customer'))

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
// Honor the server's content_type: a fetched PLAIN-TEXT body still arrives in
// body_html, so inferring "html because body_html is set" renders text as HTML.
const bodyFrameType = computed(() =>
  bodyData.value.fetched && bodyData.value.content_type ? bodyData.value.content_type : 'text',
)

async function load() {
  const url = props.jobId
    ? `/api/outlook/messages/by-job/${props.jobId}`
    : props.customerId
      ? `/api/outlook/messages/by-customer/${props.customerId}`
      : null
  if (!url) { messages.value = []; return }
  loading.value = true
  error.value = null
  openId.value = null
  try {
    const r = await api.get(url, { suppressErrorToast: true })
    messages.value = Array.isArray(r) ? r : (r?.items || [])
  } catch (err) {
    // A tenant without the email module gets a 403 here — that's "no email
    // feature", not an error worth shouting about.
    error.value = err?.status === 403 ? null : (err?.message || 'Could not load email')
    messages.value = []
  } finally {
    loading.value = false
  }
}

async function toggle(m) {
  if (openId.value === m.id) { openId.value = null; return }
  openId.value = m.id
  bodyData.value = {}
  bodyLoading.value = true
  try {
    const data = await api.get(`/api/outlook/messages/${m.id}/body`, { suppressErrorToast: true })
    if (openId.value === m.id) bodyData.value = data || {}
  } catch {
    if (openId.value === m.id) bodyData.value = { fetched: false, reason: 'graph_error' }
  } finally {
    bodyLoading.value = false
  }
}

watch(() => [props.customerId, props.jobId], load, { immediate: true })
defineExpose({ reload: load })
</script>

<style scoped>
.email-timeline { display: flex; flex-direction: column; gap: 0.5rem; }
.tl-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.35rem;
  padding: 1.5rem 1rem;
  color: var(--p-text-muted-color, #64748b);
  font-size: 0.9rem;
  text-align: center;
}
.tl-hint { font-size: 0.8rem; max-width: 34rem; }
.tl-error { color: var(--p-red-600, #dc2626); }
.tl-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.35rem; }
.tl-item {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  background: var(--p-content-background, #fff);
  overflow: hidden;
}
.tl-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  background: transparent;
  border: none;
  padding: 0.55rem 0.7rem;
  cursor: pointer;
  text-align: left;
  color: var(--p-text-color, #1e293b);
  font-size: 0.85rem;
}
.tl-head:hover { background: var(--p-content-hover-background, #f8fafc); }
.tl-dir {
  flex: 0 0 auto;
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  padding: 0.05rem 0.4rem;
  border-radius: 10px;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  color: var(--p-text-muted-color, #64748b);
}
.tl-dir.outbound { border-color: var(--p-primary-color, #2563eb); color: var(--p-primary-color, #2563eb); }
.tl-who { flex: 0 0 auto; font-weight: 600; max-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tl-subject { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tl-clip { flex: 0 0 auto; color: var(--p-text-muted-color, #94a3b8); }
.tl-when { flex: 0 0 auto; font-size: 0.75rem; color: var(--p-text-muted-color, #94a3b8); }
.tl-body { padding: 0.5rem 0.7rem 0.7rem; border-top: 1px solid var(--p-content-border-color, #e5e7eb); }
.tl-actions { margin-top: 0.4rem; }
.tl-link { font-size: 0.8rem; color: var(--p-primary-color, #2563eb); text-decoration: none; }

@media (max-width: 640px) {
  .tl-head { flex-wrap: wrap; }
  .tl-who { max-width: 100%; }
  .tl-subject { flex: 1 1 100%; }
}
</style>

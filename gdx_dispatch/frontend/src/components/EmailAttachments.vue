<!--
  EmailAttachments — list + download a message's attachments (D4).

  Lists via the authed JSON endpoint on open; downloads stream bytes with the
  Bearer token (a plain <a href> can't send one), turning them into a blob the
  browser saves. Inline images are hidden here — they belong to the body
  (rendered in EmailBodyFrame), not the attachment tray.
-->
<template>
  <div v-if="visibleAttachments.length || note" class="email-attachments">
    <div v-if="note" class="att-note" data-test="att-note">{{ note }}</div>
    <ul v-if="visibleAttachments.length" class="att-list">
      <li v-for="a in visibleAttachments" :key="a.id" class="att-item">
        <button
          type="button"
          class="att-chip"
          data-test="att-chip"
          :disabled="downloadingId === a.id"
          @click="download(a)"
        >
          <i class="pi" :class="downloadingId === a.id ? 'pi-spin pi-spinner' : 'pi-paperclip'" />
          <span class="att-name">{{ a.name || 'attachment' }}</span>
          <span v-if="a.size" class="att-size">{{ humanSize(a.size) }}</span>
        </button>
        <!-- P2.3 — file it on the job. Only offered when the message is
             already linked to a job: without one there's nothing to file it
             ON, and a picker here would duplicate the message-link dialog. -->
        <button
          v-if="linkedJobId"
          type="button"
          class="att-save"
          data-test="att-save-to-job"
          :disabled="savingId === a.id || savedIds.has(a.id)"
          :title="`Save to ${linkedJobLabel || 'the linked job'}`"
          @click="saveToJob(a)"
        >
          <i class="pi" :class="savedIds.has(a.id) ? 'pi-check' : (savingId === a.id ? 'pi-spin pi-spinner' : 'pi-briefcase')" />
          {{ savedIds.has(a.id) ? 'Saved to job' : 'Save to job' }}
        </button>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useApi } from '../composables/useApi'

const props = defineProps({
  messageId: { type: [String, Number], default: null },
  // The list row already knows whether a message has attachments — skip the
  // Graph round-trip entirely when it doesn't.
  hasAttachments: { type: Boolean, default: true },
  // P2.3 — when the message is linked to a job, each attachment can be filed
  // onto that job's documents in one click.
  linkedJobId: { type: [String, Number], default: null },
  linkedJobLabel: { type: String, default: '' },
})

const api = useApi()
const data = ref({})
const downloadingId = ref(null)
const savingId = ref(null)
const savedIds = ref(new Set())

const _NOTES = {
  reconnect_required: 'Reconnect the mailbox to load attachments.',
  message_gone: 'This message is no longer in the mailbox.',
  graph_error: 'Could not load attachments.',
}
const note = computed(() =>
  data.value.fetched === false ? (_NOTES[data.value.reason] ?? '') : '',
)
const visibleAttachments = computed(() =>
  (data.value.attachments || []).filter((a) => !a.is_inline),
)

watch(
  () => props.messageId,
  async (id) => {
    data.value = {}
    downloadingId.value = null
    savingId.value = null
    // Reset per message — "Saved to job" from the last message must not stick
    // to a same-index attachment on the next one.
    savedIds.value = new Set()
    if (!id || !props.hasAttachments) return
    try {
      const res = await api.get(`/api/outlook/messages/${id}/attachments`)
      // Guard against a race on fast message switching.
      if (props.messageId === id) data.value = res || {}
    } catch {
      if (props.messageId === id) data.value = { fetched: false, reason: 'graph_error' }
    }
  },
  { immediate: true },
)

function humanSize(bytes) {
  if (!bytes || bytes < 1024) return `${bytes || 0} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${Math.round(kb)} KB`
  return `${(kb / 1024).toFixed(1)} MB`
}

async function download(att) {
  if (downloadingId.value) return
  downloadingId.value = att.id
  let token = null
  try {
    token = sessionStorage.getItem('gdx_access_token') || null
  } catch { /* private mode */ }
  try {
    const resp = await fetch(
      `/api/outlook/messages/${props.messageId}/attachments/${encodeURIComponent(att.id)}`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} },
    )
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = att.name || 'attachment'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch {
    data.value = { ...data.value, fetched: false, reason: 'graph_error' }
  } finally {
    downloadingId.value = null
  }
}

async function saveToJob(att) {
  if (!props.linkedJobId || savingId.value) return
  savingId.value = att.id
  try {
    const r = await api.post(
      `/api/outlook/messages/${props.messageId}/attachments/${encodeURIComponent(att.id)}/save-to-job`,
      { job_id: props.linkedJobId },
      { successMessage: 'Saved to the job.' },
    )
    // Mark saved whether it was stored now or already there — either way the
    // file IS on the job, which is what the button promised.
    if (r) savedIds.value = new Set([...savedIds.value, att.id])
  } catch {
    // useApi already toasted the failure; keep the button clickable to retry.
  } finally {
    savingId.value = null
  }
}
</script>

<style scoped>
.email-attachments {
  margin: 0.5rem 0;
}
.att-note {
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #64748b);
  padding: 0.25rem 0;
}
.att-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}
.att-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  border: 1px solid var(--p-content-border-color, #cbd5e1);
  background: var(--p-content-background, #fff);
  color: var(--p-text-color, #1e293b);
  border-radius: 999px;
  padding: 0.25rem 0.7rem;
  font-size: 0.8rem;
  cursor: pointer;
  max-width: 100%;
}
.att-chip:hover:not(:disabled) {
  background: var(--p-content-hover-background, #f1f5f9);
}
.att-chip:disabled {
  opacity: 0.6;
  cursor: default;
}
.att-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 16rem;
}
.att-size {
  color: var(--p-text-muted-color, #94a3b8);
  font-size: 0.72rem;
}
.att-item {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
}
.att-save {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  border: 1px dashed var(--p-content-border-color, #cbd5e1);
  background: transparent;
  color: var(--p-text-muted-color, #64748b);
  border-radius: 999px;
  padding: 0.2rem 0.6rem;
  font-size: 0.75rem;
  cursor: pointer;
}
.att-save:hover:not(:disabled) {
  background: var(--p-content-hover-background, #f1f5f9);
  color: var(--p-text-color, #1e293b);
}
.att-save:disabled { opacity: 0.7; cursor: default; }

/* Touch: both chips sit side by side in the mobile detail sheet, where a
   ~24px control next to a ~28px one is a mis-tap waiting to happen. */
@media (pointer: coarse) {
  .att-chip, .att-save {
    min-height: 44px;
    padding-top: 0.5rem;
    padding-bottom: 0.5rem;
  }
}
</style>

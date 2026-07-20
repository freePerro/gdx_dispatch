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
})

const api = useApi()
const data = ref({})
const downloadingId = ref(null)

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
</style>

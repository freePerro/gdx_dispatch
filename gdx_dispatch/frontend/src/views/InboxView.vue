<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { useApi } from '../composables/useApi'
import { formatDateTime as fmtDate } from '../composables/useFormatters'
import Tree from 'primevue/tree'
import ContextMenu from 'primevue/contextmenu'
import Menu from 'primevue/menu'
import Popover from 'primevue/popover'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import TreeSelect from 'primevue/treeselect'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApi()

// ── state ────────────────────────────────────────────────────────────
const folders = ref([])              // flat list from /api/outlook/folders
const selectedFolderId = ref(null)   // graph_folder_id of active folder; null = "All Mail"
const selectedFolderName = ref('All Mail')

const messages = ref([])
const loadingMessages = ref(false)
const loadingFolders = ref(false)
const error = ref(null)

const selectedMsgId = ref(null)
const detail = ref(null)
const detailLoading = ref(false)

const composeMode = ref(null)   // null | 'new' | 'reply'
const composeForm = ref({ to: '', cc: '', subject: '', body: '' })
const composeStatus = ref(null)
const composeSending = ref(false)

// Folder operations state
const ctxMenu = ref(null)        // PrimeVue ContextMenu (right-click)
const folderMenu = ref(null)     // PrimeVue Menu (popup from ⋯ button)
const ctxFolder = ref(null)
const messageMenu = ref(null)    // PrimeVue Menu (popup from message ⋯ button)
const ctxMessage = ref(null)
const colorOverlay = ref(null)
const newFolderDialogOpen = ref(false)
const newFolderName = ref('')
const newFolderParent = ref(null)
const renameDialogOpen = ref(false)
const renameValue = ref('')
const moveMessageDialogOpen = ref(false)
const moveTargetFolderKey = ref(null)
const deleteFolderConfirmOpen = ref(false)
const emptyFolderConfirmOpen = ref(false)

const PRESET_COLORS = [
  { key: null,        name: 'None',   hex: 'transparent' },
  { key: 'red',       name: 'Red',    hex: '#ef4444' },
  { key: 'orange',    name: 'Orange', hex: '#f97316' },
  { key: 'yellow',    name: 'Yellow', hex: '#eab308' },
  { key: 'green',     name: 'Green',  hex: '#22c55e' },
  { key: 'teal',      name: 'Teal',   hex: '#14b8a6' },
  { key: 'blue',      name: 'Blue',   hex: '#3b82f6' },
  { key: 'purple',    name: 'Purple', hex: '#a855f7' },
  { key: 'gray',      name: 'Gray',   hex: '#6b7280' },
]

const SYSTEM_ORDER = ['inbox', 'drafts', 'sentitems', 'archive', 'junkemail', 'deleteditems']
const LIVE_FETCH_FOLDERS = new Set(['junkemail', 'deleteditems'])

const SYSTEM_ICONS = {
  inbox: 'pi pi-inbox',
  drafts: 'pi pi-file-edit',
  sentitems: 'pi pi-send',
  archive: 'pi pi-box',
  junkemail: 'pi pi-ban',
  deleteditems: 'pi pi-trash',
  outbox: 'pi pi-arrow-up-right',
}

// ── computed: folder tree shapes ─────────────────────────────────────

const pinnedFolders = computed(() => folders.value.filter(f => f.pinned))

const systemFolders = computed(() => {
  const sys = folders.value.filter(f => f.is_system && !f.pinned)
  return sys.sort((a, b) => {
    const ai = SYSTEM_ORDER.indexOf(a.well_known_name)
    const bi = SYSTEM_ORDER.indexOf(b.well_known_name)
    return ai - bi
  })
})

const folderIdSet = computed(() => new Set(folders.value.map(f => f.graph_folder_id)))

// "Root" = no parent OR parent isn't another folder we know (i.e., parent
// is the Microsoft msgFolderRoot, which we never cache). Outlook puts
// every user-visible folder under msgFolderRoot, so without this all
// folders would be hidden when their parent_folder_id IS NOT NULL but
// also not in our cache.
const customRootFolders = computed(() =>
  folders.value
    .filter(f => !f.is_system && !f.pinned)
    .filter(f => !f.parent_folder_id || !folderIdSet.value.has(f.parent_folder_id))
    .sort((a, b) => a.display_name.localeCompare(b.display_name))
)

function childrenOf(parentGraphId) {
  return folders.value
    .filter(f => f.parent_folder_id === parentGraphId)
    .sort((a, b) => a.display_name.localeCompare(b.display_name))
}

function folderToTreeNode(f) {
  const kids = childrenOf(f.graph_folder_id).map(folderToTreeNode)
  return {
    key: f.graph_folder_id,
    label: f.display_name,
    icon: f.is_system ? (SYSTEM_ICONS[f.well_known_name] || 'pi pi-folder') : 'pi pi-folder',
    data: f,
    children: kids.length ? kids : undefined,
  }
}

const customTreeNodes = computed(() => customRootFolders.value.map(folderToTreeNode))

// Tree of all folders for the move-to picker (system + custom). Same
// orphan-as-root handling as customRootFolders.
const allFoldersTree = computed(() => {
  const known = folderIdSet.value
  const roots = folders.value.filter(
    f => !f.parent_folder_id || !known.has(f.parent_folder_id),
  )
  return roots
    .sort((a, b) => a.display_name.localeCompare(b.display_name))
    .map(folderToTreeNode)
})

// ── api ──────────────────────────────────────────────────────────────

async function fetchFolders() {
  loadingFolders.value = true
  try {
    folders.value = await api.get('/api/outlook/folders')
  } catch (err) {
    error.value = err.message || 'Failed to load folders'
  } finally {
    loadingFolders.value = false
  }
}

async function fetchMessages(folderId = selectedFolderId.value) {
  loadingMessages.value = true
  error.value = null
  try {
    const url = folderId
      ? `/api/outlook/messages?limit=200&folder_id=${encodeURIComponent(folderId)}`
      : '/api/outlook/messages?limit=200'
    const r = await api.get(url)
    messages.value = Array.isArray(r) ? r : (r.items || [])
  } catch (err) {
    error.value = err.message || 'Failed to load messages'
  } finally {
    loadingMessages.value = false
  }
}

async function selectFolder(folder) {
  selectedFolderId.value = folder?.graph_folder_id || null
  selectedFolderName.value = folder?.display_name || 'All Mail'
  selectedMsgId.value = null
  detail.value = null
  composeMode.value = null
  if (folder?.is_system && LIVE_FETCH_FOLDERS.has(folder.well_known_name)) {
    // Live-fetch path: show a banner + skip DB query
    messages.value = []
    error.value = `${folder.display_name} is shown but not synced. (Live fetch coming in a follow-up slice — open in Outlook for now.)`
    return
  }
  await fetchMessages()
}

async function openMessage(m) {
  selectedMsgId.value = m.id
  detail.value = null
  composeMode.value = null
  composeStatus.value = null
  detailLoading.value = true
  try {
    detail.value = await api.get(`/api/outlook/messages/${m.id}`)
  } catch (err) {
    error.value = err.message || 'Failed to load message'
  } finally {
    detailLoading.value = false
  }
}

// ── compose ──────────────────────────────────────────────────────────

function startNewCompose() {
  selectedMsgId.value = null
  detail.value = null
  composeMode.value = 'new'
  composeStatus.value = null
  composeForm.value = { to: '', cc: '', subject: '', body: '' }
}

function startReply() {
  if (!detail.value) return
  composeMode.value = 'reply'
  composeStatus.value = null
  const subj = detail.value.subject || ''
  const replySubj = subj.toLowerCase().startsWith('re:') ? subj : `Re: ${subj}`
  const quoted = `\n\n---\nOn ${fmtDate(detail.value.sent_at || detail.value.received_at)}, ${detail.value.from_address || ''} wrote:\n${(detail.value.body_preview || '').split('\n').map(l => `> ${l}`).join('\n')}`
  composeForm.value = {
    to: detail.value.from_address || '',
    cc: '',
    subject: replySubj,
    body: quoted,
  }
}

function cancelCompose() {
  composeMode.value = null
  composeStatus.value = null
}

function splitAddrs(s) {
  return (s || '').split(/[,;]/).map(x => x.trim()).filter(Boolean)
}

async function sendCompose() {
  const form = composeForm.value
  if (!form.to.trim() || !form.subject.trim() || !form.body.trim()) {
    composeStatus.value = { ok: false, message: 'To, subject, and body are required.' }
    return
  }
  composeSending.value = true
  composeStatus.value = null
  try {
    const payload = {
      to: splitAddrs(form.to),
      subject: form.subject,
      body_html: form.body.replace(/\n/g, '<br>'),
    }
    const cc = splitAddrs(form.cc)
    if (cc.length) payload.cc = cc
    if (composeMode.value === 'reply' && detail.value?.id) {
      payload.in_reply_to = detail.value.id
    }
    const r = await api.post('/api/outlook/send', payload)
    composeStatus.value = { ok: !!r.ok, message: r.ok ? 'Sent.' : (r.detail || 'Send failed') }
    if (r.ok) {
      composeForm.value = { to: '', cc: '', subject: '', body: '' }
      composeMode.value = null
      await fetchMessages()
    }
  } catch (err) {
    composeStatus.value = { ok: false, message: err.message || 'Send failed' }
  } finally {
    composeSending.value = false
  }
}

// ── folder ops ───────────────────────────────────────────────────────

function showContextMenu(event, folder) {
  ctxFolder.value = folder
  ctxMenu.value?.show(event)
}

function toggleFolderMenu(event, folder) {
  event.stopPropagation()  // don't trigger folder selection
  ctxFolder.value = folder
  folderMenu.value?.toggle(event)
}

function toggleMessageMenu(event, msg) {
  event.stopPropagation()  // don't open the message
  ctxMessage.value = msg
  messageMenu.value?.toggle(event)
}

const messageMenuModel = computed(() => {
  const m = ctxMessage.value
  if (!m) return []
  return [
    {
      label: m.is_read ? 'Mark as unread' : 'Mark as read',
      icon: m.is_read ? 'pi pi-circle' : 'pi pi-check',
      command: () => toggleMessageRead(m),
    },
    { label: 'Move to folder…', icon: 'pi pi-folder-open', command: () => promptMoveMessageFor(m) },
    { separator: true },
    { label: 'Delete', icon: 'pi pi-trash', command: () => deleteMessage(m) },
  ]
})

async function toggleMessageRead(msg) {
  const want = !msg.is_read
  try {
    await api.patch(`/api/outlook/messages/${msg.id}/read`, { is_read: want })
    msg.is_read = want
  } catch (err) {
    error.value = err.message || 'Failed to toggle read state'
  }
}

function promptMoveMessageFor(msg) {
  // Open the existing move dialog targeting this msg without requiring
  // detail pane to be open.
  detail.value = msg
  selectedMsgId.value = msg.id
  promptMoveMessage()
}

async function deleteMessage(msg) {
  // Move-to-DeletedItems via the Graph move endpoint. Find the
  // DeletedItems folder id from our cache.
  const trash = folders.value.find(f => f.well_known_name === 'deleteditems')
  if (!trash) {
    error.value = 'Cannot delete: Deleted Items folder not found in cache.'
    return
  }
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete "${msg.subject || '(no subject)'}" — moves it to Deleted Items.` }))) return
  try {
    await api.post(`/api/outlook/messages/${msg.id}/move`, {
      destination_folder_id: trash.graph_folder_id,
    })
    messages.value = messages.value.filter(x => x.id !== msg.id)
    if (selectedMsgId.value === msg.id) {
      detail.value = null
      selectedMsgId.value = null
    }
    await fetchFolders()
  } catch (err) {
    error.value = err.message || 'Failed to delete message'
  }
}

const contextMenuModel = computed(() => {
  const f = ctxFolder.value
  if (!f) return []
  const isSystem = !!f.is_system
  return [
    { label: f.pinned ? 'Unpin' : 'Pin', icon: 'pi pi-bookmark', command: () => togglePinned(f) },
    { label: 'Set color', icon: 'pi pi-palette', command: (e) => colorOverlay.value?.toggle(e.originalEvent) },
    { separator: true },
    { label: 'New subfolder', icon: 'pi pi-plus', command: () => promptNewFolder(f) },
    { label: 'Rename', icon: 'pi pi-pencil', disabled: isSystem, command: () => promptRename(f) },
    { label: 'Mark all read', icon: 'pi pi-check', command: () => markAllRead(f) },
    { separator: true },
    { label: 'Empty folder', icon: 'pi pi-eraser', command: () => { ctxFolder.value = f; emptyFolderConfirmOpen.value = true } },
    { label: 'Delete folder', icon: 'pi pi-trash', disabled: isSystem, command: () => { ctxFolder.value = f; deleteFolderConfirmOpen.value = true } },
  ]
})

async function togglePinned(folder) {
  try {
    const updated = await api.patch(`/api/outlook/folders/${folder.graph_folder_id}`, {
      pinned: !folder.pinned,
    })
    Object.assign(folder, updated)
  } catch (err) {
    error.value = err.message || 'Failed to update folder'
  }
}

async function setColor(colorKey) {
  if (!ctxFolder.value) return
  try {
    const updated = await api.patch(`/api/outlook/folders/${ctxFolder.value.graph_folder_id}`, {
      color: colorKey,
    })
    Object.assign(ctxFolder.value, updated)
  } catch (err) {
    error.value = err.message || 'Failed to set color'
  } finally {
    colorOverlay.value?.hide()
  }
}

function promptNewFolder(parent) {
  newFolderParent.value = parent || null
  newFolderName.value = ''
  newFolderDialogOpen.value = true
}

async function createFolder() {
  if (!newFolderName.value.trim()) return
  try {
    const created = await api.post('/api/outlook/folders', {
      display_name: newFolderName.value.trim(),
      parent_folder_id: newFolderParent.value?.graph_folder_id || null,
    })
    folders.value.push(created)
    newFolderDialogOpen.value = false
  } catch (err) {
    error.value = err.message || 'Failed to create folder'
  }
}

function promptRename(folder) {
  ctxFolder.value = folder
  renameValue.value = folder.display_name
  renameDialogOpen.value = true
}

async function renameFolder() {
  if (!ctxFolder.value || !renameValue.value.trim()) return
  try {
    const updated = await api.patch(`/api/outlook/folders/${ctxFolder.value.graph_folder_id}`, {
      display_name: renameValue.value.trim(),
    })
    Object.assign(ctxFolder.value, updated)
    renameDialogOpen.value = false
  } catch (err) {
    error.value = err.message || 'Failed to rename folder'
  }
}

async function deleteFolder() {
  if (!ctxFolder.value) return
  try {
    await api.del(`/api/outlook/folders/${ctxFolder.value.graph_folder_id}`)
    folders.value = folders.value.filter(f => f.graph_folder_id !== ctxFolder.value.graph_folder_id)
    if (selectedFolderId.value === ctxFolder.value.graph_folder_id) {
      await selectFolder(null)
    }
    deleteFolderConfirmOpen.value = false
  } catch (err) {
    error.value = err.message || 'Failed to delete folder'
  }
}

async function emptyFolder() {
  if (!ctxFolder.value) return
  try {
    await api.post(`/api/outlook/folders/${ctxFolder.value.graph_folder_id}/empty`)
    if (selectedFolderId.value === ctxFolder.value.graph_folder_id) {
      messages.value = []
    }
    emptyFolderConfirmOpen.value = false
    await fetchFolders()  // refresh counts
  } catch (err) {
    error.value = err.message || 'Failed to empty folder'
  }
}

async function markAllRead(folder) {
  try {
    await api.post(`/api/outlook/folders/${folder.graph_folder_id}/mark-all-read`)
    if (selectedFolderId.value === folder.graph_folder_id) {
      messages.value = messages.value.map(m => ({ ...m, is_read: true }))
    }
    await fetchFolders()
  } catch (err) {
    error.value = err.message || 'Failed to mark folder read'
  }
}

// ── move message ─────────────────────────────────────────────────────

function promptMoveMessage() {
  moveTargetFolderKey.value = null
  moveMessageDialogOpen.value = true
}

async function moveMessage() {
  if (!detail.value || !moveTargetFolderKey.value) return
  // TreeSelect provides a {key: true} object; extract the first key.
  const destId = typeof moveTargetFolderKey.value === 'object'
    ? Object.keys(moveTargetFolderKey.value)[0]
    : moveTargetFolderKey.value
  if (!destId) return
  try {
    await api.post(`/api/outlook/messages/${detail.value.id}/move`, {
      destination_folder_id: destId,
    })
    moveMessageDialogOpen.value = false
    detail.value = null
    selectedMsgId.value = null
    await fetchMessages()
    await fetchFolders()
  } catch (err) {
    error.value = err.message || 'Failed to move message'
  }
}

// ── helpers ──────────────────────────────────────────────────────────

function colorHexFor(folder) {
  const c = PRESET_COLORS.find(p => p.key === folder.color)
  return c ? c.hex : 'transparent'
}

const sortedMessages = computed(() =>
  [...messages.value].sort((a, b) => {
    const ta = Date.parse(a.received_at || a.sent_at || 0) || 0
    const tb = Date.parse(b.received_at || b.sent_at || 0) || 0
    return tb - ta
  }),
)

// ── lifecycle ────────────────────────────────────────────────────────

onMounted(async () => {
  await fetchFolders()
  // Default to Inbox if present
  const inbox = folders.value.find(f => f.well_known_name === 'inbox')
  if (inbox) {
    await selectFolder(inbox)
  } else {
    await fetchMessages()
  }
})
</script>

<template>
  <div class="inbox-view view-card">
    <div class="inbox-header">
      <h1>{{ selectedFolderName }}</h1>
      <div class="header-actions">
        <Button label="New" icon="pi pi-pencil" data-test="inbox-new" size="small" @click="startNewCompose" />
        <Button label="New folder" icon="pi pi-folder-plus" outlined size="small" @click="promptNewFolder(null)" />
        <Button v-tooltip="'Refresh'" icon="pi pi-refresh" outlined size="small" aria-label="Refresh" @click="fetchFolders().then(() => fetchMessages())" />
      </div>
    </div>

    <div v-if="error" class="status-error">{{ error }}</div>

    <div class="inbox-layout">
      <!-- ── folder rail ── -->
      <aside class="folder-rail" data-test="folder-rail">
        <div v-if="loadingFolders" class="muted center">Loading folders…</div>

        <div v-if="pinnedFolders.length" class="rail-section">
          <h3 class="rail-section-title">Favorites</h3>
          <button
            v-for="f in pinnedFolders"
            :key="f.id"
            class="folder-row"
            :class="{ active: selectedFolderId === f.graph_folder_id }"
            data-test="folder-row"
            @click="selectFolder(f)"
            @contextmenu.prevent="showContextMenu($event, f)"
          >
            <span class="color-dot" :style="{ background: colorHexFor(f) }" />
            <i :class="f.well_known_name && SYSTEM_ICONS[f.well_known_name] || 'pi pi-folder'" />
            <span class="folder-name">{{ f.display_name }}</span>
            <span v-if="f.unread_count" class="unread-badge">{{ f.unread_count }}</span>
            <span v-tooltip="'Folder actions'" class="folder-menu-trigger" data-test="folder-menu-trigger" aria-label="Folder actions" @click="toggleFolderMenu($event, f)">⋯</span>
          </button>
        </div>

        <div v-if="systemFolders.length" class="rail-section">
          <h3 class="rail-section-title">System</h3>
          <button
            v-for="f in systemFolders"
            :key="f.id"
            class="folder-row"
            :class="{ active: selectedFolderId === f.graph_folder_id }"
            data-test="folder-row"
            @click="selectFolder(f)"
            @contextmenu.prevent="showContextMenu($event, f)"
          >
            <span class="color-dot" :style="{ background: colorHexFor(f) }" />
            <i :class="SYSTEM_ICONS[f.well_known_name] || 'pi pi-folder'" />
            <span class="folder-name">{{ f.display_name }}</span>
            <span v-if="f.unread_count" class="unread-badge">{{ f.unread_count }}</span>
            <span v-tooltip="'Folder actions'" class="folder-menu-trigger" data-test="folder-menu-trigger" aria-label="Folder actions" @click="toggleFolderMenu($event, f)">⋯</span>
          </button>
        </div>

        <div v-if="customTreeNodes.length" class="rail-section">
          <h3 class="rail-section-title">Folders</h3>
          <Tree
            :value="customTreeNodes"
            :selectionKeys="{ [selectedFolderId]: true }"
            selectionMode="single"
            class="folder-tree"
            data-test="folder-tree"
            @nodeSelect="(n) => selectFolder(n.data)"
          >
            <template #default="{ node }">
              <span
                class="tree-node-row"
                @contextmenu.prevent="showContextMenu($event, node.data)"
              >
                <span class="color-dot" :style="{ background: colorHexFor(node.data) }" />
                <span class="folder-name">{{ node.label }}</span>
                <span v-if="node.data.unread_count" class="unread-badge">{{ node.data.unread_count }}</span>
                <span v-tooltip="'Folder actions'" class="folder-menu-trigger" data-test="folder-menu-trigger" aria-label="Folder actions" @click.stop="toggleFolderMenu($event, node.data)">⋯</span>
              </span>
            </template>
          </Tree>
        </div>
      </aside>

      <!-- ── message list ── -->
      <div class="msg-list" data-test="inbox-list">
        <div v-if="loadingMessages" class="muted center">Loading…</div>
        <div v-else-if="sortedMessages.length === 0 && folders.length === 0" class="muted center" style="padding:1rem;text-align:center">
          <p style="margin:0 0 0.5rem">No mailbox connected.</p>
          <router-link to="/settings" style="color: var(--p-primary-color)">Connect Outlook in Settings → Integrations</router-link>
        </div>
        <div v-else-if="sortedMessages.length === 0" class="muted center">No messages.</div>
        <button
          v-for="m in sortedMessages"
          :key="m.id"
          class="msg-row"
          :class="{ active: selectedMsgId === m.id, unread: !m.is_read }"
          data-test="inbox-row"
          @click="openMessage(m)"
        >
          <div class="row-top">
            <span class="row-from">{{ m.from_address || '(no sender)' }}</span>
            <span class="row-when muted">{{ fmtDate(m.received_at || m.sent_at) }}</span>
            <span v-tooltip="'Message actions'" class="msg-menu-trigger" data-test="msg-menu-trigger" aria-label="Message actions" @click="toggleMessageMenu($event, m)">⋯</span>
          </div>
          <div class="row-subject">{{ m.subject || '(no subject)' }}</div>
          <div class="row-preview muted">{{ m.body_preview || '(no preview)' }}</div>
        </button>
      </div>

      <!-- ── compose pane ── -->
      <div class="msg-pane" v-if="composeMode" data-test="inbox-compose">
        <div class="pane-header">
          <h2>{{ composeMode === 'reply' ? 'Reply' : 'New message' }}</h2>
          <button class="btn-link" @click="cancelCompose">✕</button>
        </div>
        <div class="compose-fields">
          <label>To<input v-model="composeForm.to" data-test="compose-to" placeholder="name@example.com" /></label>
          <label>Cc<input v-model="composeForm.cc" data-test="compose-cc" placeholder="optional" /></label>
          <label>Subject<input v-model="composeForm.subject" data-test="compose-subject" /></label>
          <label class="body-label">
            Body
            <textarea v-model="composeForm.body" data-test="compose-body" rows="14" />
          </label>
        </div>
        <div class="compose-actions">
          <Button :disabled="composeSending" data-test="compose-send" @click="sendCompose">
            {{ composeSending ? 'Sending…' : 'Send' }}
          </Button>
          <Button outlined @click="cancelCompose">Cancel</Button>
          <span v-if="composeStatus" :class="['compose-status', composeStatus.ok ? 'ok' : 'err']">
            {{ composeStatus.message }}
          </span>
        </div>
      </div>

      <!-- ── detail pane ── -->
      <div class="msg-pane" v-else-if="detail" data-test="inbox-detail">
        <div class="pane-header">
          <h2>{{ detail.subject || '(no subject)' }}</h2>
          <button class="btn-link" @click="() => { detail = null; selectedMsgId = null; }">✕</button>
        </div>
        <div class="detail-meta">
          <div><span class="muted">From:</span> {{ detail.from_address }}</div>
          <div><span class="muted">To:</span> {{ (detail.to_addresses || []).join(', ') }}</div>
          <div v-if="detail.cc_addresses?.length"><span class="muted">Cc:</span> {{ detail.cc_addresses.join(', ') }}</div>
          <div><span class="muted">Date:</span> {{ fmtDate(detail.sent_at || detail.received_at) }}</div>
          <div v-if="detail.has_attachments" class="muted">📎 Has attachments</div>
        </div>
        <div class="detail-body">
          <pre>{{ detail.body_preview || '(no body preview available)' }}</pre>
        </div>
        <div class="detail-actions">
          <Button label="Reply" icon="pi pi-reply" data-test="inbox-reply" @click="startReply" />
          <Button label="Move" icon="pi pi-folder-open" outlined data-test="inbox-move" @click="promptMoveMessage" />
        </div>
      </div>

      <div class="msg-pane empty" v-else>
        <div class="muted center">
          <p v-if="detailLoading">Loading…</p>
          <p v-else>Select a message to read.</p>
        </div>
      </div>
    </div>

    <!-- ── ContextMenu (folder right-click) ── -->
    <ContextMenu ref="ctxMenu" :model="contextMenuModel" data-test="folder-ctx-menu" />

    <!-- ── Popup Menu (⋯ button on folder rows) ── -->
    <Menu ref="folderMenu" :model="contextMenuModel" :popup="true" data-test="folder-popup-menu" />

    <!-- ── Popup Menu (⋯ button on message rows) ── -->
    <Menu ref="messageMenu" :model="messageMenuModel" :popup="true" data-test="msg-popup-menu" />

    <!-- ── Color picker overlay ── -->
    <Popover ref="colorOverlay" data-test="color-overlay">
      <div class="color-picker">
        <button
          v-for="c in PRESET_COLORS"
          :key="c.name"
          class="color-swatch"
          :style="{ background: c.hex }"
          v-tooltip="c.name"
          :aria-label="c.name"
          @click="setColor(c.key)"
        >
          <i v-if="c.key === null" class="pi pi-times" />
        </button>
      </div>
    </Popover>

    <!-- ── New folder dialog ── -->
    <Dialog v-model:visible="newFolderDialogOpen" header="New folder" modal :style="{ width: '24rem' }">
      <p class="muted" v-if="newFolderParent">Under: {{ newFolderParent.display_name }}</p>
      <InputText v-model="newFolderName" placeholder="Folder name" autofocus class="w-full" data-test="new-folder-name" />
      <template #footer>
        <Button label="Cancel" outlined @click="newFolderDialogOpen = false" />
        <Button label="Create" data-test="new-folder-create" @click="createFolder" />
      </template>
    </Dialog>

    <!-- ── Rename folder dialog ── -->
    <Dialog v-model:visible="renameDialogOpen" header="Rename folder" modal :style="{ width: '24rem' }">
      <InputText v-model="renameValue" autofocus class="w-full" data-test="rename-folder-input" />
      <template #footer>
        <Button label="Cancel" outlined @click="renameDialogOpen = false" />
        <Button label="Rename" data-test="rename-folder-save" @click="renameFolder" />
      </template>
    </Dialog>

    <!-- ── Delete folder confirm ── -->
    <Dialog v-model:visible="deleteFolderConfirmOpen" header="Delete folder" modal :style="{ width: '24rem' }">
      <p>Delete <strong>{{ ctxFolder?.display_name }}</strong>? Messages in this folder will be removed locally. Microsoft moves the folder to Recoverable Items for 30 days.</p>
      <template #footer>
        <Button label="Cancel" outlined @click="deleteFolderConfirmOpen = false" />
        <Button label="Delete" severity="danger" data-test="delete-folder-confirm" @click="deleteFolder" />
      </template>
    </Dialog>

    <!-- ── Empty folder confirm ── -->
    <Dialog v-model:visible="emptyFolderConfirmOpen" header="Empty folder" modal :style="{ width: '26rem' }">
      <p>Delete every message in <strong>{{ ctxFolder?.display_name }}</strong>? This cannot be undone from inside GDX.</p>
      <template #footer>
        <Button label="Cancel" outlined @click="emptyFolderConfirmOpen = false" />
        <Button label="Empty" severity="danger" data-test="empty-folder-confirm" @click="emptyFolder" />
      </template>
    </Dialog>

    <!-- ── Move message dialog ── -->
    <Dialog v-model:visible="moveMessageDialogOpen" header="Move to folder" modal :style="{ width: '28rem' }">
      <TreeSelect
        v-model="moveTargetFolderKey"
        :options="allFoldersTree"
        placeholder="Choose a folder…"
        class="w-full"
        data-test="move-target"
      />
      <template #footer>
        <Button label="Cancel" outlined @click="moveMessageDialogOpen = false" />
        <Button label="Move" :disabled="!moveTargetFolderKey" data-test="move-confirm" @click="moveMessage" />
      </template>
    </Dialog>
  </div>
</template>

<style scoped>
.inbox-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  height: 100%;
  min-height: 0;
  color: var(--text-primary);
}
.inbox-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.header-actions { display: flex; gap: 0.5rem; }

.inbox-layout {
  display: grid;
  grid-template-columns: 240px minmax(280px, 1fr) 2fr;
  gap: 1rem;
  flex: 1;
  min-height: 0;
}

/* ── folder rail ── */
.folder-rail {
  background: var(--surface-card);
  border: 1px solid var(--surface-border);
  border-radius: 8px;
  overflow-y: auto;
  padding: 0.5rem 0;
  display: flex;
  flex-direction: column;
}
.rail-section { margin-bottom: 0.75rem; }
.rail-section-title {
  font-size: 0.7rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-secondary);
  padding: 0.25rem 0.75rem;
  margin: 0;
}
.folder-row {
  background: transparent;
  border: none;
  width: 100%;
  text-align: left;
  padding: 0.4rem 0.75rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: var(--text-primary);
  cursor: pointer;
  font-size: 0.9rem;
}
.folder-row:hover { background: var(--surface-hover); }
.folder-row.active { background: var(--surface-selected); font-weight: 600; }
.folder-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.unread-badge {
  background: var(--p-primary-color);
  color: #fff;
  border-radius: 10px;
  font-size: 0.7rem;
  padding: 0 0.4rem;
  min-width: 1.2rem;
  text-align: center;
}
.color-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
  border: 1px solid var(--surface-border);
}
.folder-menu-trigger {
  margin-left: auto;
  padding: 0 0.4rem;
  font-size: 1.1rem;
  line-height: 1;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 4px;
  opacity: 0.5;
  user-select: none;
}
.folder-menu-trigger:hover {
  opacity: 1;
  background: var(--surface-hover);
  color: var(--text-primary);
}
.folder-row:hover .folder-menu-trigger,
.folder-row.active .folder-menu-trigger,
.tree-node-row:hover .folder-menu-trigger {
  opacity: 0.85;
}
.msg-menu-trigger {
  padding: 0 0.4rem;
  font-size: 1rem;
  line-height: 1;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 4px;
  opacity: 0.5;
  user-select: none;
  margin-left: 0.4rem;
}
.msg-menu-trigger:hover {
  opacity: 1;
  background: var(--surface-hover);
  color: var(--text-primary);
}
.msg-row:hover .msg-menu-trigger,
.msg-row.active .msg-menu-trigger {
  opacity: 0.8;
}

.folder-tree :deep(.p-tree) {
  background: transparent;
  border: none;
  padding: 0;
}
.folder-tree :deep(.p-treenode-content) {
  padding: 0.3rem 0.5rem;
  border-radius: 0;
}
.folder-tree :deep(.p-treenode-content.p-highlight) {
  background: var(--surface-selected);
}
.tree-node-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex: 1;
  font-size: 0.9rem;
}

/* ── message list ── */
.msg-list {
  background: var(--surface-card);
  border: 1px solid var(--surface-border);
  border-radius: 8px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.msg-row {
  text-align: left;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--surface-border);
  padding: 0.6rem 0.75rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  color: var(--text-primary);
}
.msg-row:hover { background: var(--surface-hover); }
.msg-row.active { background: var(--surface-selected); }
.msg-row.unread .row-subject { font-weight: 700; }
.row-top {
  display: flex;
  justify-content: space-between;
  font-size: 0.85rem;
}
.row-from { font-weight: 600; }
.row-when { font-size: 0.75rem; }
.row-subject { font-size: 0.95rem; }
.row-preview {
  font-size: 0.8rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── detail/compose pane ── */
.msg-pane {
  background: var(--surface-card);
  border: 1px solid var(--surface-border);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.msg-pane.empty { align-items: center; justify-content: center; }
.pane-header {
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--surface-border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.pane-header h2 { margin: 0; font-size: 1rem; }
.btn-link {
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 1.25rem;
  color: var(--text-secondary);
}
.detail-meta {
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--surface-border);
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.85rem;
}
.detail-body {
  flex: 1;
  overflow-y: auto;
  padding: 1rem;
}
.detail-body pre {
  white-space: pre-wrap;
  word-wrap: break-word;
  font-family: inherit;
  margin: 0;
}
.detail-actions {
  padding: 0.75rem 1rem;
  border-top: 1px solid var(--surface-border);
  display: flex;
  gap: 0.5rem;
}
.compose-fields {
  flex: 1;
  overflow-y: auto;
  padding: 0.75rem 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.compose-fields label {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.85rem;
  color: var(--text-secondary);
}
.compose-fields input,
.compose-fields textarea {
  border: 1px solid var(--surface-border);
  border-radius: 6px;
  padding: 0.5rem;
  background: var(--surface-input, var(--surface-card));
  color: var(--text-primary);
  font-family: inherit;
}
.body-label { flex: 1; }
.body-label textarea { flex: 1; min-height: 200px; resize: vertical; }
.compose-actions {
  padding: 0.75rem 1rem;
  border-top: 1px solid var(--surface-border);
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
.compose-status.ok { color: var(--color-success-500, #065f46); }
.compose-status.err { color: var(--color-danger-500, #b91c1c); }

/* ── color picker ── */
.color-picker {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 0.4rem;
  padding: 0.5rem;
}
.color-swatch {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: 1px solid var(--surface-border);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
.color-swatch:hover { transform: scale(1.1); }

.muted { color: var(--text-secondary); }
.center { text-align: center; padding: 2rem; }
.status-error { color: var(--color-danger-500, #b91c1c); }
</style>

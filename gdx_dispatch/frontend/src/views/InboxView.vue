<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useAuthStore } from '../stores/auth'
import { useToast } from 'primevue/usetoast'
import { formatDateTime as fmtDate } from '../composables/useFormatters'
import Tree from 'primevue/tree'
import ContextMenu from 'primevue/contextmenu'
import Menu from 'primevue/menu'
import Popover from 'primevue/popover'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import TreeSelect from 'primevue/treeselect'
import EmailBodyFrame from '../components/EmailBodyFrame.vue'
import EmailAttachments from '../components/EmailAttachments.vue'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApi()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const toast = useToast()

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
// D1 — full body is live-fetched separately from metadata so the pane paints
// instantly with the preview, then swaps in the real body when it lands.
const bodyData = ref({})
const bodyLoading = ref(false)
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
// Honor the server's content_type (a fetched PLAIN-TEXT body still populates
// body_html, so deriving type from "has body_html" would render text as HTML).
// Fallback path (no fetch → showing preview) is plain text.
const bodyFrameType = computed(() =>
  bodyData.value.fetched && bodyData.value.content_type
    ? bodyData.value.content_type
    : 'text',
)

const composeMode = ref(null)   // null | 'new' | 'reply'
const composeForm = ref({ to: '', cc: '', subject: '', body: '' })
const composeStatus = ref(null)
const composeSending = ref(false)
// P2.5 — the job this outbound message is about. Sending with it stamps a
// `[Job #<uuid>]` marker on the subject, which is what makes the customer's
// REPLY auto-link back to the job (tagger job_thread strategy). Set from the
// message being replied to, or from ?job_id= when composing from a job page.
const composeJobId = ref(null)
const composeJobLabel = ref('')
// "Reply by email" from the Leads page (?lead_id= / ?landing_lead_id=).
// The SEND is the contact event — on success we call record-contact on the
// lead, never at button-click time (a click is not an outreach fact). The
// armed address is kept so a composer the user re-purposed for someone
// else entirely doesn't stamp the lead. The server side is the real guard:
// record-contact only ever moves new → contacted, so a stale replay can't
// downgrade anything.
const composeLeadId = ref(null)
const composeLandingLeadId = ref(null)
const composeLeadEmail = ref('')

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

// Sync-health banner. The 2026-07-30 outage ran five days before anyone
// noticed the inbox had gone quiet — this tells the office the moment sync
// breaks, right where they're already looking. Best-effort: a failure here
// must never take the inbox down.
const syncHealth = ref(null)

async function fetchSyncHealth() {
  try {
    syncHealth.value = await api.get('/api/outlook/sync-health')
  } catch {
    syncHealth.value = null
  }
}

const MSG_PAGE_SIZE = 50
const msgOffset = ref(0)
const hasMoreMessages = ref(false)

// 1.1 — search runs SERVER-side (the whole mailbox), not over the loaded page.
// Filtering only what's on screen would look like search and silently miss
// every message past the first 50.
const searchTerm = ref('')
const activeSearch = ref('')
let _searchTimer = null

function onSearchInput() {
  clearTimeout(_searchTimer)
  _searchTimer = setTimeout(() => {
    activeSearch.value = searchTerm.value.trim()
    fetchMessages()
  }, 300)
}

function clearSearch() {
  clearTimeout(_searchTimer)
  searchTerm.value = ''
  activeSearch.value = ''
  fetchMessages()
}

async function fetchMessages(folderId = selectedFolderId.value, { append = false } = {}) {
  loadingMessages.value = true
  error.value = null
  try {
    // Page over RAW rows (offset), not visible count — the server filters
    // visibility after the window, so a page may be short; has_more/next_offset
    // let us keep loading until every message is reachable (D7).
    const offset = append ? msgOffset.value : 0
    let base = `/api/outlook/messages?limit=${MSG_PAGE_SIZE}&offset=${offset}`
    // An active search spans ALL folders. The box sits above the folder rail
    // and reads as "search my mail" — scoping it to whichever folder happens
    // to be selected (Inbox, on mount) silently hides every hit in Archive.
    if (activeSearch.value) base += `&q=${encodeURIComponent(activeSearch.value)}`
    const url = folderId && !activeSearch.value
      ? `${base}&folder_id=${encodeURIComponent(folderId)}`
      : base
    const r = await api.get(url)
    const items = Array.isArray(r) ? r : (r.items || [])
    if (append) {
      // Dedupe by id — offset pages can overlap if mail arrived between calls.
      const seen = new Set(messages.value.map((m) => m.id))
      messages.value = [...messages.value, ...items.filter((m) => !seen.has(m.id))]
    } else {
      messages.value = items
    }
    msgOffset.value = (r && typeof r.next_offset === 'number') ? r.next_offset : offset + MSG_PAGE_SIZE
    hasMoreMessages.value = !!(r && r.has_more)
  } catch (err) {
    error.value = err.message || 'Failed to load messages'
  } finally {
    loadingMessages.value = false
  }
}

function loadMoreMessages() {
  return fetchMessages(selectedFolderId.value, { append: true })
}

async function selectFolder(folder) {
  selectedFolderId.value = folder?.graph_folder_id || null
  selectedFolderName.value = folder?.display_name || 'All Mail'
  selectedMsgId.value = null
  detail.value = null
  composeMode.value = null
  if (folder?.is_system && LIVE_FETCH_FOLDERS.has(folder.well_known_name)) {
    // Live-fetch path: show a banner + skip DB query. Reset pagination too, or
    // a stale "Load more" from the previous folder would render under the
    // banner and, on click, query the DB folder this path deliberately skips.
    messages.value = []
    hasMoreMessages.value = false
    msgOffset.value = 0
    error.value = `${folder.display_name} is shown but not synced. (Live fetch coming in a follow-up slice — open in Outlook for now.)`
    return
  }
  await fetchMessages()
}

async function openMessage(m) {
  selectedMsgId.value = m.id
  detail.value = null
  bodyData.value = {}
  thread.value = []
  composeMode.value = null
  composeStatus.value = null
  detailLoading.value = true
  try {
    const meta = await api.get(`/api/outlook/messages/${m.id}`)
    // Guard the race: a fast second click must not let message A's metadata
    // overwrite the pane the user has already moved to.
    if (selectedMsgId.value !== m.id) return
    detail.value = meta
  } catch (err) {
    if (selectedMsgId.value === m.id) error.value = err.message || 'Failed to load message'
  } finally {
    if (selectedMsgId.value === m.id) detailLoading.value = false
  }
  if (detail.value && selectedMsgId.value === m.id) {
    loadBody(m.id)
    loadThread(m.id)
  }
}

// ── 1.3 conversation ─────────────────────────────────────────────────
// The thread comes from the SERVER (every sibling re-checked against the
// visibility chokepoint), not from grouping the loaded page — otherwise the
// "3 messages" count would mean "3 of the ones that happen to be on screen".
const thread = ref([])

async function loadThread(id) {
  try {
    const rows = await api.get(`/api/outlook/messages/${id}/thread`)
    if (selectedMsgId.value === id) thread.value = Array.isArray(rows) ? rows : []
  } catch {
    if (selectedMsgId.value === id) thread.value = []
  }
}

// Only worth showing when there IS a conversation — a lone message renders
// as a one-row strip otherwise, which is just noise.
const threadOthers = computed(() =>
  thread.value.filter((m) => m.id !== selectedMsgId.value),
)

async function loadBody(id) {
  bodyLoading.value = true
  try {
    const data = await api.get(`/api/outlook/messages/${id}/body`)
    // Guard against a race: user may have opened another message meanwhile.
    if (selectedMsgId.value === id) bodyData.value = data || {}
  } catch {
    // Non-fatal: the pane already shows the preview from `detail`.
    if (selectedMsgId.value === id) bodyData.value = { fetched: false, reason: 'graph_error' }
  } finally {
    bodyLoading.value = false
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

// ── compose ──────────────────────────────────────────────────────────

function startNewCompose() {
  selectedMsgId.value = null
  detail.value = null
  composeMode.value = 'new'
  composeStatus.value = null
  composeJobId.value = null
  composeJobLabel.value = ''
  composeLeadId.value = null
  composeLandingLeadId.value = null
  composeLeadEmail.value = ''
  draftSavedFingerprint.value = null
  draftWebLink.value = null
  composeForm.value = { to: '', cc: '', subject: '', body: '' }
}

function quotedBody() {
  const d = detail.value
  return `\n\n---\nOn ${fmtDate(d.sent_at || d.received_at)}, ${d.from_address || ''} wrote:\n${(d.body_preview || '').split('\n').map(l => `> ${l}`).join('\n')}`
}

function replySubject() {
  const subj = detail.value.subject || ''
  return subj.toLowerCase().startsWith('re:') ? subj : `Re: ${subj}`
}

function startReply() {
  if (!detail.value) return
  composeMode.value = 'reply'
  composeStatus.value = null
  composeJobId.value = detail.value.linked_job_id || null
  composeJobLabel.value = detail.value.linked_job_label || ''
  composeLeadId.value = null
  composeLandingLeadId.value = null
  composeLeadEmail.value = ''
  composeForm.value = {
    to: detail.value.from_address || '',
    cc: '',
    subject: replySubject(),
    body: quotedBody(),
  }
}

// 1.4 — reply-all: original sender + everyone on To/Cc, minus this mailbox.
// The self-drop is load-bearing: on a shared office inbox the mailbox's OWN
// address is in the original To/Cc, so without dropping it every reply-all
// mails the inbox back into itself and the thread doubles each round-trip.
// The server tells us that address (mailbox_address, the account's UPN) —
// it can't be inferred client-side from the recipient list.
function startReplyAll() {
  if (!detail.value) return
  const d = detail.value
  const mine = new Set(
    [d.to_addresses, d.cc_addresses]
      .flat()
      .filter(Boolean)
      .map((a) => String(a).toLowerCase()),
  )
  const from = (d.from_address || '').toLowerCase()
  const self = (d.mailbox_address || '').toLowerCase()
  const cc = [...mine].filter((a) => a && a !== from && a !== self)
  composeMode.value = 'reply'
  composeStatus.value = null
  composeJobId.value = d.linked_job_id || null
  composeJobLabel.value = d.linked_job_label || ''
  composeLeadId.value = null
  composeLandingLeadId.value = null
  composeLeadEmail.value = ''
  composeForm.value = {
    to: d.from_address || '',
    cc: cc.join(', '),
    subject: replySubject(),
    body: quotedBody(),
  }
}

// 1.4 — forward goes through Graph's own /forward action (server side), which
// carries the ORIGINAL ATTACHMENTS. That's why this is its own dialog instead
// of a pre-filled compose: a compose-based forward would silently drop them.
const forwardOpen = ref(false)
const forwardForm = ref({ to: '', cc: '', comment: '' })
const forwardSending = ref(false)

function startForward() {
  if (!detail.value) return
  forwardForm.value = { to: '', cc: '', comment: '' }
  forwardOpen.value = true
}

async function sendForward() {
  const to = splitAddrs(forwardForm.value.to)
  if (!to.length) {
    error.value = 'Forward needs at least one recipient.'
    return
  }
  forwardSending.value = true
  try {
    const payload = { to, comment: forwardForm.value.comment || '' }
    const cc = splitAddrs(forwardForm.value.cc)
    if (cc.length) payload.cc = cc
    await api.post(`/api/outlook/messages/${detail.value.id}/forward`, payload, {
      successMessage: 'Forwarded.',
    })
    forwardOpen.value = false
  } catch (err) {
    error.value = err.message || 'Forward failed'
  } finally {
    forwardSending.value = false
  }
}

// 1.5 — save the compose pane as a REAL Graph draft so it shows up in the
// Drafts folder (and in Outlook) instead of evaporating on close.
const draftSaving = ref(false)
const draftWebLink = ref(null)
// Fingerprint of what we last saved. A second click on unchanged content
// would otherwise create a SECOND Graph draft — there's no update path yet,
// so the guard is what keeps Drafts from filling with near-duplicates.
const draftSavedFingerprint = ref(null)

function composeFingerprint() {
  const f = composeForm.value
  return JSON.stringify([f.to, f.cc, f.subject, f.body, composeJobId.value])
}

async function saveDraft() {
  const form = composeForm.value
  if (draftSavedFingerprint.value === composeFingerprint()) {
    composeStatus.value = { ok: true, message: 'Already saved to Drafts — nothing changed since.' }
    return
  }
  draftSaving.value = true
  composeStatus.value = null
  try {
    const payload = {
      subject: form.subject || '',
      body_html: (form.body || '').replace(/\n/g, '<br>'),
    }
    const to = splitAddrs(form.to)
    if (to.length) payload.to = to
    const cc = splitAddrs(form.cc)
    if (cc.length) payload.cc = cc
    if (composeJobId.value) payload.job_id = composeJobId.value
    const r = await api.post('/api/outlook/drafts', payload)
    draftSavedFingerprint.value = composeFingerprint()
    draftWebLink.value = r?.web_link || null
    // Say where it actually is. The draft is real and in Outlook NOW, but
    // GDX's Drafts folder is a mirror refreshed by the sync (every 30 min),
    // so "Saved to Drafts" alone sends the user to a folder that still looks
    // empty and reads as a failed save.
    composeStatus.value = {
      ok: true,
      message: 'Saved to your Outlook Drafts — it appears in the Drafts folder after the next sync.',
    }
  } catch (err) {
    composeStatus.value = { ok: false, message: err.message || 'Could not save draft' }
  } finally {
    draftSaving.value = false
  }
}

// P2.4 — AI-suggested reply body. Replaces the quote block, never the user's
// own typing: if they've already written something, append below it.
const aiDrafting = ref(false)

async function draftWithAi() {
  if (!detail.value) return
  aiDrafting.value = true
  composeStatus.value = null
  try {
    const r = await api.post(`/api/outlook/messages/${detail.value.id}/ai-draft`, {})
    const text = r?.draft_text || ''
    if (!text) return
    composeForm.value.body = `${text}\n${quotedBody()}`
    if (r.source === 'fallback') {
      composeStatus.value = { ok: true, message: 'No AI configured — inserted a standard reply.' }
    }
  } catch (err) {
    composeStatus.value = { ok: false, message: err.message || 'AI draft failed' }
  } finally {
    aiDrafting.value = false
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
    // P2.5 — server stamps the [Job #…] subject marker so the reply links back.
    if (composeJobId.value) payload.job_id = composeJobId.value
    const r = await api.post('/api/outlook/send', payload)
    composeStatus.value = { ok: !!r.ok, message: r.ok ? 'Sent.' : (r.detail || 'Send failed') }
    if (r.ok) {
      // Lead-originated compose: the successful send IS the contact event.
      // Only if the lead's address is still among the recipients — a
      // composer re-purposed for someone else stamps nothing. Fire-and-
      // forget: a failed stamp must not un-send the email or block the
      // composer from closing. record-contact is new→contacted only, so a
      // late or duplicate call can never downgrade a lead.
      const sentTo = splitAddrs(form.to).map((a) => a.toLowerCase())
      const stillToLead = composeLeadEmail.value && sentTo.includes(composeLeadEmail.value)
      if (composeLeadId.value && stillToLead) {
        api.post(`/api/leads/${composeLeadId.value}/record-contact`, null, { suppressErrorToast: true }).catch(() => {})
      }
      if (composeLandingLeadId.value && stillToLead) {
        api.post(`/api/landing-leads/${composeLandingLeadId.value}/record-contact`, null, { suppressErrorToast: true }).catch(() => {})
      }
      composeLeadId.value = null
      composeLandingLeadId.value = null
      composeLeadEmail.value = ''
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

// ── P2.1/P2.2 link this email to a customer or job ───────────────────
// The auto-tagger gets most of these; this is the correction path (and the
// only path for mail from an address we've never seen). Linking is what puts
// the message on the customer's / job's Email tab.

// Mirrors views_router._TAG_MANAGER_ROLES. Techs consume tags, they don't
// curate them — the server 403s them, so don't offer the control.
const _TAG_MANAGER_ROLES = ['owner', 'admin', 'dispatcher', 'csr', 'manager', 'sales']
const creatingEstimate = ref(false)

// "Start an estimate from the inbox, linked to the customer automatically."
// Uses the message's customer link (the chip next to From/To). Not linked
// yet → open the link picker instead of a dead click; once linked, one
// click creates the estimate (job name = subject) and lands in the editor.
async function startEstimateFromMessage() {
  if (!detail.value) return
  if (!detail.value.linked_customer_id) {
    toast.add({
      severity: 'info',
      summary: 'Link a customer first',
      detail: 'Pick which customer this message belongs to — the estimate will attach to them.',
      life: 4000,
    })
    openLinkDialog()
    return
  }
  creatingEstimate.value = true
  try {
    const res = await api.post('/api/estimates', {
      customer_id: detail.value.linked_customer_id,
      label: (detail.value.subject || '').replace(/^(re|fwd?):\s*/i, '').trim() || 'From email',
    })
    const est = res?.data || res
    router.push(`/estimates/${est.id}`)
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not create estimate', detail: e?.message || '', life: 5000 })
  } finally {
    creatingEstimate.value = false
  }
}

const canManageLinks = computed(() =>
  _TAG_MANAGER_ROLES.includes(String(auth.user?.role || '').toLowerCase()),
)

const linkOpen = ref(false)
const linkCustomerQuery = ref('')
const linkJobQuery = ref('')
const linkCustomerResults = ref([])
const linkJobResults = ref([])
const linkChoice = ref({ customer_id: null, job_id: null, customer_name: '', job_label: '' })
const linkSaving = ref(false)
let _linkCustTimer = null
let _linkJobTimer = null

function openLinkDialog() {
  if (!detail.value) return
  linkChoice.value = {
    customer_id: detail.value.linked_customer_id || null,
    job_id: detail.value.linked_job_id || null,
    customer_name: detail.value.linked_customer_name || '',
    job_label: detail.value.linked_job_label || '',
  }
  linkCustomerQuery.value = ''
  linkJobQuery.value = ''
  linkCustomerResults.value = []
  linkJobResults.value = []
  linkOpen.value = true
}

async function searchLinkCustomers() {
  clearTimeout(_linkCustTimer)
  _linkCustTimer = setTimeout(async () => {
    const q = linkCustomerQuery.value.trim()
    if (q.length < 2) { linkCustomerResults.value = []; return }
    try {
      const r = await api.get(`/api/customers?q=${encodeURIComponent(q)}&per_page=10`,
        { suppressErrorToast: true })
      linkCustomerResults.value = Array.isArray(r) ? r : (r?.items || [])
    } catch { linkCustomerResults.value = [] }
  }, 300)
}

async function searchLinkJobs() {
  clearTimeout(_linkJobTimer)
  _linkJobTimer = setTimeout(async () => {
    const q = linkJobQuery.value.trim()
    if (q.length < 2) { linkJobResults.value = []; return }
    try {
      const r = await api.get(`/api/jobs?search=${encodeURIComponent(q)}&per_page=10`,
        { suppressErrorToast: true })
      linkJobResults.value = Array.isArray(r) ? r : (r?.items || [])
    } catch { linkJobResults.value = [] }
  }, 300)
}

function pickLinkCustomer(c) {
  linkChoice.value.customer_id = c.id
  linkChoice.value.customer_name = c.name || ''
  linkCustomerResults.value = []
  linkCustomerQuery.value = ''
}

function pickLinkJob(j) {
  linkChoice.value.job_id = j.id
  linkChoice.value.job_label = j.job_number || j.title || String(j.id).slice(0, 8)
  // A job implies its customer — pre-fill it so the message lands on both
  // timelines, which is what "this email is about this job" actually means.
  if (!linkChoice.value.customer_id && j.customer_id) {
    linkChoice.value.customer_id = j.customer_id
    linkChoice.value.customer_name = j.customer_name || ''
  }
  linkJobResults.value = []
  linkJobQuery.value = ''
}

async function saveLink() {
  if (!detail.value) return
  const { customer_id, job_id } = linkChoice.value
  linkSaving.value = true
  try {
    if (!customer_id && !job_id) {
      detail.value = await api.del(`/api/outlook/messages/${detail.value.id}/link`)
    } else {
      const payload = {}
      if (customer_id) payload.customer_id = customer_id
      if (job_id) payload.job_id = job_id
      detail.value = await api.post(`/api/outlook/messages/${detail.value.id}/link`, payload)
    }
    // Keep the list row's badge in step with the pane without a full refetch.
    const row = messages.value.find((m) => m.id === detail.value.id)
    if (row) {
      row.linked_customer_id = detail.value.linked_customer_id
      row.linked_job_id = detail.value.linked_job_id
      row.linked_customer_name = detail.value.linked_customer_name
      row.linked_job_label = detail.value.linked_job_label
    }
    linkOpen.value = false
  } catch (err) {
    error.value = err.message || 'Failed to update link'
  } finally {
    linkSaving.value = false
  }
}

function clearLinkChoice() {
  linkChoice.value = { customer_id: null, job_id: null, customer_name: '', job_label: '' }
}

// ── P2.2 email → follow-up task ──────────────────────────────────────

const taskSaving = ref(false)

async function createTaskFromEmail() {
  if (!detail.value) return
  taskSaving.value = true
  try {
    await api.post(`/api/outlook/messages/${detail.value.id}/create-task`, {}, {
      successMessage: 'Follow-up task created.',
    })
  } catch (err) {
    error.value = err.message || 'Could not create the task'
  } finally {
    taskSaving.value = false
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
    {
      label: m.is_flagged ? 'Unflag' : 'Flag',
      icon: m.is_flagged ? 'pi pi-flag' : 'pi pi-flag-fill',
      command: () => toggleMessageFlag(m),
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

// Flag = Outlook's follow-up flag, written to Microsoft first (the server
// only mirrors on success). Outlook's *pin* has no API, so this is the
// "keep it on top" control that works from both sides.
async function toggleMessageFlag(msg) {
  const want = !msg.is_flagged
  try {
    await api.patch(`/api/outlook/messages/${msg.id}/flag`, { is_flagged: want })
    msg.is_flagged = want
  } catch (err) {
    error.value = err.message || 'Failed to toggle flag'
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

// Flagged first, then newest. The server orders the same way (so paging
// holds), but a flag toggled here must jump the row without a refetch — and
// flags set in Outlook arrive by sync, so the mirror is what gets sorted.
const sortedMessages = computed(() =>
  [...messages.value].sort((a, b) => {
    const fa = a.is_flagged ? 1 : 0
    const fb = b.is_flagged ? 1 : 0
    if (fa !== fb) return fb - fa
    const ta = Date.parse(a.received_at || a.sent_at || 0) || 0
    const tb = Date.parse(b.received_at || b.sent_at || 0) || 0
    return tb - ta
  }),
)

// ── lifecycle ────────────────────────────────────────────────────────

onMounted(async () => {
  fetchSyncHealth()  // deliberately not awaited — banner must not delay mail
  await fetchFolders()
  // Default to Inbox if present
  const inbox = folders.value.find(f => f.well_known_name === 'inbox')
  if (inbox) {
    await selectFolder(inbox)
  } else {
    await fetchMessages()
  }
  // "Email about this job" from a job page lands here (P2.5). Opening the
  // composer with the job attached is what gets the [Job #…] marker onto the
  // subject — which is what makes the customer's reply link itself back.
  // Optional-chained: this view is also mounted in isolation (component
  // tests, storybook) where there is no router — a missing route must not
  // take the whole inbox down before it renders a single message.
  const q = route?.query || {}
  // ?to= alone (Reply-by-email from Leads) opens the composer too — a lead
  // has no job yet, so the old job_id-only gate dead-ended that path.
  if (q.job_id || q.to) {
    startNewCompose()
    if (q.job_id) {
      composeJobId.value = String(q.job_id)
      composeJobLabel.value = q.job_label ? String(q.job_label) : ''
    }
    composeForm.value.to = q.to ? String(q.to) : ''
    composeForm.value.subject = q.subject ? String(q.subject) : ''
    composeLeadId.value = q.lead_id ? String(q.lead_id) : null
    composeLandingLeadId.value = q.landing_lead_id ? String(q.landing_lead_id) : null
    composeLeadEmail.value = q.to ? String(q.to).toLowerCase() : ''
    // Strip the query once armed: a session-restore / back-button revisit
    // of the bare URL days later must not re-open a lead-armed composer.
    router?.replace?.({ path: route.path })
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

    <div class="inbox-search">
      <i class="pi pi-search" aria-hidden="true" />
      <input
        v-model="searchTerm"
        type="search"
        class="inbox-search-input"
        placeholder="Search subject, sender, or preview…"
        aria-label="Search mail"
        data-test="inbox-search"
        @input="onSearchInput"
        @keyup.enter="onSearchInput"
      />
      <button v-if="activeSearch" class="btn-link" data-test="inbox-search-clear" @click="clearSearch">✕</button>
      <span v-if="activeSearch" class="search-note muted" data-test="inbox-search-note">
        Searching all folders — subject, sender and preview text, not full message bodies.
      </span>
    </div>

    <div v-if="error" class="status-error">{{ error }}</div>

    <div
      v-if="syncHealth && syncHealth.status === 'unhealthy'"
      class="sync-health-banner"
      role="alert"
      data-test="sync-health-banner"
    >
      <i class="pi pi-exclamation-triangle" aria-hidden="true" />
      <span>
        <strong>Email is not syncing.</strong>
        {{ syncHealth.problems.join('; ') }}<template v-if="syncHealth.newest_sync_at">
          — last successful sync {{ fmtDate(syncHealth.newest_sync_at) }}</template>.
        New mail will be missing until this is fixed.
      </span>
    </div>

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
          :class="{ active: selectedMsgId === m.id, unread: !m.is_read, flagged: m.is_flagged }"
          data-test="inbox-row"
          @click="openMessage(m)"
        >
          <div class="row-top">
            <i v-if="m.is_flagged" v-tooltip="'Flagged in Outlook'" class="pi pi-flag-fill row-flag" data-test="inbox-row-flag" aria-label="Flagged" />
            <span class="row-from">{{ m.from_address || '(no sender)' }}</span>
            <span class="row-when muted">{{ fmtDate(m.received_at || m.sent_at) }}</span>
            <span v-tooltip="'Message actions'" class="msg-menu-trigger" data-test="msg-menu-trigger" aria-label="Message actions" @click="toggleMessageMenu($event, m)">⋯</span>
          </div>
          <div class="row-subject">{{ m.subject || '(no subject)' }}</div>
          <div class="row-preview muted">{{ m.body_preview || '(no preview)' }}</div>
          <!-- P2.1 — what this email is ABOUT. The whole reason to read mail
               inside GDX rather than in Outlook. -->
          <div v-if="m.linked_customer_id || m.linked_job_id" class="row-links" data-test="inbox-row-links">
            <span v-if="m.linked_customer_id" class="link-chip customer">
              <i class="pi pi-user" aria-hidden="true" />
              {{ m.linked_customer_name || 'Customer' }}
            </span>
            <span v-if="m.linked_job_id" class="link-chip job">
              <i class="pi pi-briefcase" aria-hidden="true" />
              {{ m.linked_job_label || 'Job' }}
            </span>
          </div>
        </button>
        <button
          v-if="hasMoreMessages"
          class="msg-loadmore"
          data-test="inbox-load-more"
          :disabled="loadingMessages"
          @click="loadMoreMessages"
        >
          {{ loadingMessages ? 'Loading…' : 'Load more' }}
        </button>
      </div>

      <!-- ── compose pane ── -->
      <div class="msg-pane" v-if="composeMode" data-test="inbox-compose">
        <div class="pane-header">
          <h2>{{ composeMode === 'reply' ? 'Reply' : 'New message' }}</h2>
          <button class="btn-link" @click="cancelCompose">✕</button>
        </div>
        <div class="compose-fields">
          <div v-if="composeJobId" class="compose-job-chip" data-test="compose-job-chip">
            <i class="pi pi-briefcase" aria-hidden="true" />
            About {{ composeJobLabel || 'this job' }} — replies link back to it automatically.
          </div>
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
          <Button
            outlined
            icon="pi pi-save"
            label="Save draft"
            :loading="draftSaving"
            data-test="compose-save-draft"
            @click="saveDraft"
          />
          <Button
            v-if="composeMode === 'reply' && detail"
            outlined
            icon="pi pi-sparkles"
            label="Draft with AI"
            :loading="aiDrafting"
            data-test="compose-ai-draft"
            @click="draftWithAi"
          />
          <Button outlined @click="cancelCompose">Cancel</Button>
          <span v-if="composeStatus" :class="['compose-status', composeStatus.ok ? 'ok' : 'err']">
            {{ composeStatus.message }}
            <a
              v-if="draftWebLink && composeStatus.ok"
              :href="draftWebLink"
              target="_blank"
              rel="noopener"
              data-test="compose-draft-link"
            >Open in Outlook</a>
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
          <div v-if="detail.is_personal" class="muted" data-test="inbox-personal-flag">🔒 Personal — visible only to you</div>
          <div class="detail-links" data-test="inbox-detail-links">
            <span v-if="detail.linked_customer_id" class="link-chip customer">
              <i class="pi pi-user" aria-hidden="true" />
              {{ detail.linked_customer_name || 'Customer' }}
            </span>
            <span v-if="detail.linked_job_id" class="link-chip job">
              <i class="pi pi-briefcase" aria-hidden="true" />
              {{ detail.linked_job_label || 'Job' }}
            </span>
            <span v-if="!detail.linked_customer_id && !detail.linked_job_id" class="muted">Not linked to a customer or job</span>
            <!-- Office roles only, matching POST /link's own gate — a tech
                 could otherwise search customers, pick one, save, and be told
                 they aren't permitted. -->
            <button v-if="canManageLinks" class="btn-link small" data-test="inbox-link-open" @click="openLinkDialog">
              {{ detail.linked_customer_id || detail.linked_job_id ? 'Change link' : 'Link…' }}
            </button>
          </div>
        </div>

        <!-- 1.3 conversation strip: the rest of this thread, server-resolved
             and visibility-filtered. -->
        <div v-if="threadOthers.length" class="thread-strip" data-test="inbox-thread">
          <div class="thread-title muted">Conversation · {{ thread.length }} messages</div>
          <button
            v-for="t in threadOthers"
            :key="t.id"
            class="thread-row"
            data-test="inbox-thread-row"
            @click="openMessage(t)"
          >
            <span class="thread-from">{{ t.from_address || '(no sender)' }}</span>
            <span class="thread-subject">{{ t.subject || '(no subject)' }}</span>
            <span class="thread-when muted">{{ fmtDate(t.received_at || t.sent_at) }}</span>
          </button>
        </div>
        <EmailAttachments
          v-if="detail.has_attachments"
          :message-id="detail.id"
          :has-attachments="detail.has_attachments"
          :linked-job-id="detail.linked_job_id"
          :linked-job-label="detail.linked_job_label"
        />
        <EmailBodyFrame
          :html="bodyData.body_html || bodyData.body_preview || detail.body_preview || ''"
          :content-type="bodyFrameType"
          :loading="bodyLoading"
          :note="bodyNote"
        />
        <div class="detail-actions">
          <Button label="Reply" icon="pi pi-reply" data-test="inbox-reply" @click="startReply" />
          <Button label="Reply all" icon="pi pi-replay" outlined data-test="inbox-reply-all" @click="startReplyAll" />
          <!-- Start an estimate for the customer this message is linked to
               (Doug 2026-08-18). Unlinked message → the click opens the link
               dialog instead of dead-ending; the estimate opens pre-filled
               with the subject as the job name. Office-gated like linking. -->
          <Button
            v-if="canManageLinks"
            :label="detail.linked_customer_id ? 'New estimate' : 'New estimate…'"
            icon="pi pi-file-plus"
            outlined
            :loading="creatingEstimate"
            data-test="inbox-new-estimate"
            @click="startEstimateFromMessage"
          />
          <!-- Owner-only, same gate as the personal toggle below: Graph's
               forward action resolves the id against the CALLER's mailbox and
               would send under the owner's name, so the server 403s everyone
               else. Showing the button, collecting recipients, and failing at
               the end with "ask an admin for permission" — a permission no
               role grant can give — is the worst version of that. -->
          <Button
            v-if="detail.viewer_is_owner"
            label="Forward"
            icon="pi pi-share-alt"
            outlined
            data-test="inbox-forward"
            @click="startForward"
          />
          <Button
            label="Create task"
            icon="pi pi-check-square"
            outlined
            :loading="taskSaving"
            data-test="inbox-create-task"
            @click="createTaskFromEmail"
          />
          <Button label="Move" icon="pi pi-folder-open" outlined data-test="inbox-move" @click="promptMoveMessage" />
          <!-- Owner-only: the per-message privacy override. Server 403s
               non-owners; viewer_is_owner hides the control from them. -->
          <Button
            v-if="detail.viewer_is_owner"
            :label="detail.is_personal ? 'Make shared' : 'Make personal'"
            :icon="detail.is_personal ? 'pi pi-lock-open' : 'pi pi-lock'"
            outlined
            :loading="personalSaving"
            data-test="inbox-personal-toggle"
            @click="togglePersonal"
          />
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

    <!-- ── Forward dialog (1.4) ── -->
    <Dialog v-model:visible="forwardOpen" header="Forward message" modal :style="{ width: '30rem' }">
      <div class="compose-fields dialog-fields">
        <label>To<input v-model="forwardForm.to" data-test="forward-to" placeholder="name@example.com" /></label>
        <label>Cc<input v-model="forwardForm.cc" data-test="forward-cc" placeholder="optional" /></label>
        <label>Note
          <textarea v-model="forwardForm.comment" rows="5" data-test="forward-comment" placeholder="Optional note above the forwarded message" />
        </label>
      </div>
      <p class="muted" style="font-size:0.8rem">The original attachments are forwarded with it.</p>
      <template #footer>
        <Button label="Cancel" outlined @click="forwardOpen = false" />
        <Button label="Forward" :loading="forwardSending" data-test="forward-send" @click="sendForward" />
      </template>
    </Dialog>

    <!-- ── Link to customer / job dialog (P2.1 / P2.2) ── -->
    <Dialog v-model:visible="linkOpen" header="Link this email" modal :style="{ width: '30rem' }">
      <div class="link-picker">
        <div class="link-current">
          <span v-if="linkChoice.customer_name" class="link-chip customer">{{ linkChoice.customer_name }}</span>
          <span v-else-if="linkChoice.customer_id" class="link-chip customer">Customer selected</span>
          <span v-if="linkChoice.job_label" class="link-chip job">{{ linkChoice.job_label }}</span>
          <span v-else-if="linkChoice.job_id" class="link-chip job">Job selected</span>
          <button
            v-if="linkChoice.customer_id || linkChoice.job_id"
            class="btn-link small"
            data-test="link-clear"
            @click="clearLinkChoice"
          >Clear</button>
        </div>

        <label class="picker-label">
          Customer
          <InputText v-model="linkCustomerQuery" placeholder="Search customers…" data-test="link-customer-search" @input="searchLinkCustomers" />
        </label>
        <ul v-if="linkCustomerResults.length" class="picker-results" data-test="link-customer-results">
          <li v-for="c in linkCustomerResults" :key="c.id">
            <button class="picker-row" @click="pickLinkCustomer(c)">{{ c.name }}<span class="muted"> {{ c.email || '' }}</span></button>
          </li>
        </ul>

        <label class="picker-label">
          Job
          <InputText v-model="linkJobQuery" placeholder="Search jobs…" data-test="link-job-search" @input="searchLinkJobs" />
        </label>
        <ul v-if="linkJobResults.length" class="picker-results" data-test="link-job-results">
          <li v-for="j in linkJobResults" :key="j.id">
            <button class="picker-row" @click="pickLinkJob(j)">
              {{ j.job_number || j.title }}<span class="muted"> {{ j.customer_name || '' }}</span>
            </button>
          </li>
        </ul>
        <p class="muted" style="font-size:0.8rem">
          Clearing both and saving records "no link" — the auto-tagger won't re-link it.
        </p>
      </div>
      <template #footer>
        <Button label="Cancel" outlined @click="linkOpen = false" />
        <Button label="Save link" :loading="linkSaving" data-test="link-save" @click="saveLink" />
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
.msg-loadmore {
  width: 100%;
  padding: 0.6rem;
  border: none;
  border-top: 1px solid var(--p-content-border-color);
  background: var(--p-content-background);
  color: var(--p-primary-color);
  font-size: 0.85rem;
  cursor: pointer;
}
.msg-loadmore:hover:not(:disabled) {
  background: var(--p-content-hover-background);
}
.msg-loadmore:disabled {
  opacity: 0.6;
  cursor: default;
}
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
.msg-row.flagged { border-left: 3px solid var(--p-orange-500, #f97316); }
.row-flag { color: var(--p-orange-500, #f97316); font-size: 0.8rem; margin-right: 0.35rem; }
.row-top {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.85rem;
}
/* Long sender addresses must truncate — without min-width:0 + ellipsis they
   shove the date into a word-per-line vertical wrap and force the list into
   a horizontal scrollbar (2026-08-18 prod walk). */
.row-from {
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-when { font-size: 0.75rem; white-space: nowrap; flex-shrink: 0; }
.row-subject {
  font-size: 0.95rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
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
  flex-shrink: 0;
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
  flex-shrink: 0;
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
  flex-wrap: wrap;
  flex-shrink: 0;
}
/* The body frame yields to the pane's chrome (header, meta, thread strip,
   actions) and fills whatever remains, instead of dictating 55vh and
   forcing flex to crush its siblings. Scoped :deep so EmailBodyFrame's
   other consumers (mobile inbox, timeline) keep their own sizing. */
.msg-pane .email-body-frame {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0 1rem 0.5rem;
}
.msg-pane .email-body-frame :deep(.ebf-iframe) {
  flex: 1 1 auto;
  height: auto;
  min-height: 10rem;
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
.compose-job-chip {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.78rem;
  color: var(--text-secondary);
  background: var(--surface-hover);
  border: 1px solid var(--surface-border);
  border-radius: 6px;
  padding: 0.35rem 0.5rem;
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

/* ── search ── */
.inbox-search {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  background: var(--surface-card);
  border: 1px solid var(--surface-border);
  border-radius: 8px;
  padding: 0.4rem 0.75rem;
}
.inbox-search .pi-search { color: var(--text-secondary); }
.inbox-search-input {
  flex: 1;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 0.9rem;
  outline: none;
  min-width: 0;
}
.search-note { font-size: 0.75rem; }

/* ── link chips ── */
.row-links, .detail-links {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  margin-top: 0.15rem;
}
.detail-links { margin-top: 0.35rem; font-size: 0.8rem; }
.link-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  font-size: 0.72rem;
  padding: 0.05rem 0.45rem;
  border-radius: 10px;
  border: 1px solid var(--surface-border);
  background: var(--surface-hover);
  color: var(--text-secondary);
  max-width: 14rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.link-chip.customer { border-color: var(--p-primary-color); color: var(--p-primary-color); }
.btn-link.small { font-size: 0.78rem; padding: 0; }

/* ── conversation strip ── */
.thread-strip {
  border-bottom: 1px solid var(--surface-border);
  padding: 0.5rem 1rem;
  max-height: 9rem;
  overflow-y: auto;
  /* The pane is a flex column; without this the fixed-height body frame
     crushed the strip to a sliver — all a user saw was its scrollbar
     arrows (prod walk 2026-08-18). */
  flex-shrink: 0;
}
.thread-title { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; }
.thread-row {
  display: flex;
  gap: 0.5rem;
  width: 100%;
  text-align: left;
  background: transparent;
  border: none;
  padding: 0.25rem 0;
  cursor: pointer;
  color: var(--text-primary);
  font-size: 0.8rem;
}
.thread-row:hover { background: var(--surface-hover); }
.thread-from { font-weight: 600; flex: 0 0 auto; max-width: 10rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.thread-subject { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.thread-when { flex: 0 0 auto; font-size: 0.72rem; }

/* ── link picker ── */
.link-picker { display: flex; flex-direction: column; gap: 0.6rem; }
.link-current { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; min-height: 1.5rem; }
.picker-label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.85rem; color: var(--text-secondary); }
.picker-results {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--surface-border);
  border-radius: 6px;
  max-height: 10rem;
  overflow-y: auto;
}
.picker-row {
  width: 100%;
  text-align: left;
  background: transparent;
  border: none;
  padding: 0.4rem 0.6rem;
  cursor: pointer;
  color: var(--text-primary);
  font-size: 0.85rem;
}
.picker-row:hover { background: var(--surface-hover); }
.dialog-fields { padding: 0; }

.muted { color: var(--text-secondary); }
.center { text-align: center; padding: 2rem; }
.status-error { color: var(--color-danger-500, #b91c1c); }

.sync-health-banner {
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
  margin: 0 0 0.75rem;
  padding: 0.6rem 0.85rem;
  border: 1px solid var(--color-warning-border);
  border-radius: 8px;
  background: var(--color-warning-bg);
  color: var(--color-text, inherit);
}
.sync-health-banner .pi {
  color: var(--color-warning-500);
  margin-top: 0.15rem;
}
</style>

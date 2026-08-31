<template>
    <section class="mobile-phone">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>Phone</h1>
          <Button
            v-tooltip="'Refresh'"
            icon="pi pi-refresh"
            aria-label="Refresh"
            text
            size="small"
            :loading="loading"
            @click="refresh"
            data-test="mp-refresh"
          />
        </div>
        <div class="tab-strip" role="tablist" aria-label="Phone views">
          <button
            type="button"
            role="tab"
            class="tab-chip"
            :class="{ active: tab === 'voicemail' }"
            :aria-selected="tab === 'voicemail'"
            data-test="mp-tab-voicemail"
            @click="switchTab('voicemail')"
          >
            <i class="pi pi-envelope" /> Voicemail
          </button>
          <button
            type="button"
            role="tab"
            class="tab-chip"
            :class="{ active: tab === 'calls' }"
            :aria-selected="tab === 'calls'"
            data-test="mp-tab-calls"
            @click="switchTab('calls')"
          >
            <i class="pi pi-phone" /> Calls
          </button>
        </div>
      </header>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading && !calls.length" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading {{ tab === 'voicemail' ? 'voicemails' : 'calls' }}…</span>
      </div>
      <div v-else-if="!calls.length" class="state-msg">
        <i :class="tab === 'voicemail' ? 'pi pi-envelope empty-icon' : 'pi pi-phone empty-icon'" />
        <div class="empty-title">{{ tab === 'voicemail' ? 'No voicemails' : 'No calls yet' }}</div>
        <div class="empty-help">New activity appears within about 10 minutes.</div>
      </div>

      <ol v-else class="card-list">
        <li
          v-for="c in calls"
          :key="c.id"
          class="call-card"
          @click="openDetail(c)"
          data-test="mp-call-row"
        >
          <div class="call-row">
            <span class="call-who">{{ callerLabel(c) }}</span>
            <span class="call-when">{{ fmtAgo(c.started_at) }}</span>
          </div>
          <div class="call-meta">
            <i
              :class="[
                'pi',
                c.direction === 'in' ? 'pi-arrow-down-left dir-in' : 'pi-arrow-up-right dir-out',
              ]"
              :aria-label="prettyDirection(c.direction)"
            />
            <span class="meta-status">{{ friendlyStatus(c) }}</span>
            <span v-if="c.duration_s" class="meta-duration">{{ formatDuration(c.duration_s) }}</span>
            <i
              v-if="c.has_voicemail"
              class="pi pi-envelope meta-vm"
              aria-label="has voicemail"
              data-test="mp-vm-flag"
            />
            <i v-if="c.has_recording" class="pi pi-microphone meta-vm" aria-label="has recording" />
          </div>
        </li>
      </ol>
      <button
        v-if="calls.length < total"
        class="mp-loadmore"
        data-test="mp-load-more"
        :disabled="loading"
        @click="loadMore"
      >
        {{ loading ? 'Loading…' : 'Load more' }}
      </button>

      <!-- Call detail: full-screen bottom sheet, matching MobileInboxView -->
      <Dialog
        v-model:visible="detailOpen"
        :header="detail ? callerLabel(detail) : 'Call'"
        modal
        :style="{ width: '100vw', height: '100dvh' }"
        :breakpoints="{ '768px': '100vw' }"
        position="bottom"
        @hide="closeDetail"
      >
        <div v-if="detailLoading" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
        </div>
        <div v-else-if="detail" class="detail-body">
          <div class="detail-meta">
            <div><strong>{{ prettyDirection(detail.direction) }}</strong> · {{ friendlyStatus(detail) }}</div>
            <div>{{ detail.direction === 'out' ? detail.to_number : detail.from_number }}</div>
            <div class="muted">{{ formatDateTime(detail.started_at) }}<span v-if="detail.duration_s"> · {{ formatDuration(detail.duration_s) }}</span></div>
            <div v-if="detail.customer_name"><strong>Customer:</strong> {{ detail.customer_name }}</div>
          </div>

          <!-- Transcript renders even when the audio fetch fails — an expired
               upstream URL must not hide the words too. -->
          <div v-if="voicemailBlobUrl || transcript" class="audio-block" data-test="mp-vm-player">
            <h3><i class="pi pi-envelope" /> Voicemail</h3>
            <audio v-if="voicemailBlobUrl" :src="voicemailBlobUrl" controls preload="metadata" @play="markHeard" />
            <p v-if="transcript" class="transcript">{{ transcript }}</p>
          </div>

          <div v-if="recordingBlobUrl" class="audio-block" data-test="mp-rec-player">
            <h3><i class="pi pi-microphone" /> Recording</h3>
            <audio :src="recordingBlobUrl" controls preload="metadata" />
          </div>

          <div v-if="audioError" class="error-banner">{{ audioError }}</div>

          <Button
            v-if="callbackNumber"
            class="callback-btn"
            :label="`Call ${callbackNumber}`"
            icon="pi pi-phone"
            severity="success"
            :loading="originating"
            data-test="mp-call-back"
            @click="callBack"
          />
          <p v-if="callbackNumber" class="callback-hint muted">Rings your extension first, then connects the customer.</p>
          <div v-if="originateStatus" :class="['status-line', originateStatus.ok ? 'status-ok' : 'error-banner']">
            {{ originateStatus.message }}
          </div>
        </div>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import { useApi } from '../composables/useApi'
import { formatDateTime } from '../composables/useFormatters'
import { callerDisplay, friendlyStatus, prettyDirection } from '../utils/phoneComLabels'

const api = useApi()

const tab = ref('voicemail')
const calls = ref([])
const total = ref(0)
const page = ref(1)
const perPage = 50
const loading = ref(false)
const error = ref(null)

function callerLabel(call) {
  return call.customer_name || callerDisplay(call) || call.to_number || '—'
}

function formatDuration(s) {
  if (!s) return ''
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${m}:${String(sec).padStart(2, '0')}`
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

const fetchCalls = async ({ append = false } = {}) => {
  loading.value = true
  error.value = null
  try {
    const params = new URLSearchParams()
    params.set('page', page.value)
    params.set('per_page', perPage)
    if (tab.value === 'voicemail') params.set('has_voicemail', 'true')
    const r = await api.get(`/api/phone-com/calls?${params.toString()}`)
    calls.value = append ? [...calls.value, ...r.items] : r.items
    total.value = r.total
  } catch (err) {
    error.value = err.message || 'Failed to load calls'
  } finally {
    loading.value = false
  }
}

const refresh = () => {
  page.value = 1
  fetchCalls()
}

const switchTab = (next) => {
  if (tab.value === next) return
  tab.value = next
  page.value = 1
  calls.value = []
  fetchCalls()
}

const loadMore = () => {
  page.value += 1
  fetchCalls({ append: true })
}

// ── Detail sheet + audio (mirrors PhoneComCallsView's blob pattern:
// <audio src> can't carry the Bearer header, so fetch as an authed blob) ──
const detailOpen = ref(false)
const detail = ref(null)
const detailLoading = ref(false)
const transcript = ref('')
const voicemailBlobUrl = ref(null)
const recordingBlobUrl = ref(null)
const audioError = ref(null)

function _authHeaders() {
  const tok = sessionStorage.getItem('gdx_access_token')
    || localStorage.getItem('gdx_access_token')
    || localStorage.getItem('auth_token')
    || ''
  return tok ? { Authorization: `Bearer ${tok}` } : {}
}

async function _fetchAudioBlob(path) {
  const r = await fetch(path, { headers: _authHeaders() })
  if (!r.ok) throw new Error(`audio fetch ${r.status}`)
  return URL.createObjectURL(await r.blob())
}

function _revoke(url) {
  if (url && url.startsWith('blob:')) URL.revokeObjectURL(url)
}

const openDetail = async (call) => {
  detail.value = null
  transcript.value = ''
  audioError.value = null
  originateStatus.value = null
  _revoke(voicemailBlobUrl.value)
  _revoke(recordingBlobUrl.value)
  voicemailBlobUrl.value = null
  recordingBlobUrl.value = null
  detailOpen.value = true
  detailLoading.value = true
  try {
    detail.value = await api.get(`/api/phone-com/calls/${call.id}`)
    if (detail.value.has_voicemail) {
      try {
        const t = await api.get(`/api/phone-com/calls/${call.id}/voicemail-transcript`)
        transcript.value = t.transcript || ''
      } catch { transcript.value = '' }
      try {
        voicemailBlobUrl.value = await _fetchAudioBlob(`/api/phone-com/calls/${call.id}/voicemail-audio`)
      } catch (err) {
        audioError.value = `voicemail: ${err.message}`
      }
    }
    if (detail.value.has_recording) {
      try {
        recordingBlobUrl.value = await _fetchAudioBlob(`/api/phone-com/calls/${call.id}/recording`)
      } catch (err) {
        audioError.value = `${audioError.value ? audioError.value + '; ' : ''}recording: ${err.message}`
      }
    }
  } catch (err) {
    error.value = err.message || 'Failed to load call detail'
    detailOpen.value = false
  } finally {
    detailLoading.value = false
  }
}

const markHeard = async () => {
  if (!detail.value) return
  try {
    await api.post(`/api/phone-com/calls/${detail.value.id}/mark-heard`)
  } catch { /* heard-state is best-effort; playback already succeeded */ }
}

const closeDetail = () => {
  _revoke(voicemailBlobUrl.value)
  _revoke(recordingBlobUrl.value)
  voicemailBlobUrl.value = null
  recordingBlobUrl.value = null
  transcript.value = ''
  audioError.value = null
  detail.value = null
  originateStatus.value = null
}

// ── Click-to-call back ──
const originating = ref(false)
const originateStatus = ref(null)

const callbackNumber = computed(() => {
  if (!detail.value) return ''
  return detail.value.direction === 'out' ? detail.value.to_number : detail.value.from_number
})

const callBack = async () => {
  if (!callbackNumber.value) return
  originating.value = true
  originateStatus.value = null
  try {
    await api.post('/api/phone-com/calls/originate', {
      to: callbackNumber.value,
      customer_id: detail.value?.customer_id || undefined,
      job_id: detail.value?.job_id || undefined,
    })
    originateStatus.value = { ok: true, message: 'Calling — answer your extension to connect.' }
  } catch (err) {
    originateStatus.value = { ok: false, message: err.message || 'Call failed' }
  } finally {
    originating.value = false
  }
}

onMounted(fetchCalls)
// Navigating away with the detail sheet open skips closeDetail — revoke
// any live blob URLs here too so they can't leak across route changes.
onUnmounted(() => {
  _revoke(voicemailBlobUrl.value)
  _revoke(recordingBlobUrl.value)
})
</script>

<style scoped>
.mobile-phone {
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

.tab-strip {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-2);
}
.tab-chip {
  flex: 1 1 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
  padding: 0.5rem 0.75rem;
  min-height: 44px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: var(--surface-elevated);
  color: var(--text-muted);
  font-size: 0.85rem;
  cursor: pointer;
}
.tab-chip.active {
  background: var(--interactive-primary);
  border-color: var(--interactive-primary);
  color: #fff;
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
.call-card {
  background: var(--surface-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 0.625rem;
  padding: var(--space-3);
  cursor: pointer;
}
.call-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}
.call-who {
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.call-when {
  flex: 0 0 auto;
  font-size: 0.75rem;
  color: var(--text-muted);
}
.call-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: 0.25rem;
  font-size: 0.8rem;
  color: var(--text-muted);
}
.dir-in {
  color: var(--interactive-primary);
}
.dir-out {
  color: var(--text-muted);
}
.meta-vm {
  color: var(--interactive-primary);
}

.mp-loadmore {
  width: 100%;
  margin-top: var(--space-3);
  padding: 0.75rem;
  min-height: 44px;
  border: 1px solid var(--border-subtle);
  border-radius: 0.625rem;
  background: var(--surface-elevated);
  color: var(--text-primary);
  cursor: pointer;
}

.detail-body {
  display: grid;
  gap: var(--space-3);
}
.detail-meta {
  display: grid;
  gap: 0.25rem;
}
.muted {
  color: var(--text-muted);
  font-size: 0.85rem;
}

.audio-block h3 {
  margin: 0 0 var(--space-2);
  font-size: 0.95rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.audio-block audio {
  width: 100%;
}
.transcript {
  margin: var(--space-2) 0 0;
  padding: var(--space-2);
  background: var(--surface-panel);
  border-radius: 0.5rem;
  font-size: 0.9rem;
  white-space: pre-wrap;
}

.callback-btn {
  width: 100%;
}
.callback-hint {
  margin: 0;
  text-align: center;
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

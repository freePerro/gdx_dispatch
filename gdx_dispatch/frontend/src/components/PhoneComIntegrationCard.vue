<script setup>
import { ref, computed, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApi()

const phoneEnabled = ref(false)
const loading = ref(true)
const error = ref(null)

const phoneSettings = ref({
  token_set: false,
  voip_id: null,
  default_extension_id: null,
  default_caller_id: null,
  last_validated_at: null,
  last_synced_at: null,
  last_error: null,
  account_features: null,
  webhook_status: { registered: false, callback_id: null, listener_id: null },
})

const tokenInput = ref('')
const voipIdInput = ref('')
const defaultExtInput = ref('')
const defaultCallerInput = ref('')
const saveStatus = ref(null)
const adminError = ref(false)

// Token-details panel (P3.10)
const tokenDetails = ref(null)
const tokenDetailsLoading = ref(false)
const tokenDetailsError = ref(null)

// OAuth code-exchange flow (P3.10)
const oauthOpen = ref(false)
const oauthInput = ref({
  client_id: '',
  client_secret: '',
  redirect_uri: '',
  code: '',
})
const oauthBusy = ref(false)
const oauthStatus = ref(null)

// Blocked-calls panel (P2.6)
const blockedOpen = ref(false)
const blockedItems = ref([])
const blockedLoading = ref(false)
const blockedError = ref(null)
const blockedForm = ref({ name: '', number: '', direction: 'in', action: 'block' })
const blockedFormBusy = ref(false)

// Wave C / S3 + S4 — catalogs populated by phone_com sync. When non-empty,
// the Settings card shows real dropdowns instead of free-text fields.
const numbersCatalog = ref([])  // [{phone_com_number, label, is_default_outbound}]
const extensionsCatalog = ref([])  // [{phone_com_extension_id, name, number}]

const recordingOn = computed(() => {
  const features = phoneSettings.value.account_features || {}
  return Boolean(features['call-recording-on'])
})

function formatRelative(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const diffSec = Math.max(0, (Date.now() - t) / 1000)
  if (diffSec < 60) return 'just now'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`
  if (diffSec < 7 * 86400) return `${Math.floor(diffSec / 86400)}d ago`
  return new Date(t).toLocaleDateString()
}

const fetchModules = async () => {
  try {
    loading.value = true
    error.value = null
    const response = await api.get('/api/settings/modules')
    const mod = response.modules?.find(m => m.key === 'phone_com')
    if (mod) phoneEnabled.value = !!mod.enabled
  } catch (err) {
    error.value = err.message || 'Failed to load module state'
  } finally {
    loading.value = false
  }
}

const fetchSettings = async () => {
  try {
    const response = await api.get('/api/settings/integrations/phone-com')
    phoneSettings.value = response
    // Pre-fill non-token fields with current values for visibility/edit.
    voipIdInput.value = response.voip_id ? String(response.voip_id) : ''
    defaultExtInput.value = response.default_extension_id || ''
    defaultCallerInput.value = response.default_caller_id || ''
    adminError.value = false
  } catch (err) {
    if (err.status === 403) adminError.value = true
    else if (err.status !== 401) console.error('Failed to fetch phone-com settings:', err)
  }
}

const fetchCatalogs = async () => {
  // Wave C: pull numbers + extensions catalog so the Settings card can render
  // real dropdowns. Both are gated on phone_com being enabled. 401/403/test-
  // missing-mocks are silenced — empty catalog falls back to free-text inputs.
  try {
    const r = await api.get('/api/phone-com/numbers')
    numbersCatalog.value = (r && Array.isArray(r.items)) ? r.items : []
  } catch (_e) {
    numbersCatalog.value = []
  }
  try {
    const r = await api.get('/api/phone-com/extensions')
    extensionsCatalog.value = (r && Array.isArray(r.items)) ? r.items : []
  } catch (_e) {
    extensionsCatalog.value = []
  }
}

const toggleModule = async () => {
  const previous = phoneEnabled.value
  const next = !previous
  phoneEnabled.value = next
  try {
    const endpoint = next
      ? '/api/settings/modules/phone_com/enable'
      : '/api/settings/modules/phone_com/disable'
    await api.post(endpoint)
  } catch (err) {
    phoneEnabled.value = previous
    error.value = err.message || 'Failed to update module'
  }
}

const buildPatchPayload = () => {
  const payload = {}
  if (tokenInput.value && tokenInput.value.length >= 10) {
    payload.token = tokenInput.value
  }
  if (voipIdInput.value) {
    const n = Number(voipIdInput.value)
    const current = phoneSettings.value.voip_id ?? null
    if (Number.isFinite(n) && n > 0 && n !== current) payload.voip_id = n
  }
  if (defaultExtInput.value !== (phoneSettings.value.default_extension_id || '')) {
    payload.default_extension_id = defaultExtInput.value || null
  }
  if (defaultCallerInput.value !== (phoneSettings.value.default_caller_id || '')) {
    payload.default_caller_id = defaultCallerInput.value || null
  }
  return payload
}

const saveAndTest = async () => {
  const payload = buildPatchPayload()
  if (Object.keys(payload).length === 0) {
    saveStatus.value = {
      ok: false,
      message: 'Nothing to save.',
      class: 'status-warn',
    }
    return
  }
  try {
    const response = await api.patch('/api/settings/integrations/phone-com', payload)
    const test = response.test_result || {}
    if (test.ok) {
      const acct = test.account_name ? ` (${test.account_name})` : ''
      const wh = response.webhook_status?.registered ? ' · webhook registered' : ''
      saveStatus.value = {
        ok: true,
        message: `voip_id=${test.voip_id}${acct} · ${test.latency_ms || 0}ms${wh}`,
        class: 'status-ok',
      }
      tokenInput.value = ''
    } else {
      saveStatus.value = {
        ok: false,
        message: test.error || 'Validation failed',
        class: 'status-error',
      }
    }
    await fetchSettings()
  } catch (err) {
    saveStatus.value = {
      ok: false,
      message: err.message || 'Failed to save',
      class: 'status-error',
    }
  }
}

const reTest = async () => {
  try {
    const result = await api.post('/api/settings/integrations/phone-com/test')
    if (result.ok) {
      saveStatus.value = {
        ok: true,
        message: `${result.account_name || 'OK'} · ${result.latency_ms || 0}ms`,
        class: 'status-ok',
      }
    } else {
      saveStatus.value = {
        ok: false,
        message: result.error || 'Test failed',
        class: 'status-error',
      }
    }
    await fetchSettings()
  } catch (err) {
    saveStatus.value = {
      ok: false,
      message: err.message || 'Test failed',
      class: 'status-error',
    }
  }
}

const syncNow = async () => {
  saveStatus.value = {
    ok: true,
    message: 'Syncing from Phone.com…',
    class: 'status-ok',
  }
  try {
    const r = await api.post('/api/settings/integrations/phone-com/sync-now')
    const summary = `${r.calls_synced || 0} calls · ${r.messages_synced || 0} SMS · ${r.voicemails_synced || 0} voicemails`
    saveStatus.value = {
      ok: true,
      message: `Synced: ${summary}`,
      class: 'status-ok',
    }
  } catch (err) {
    saveStatus.value = {
      ok: false,
      message: err.message || 'Sync failed',
      class: 'status-error',
    }
  }
}

const toggleTokenDetails = async () => {
  if (tokenDetails.value !== null) {
    tokenDetails.value = null
    return
  }
  await fetchTokenDetails()
}

const fetchTokenDetails = async () => {
  tokenDetailsLoading.value = true
  tokenDetailsError.value = null
  try {
    tokenDetails.value = await api.get('/api/settings/integrations/phone-com/token-details')
  } catch (err) {
    tokenDetailsError.value = err.message || 'Failed to load token details'
    tokenDetails.value = null
  } finally {
    tokenDetailsLoading.value = false
  }
}

const exchangeOAuthCode = async () => {
  oauthStatus.value = null
  const payload = { ...oauthInput.value }
  if (!payload.client_id || !payload.client_secret || !payload.code || !payload.redirect_uri) {
    oauthStatus.value = { ok: false, message: 'All four fields are required.' }
    return
  }
  oauthBusy.value = true
  try {
    const r = await api.post('/api/settings/integrations/phone-com/oauth/exchange', payload)
    if (r.ok) {
      const wh = r.webhook_status?.registered ? ' · webhook registered' : ''
      oauthStatus.value = { ok: true, message: `Connected · voip_id=${r.voip_id}${wh}` }
      oauthInput.value = { client_id: '', client_secret: '', redirect_uri: '', code: '' }
      oauthOpen.value = false
      await fetchSettings()
      await fetchCatalogs()
    } else {
      oauthStatus.value = { ok: false, message: r.error || 'Exchange returned no token' }
    }
  } catch (err) {
    oauthStatus.value = { ok: false, message: err.message || 'OAuth exchange failed' }
  } finally {
    oauthBusy.value = false
  }
}

const fetchBlockedCalls = async () => {
  blockedLoading.value = true
  blockedError.value = null
  try {
    const r = await api.get('/api/phone-com/blocked-calls')
    blockedItems.value = r.items || []
  } catch (err) {
    blockedError.value = err.message || 'Failed to load blocked numbers'
    blockedItems.value = []
  } finally {
    blockedLoading.value = false
  }
}

const toggleBlockedPanel = async () => {
  blockedOpen.value = !blockedOpen.value
  if (blockedOpen.value && blockedItems.value.length === 0) {
    await fetchBlockedCalls()
  }
}

const submitBlockedNumber = async () => {
  const payload = { ...blockedForm.value }
  if (!payload.name || !payload.number) {
    blockedError.value = 'Name and number are required.'
    return
  }
  blockedFormBusy.value = true
  blockedError.value = null
  try {
    await api.post('/api/phone-com/blocked-calls', payload)
    blockedForm.value = { name: '', number: '', direction: 'in', action: 'block' }
    await fetchBlockedCalls()
  } catch (err) {
    blockedError.value = err.message || 'Failed to add'
  } finally {
    blockedFormBusy.value = false
  }
}

const removeBlockedNumber = async (id) => {
  if (!(await confirmAsync({ header: 'Confirm', message: 'Remove this number from the block list?' }))) return
  try {
    await api.del(`/api/phone-com/blocked-calls/${id}`)
    await fetchBlockedCalls()
  } catch (err) {
    blockedError.value = err.message || 'Failed to remove'
  }
}

// Diagnostics panel — token/voip/webhook health + the listener event-filter
// tag-match check (does our 'phone.*' match Phone.com's bare tags?).
const diagOpen = ref(false)
const diagLoading = ref(false)
const diagError = ref(null)
const diagChecks = ref([])

function diagIcon(s) {
  return s === 'ok' ? '✓' : s === 'warn' ? '!' : s === 'fail' ? '✕' : '·'
}

const runDiagnostics = async () => {
  diagLoading.value = true
  diagError.value = null
  try {
    const r = await api.get('/api/settings/integrations/phone-com/diagnostics')
    diagChecks.value = r.checks || []
  } catch (err) {
    diagError.value = err.message || 'Diagnostics failed'
    diagChecks.value = []
  } finally {
    diagLoading.value = false
  }
}

const toggleDiagnostics = () => {
  diagOpen.value = !diagOpen.value
  if (diagOpen.value) runDiagnostics()
}

const disconnect = async () => {
  if (!(await confirmAsync({ header: 'Confirm', message: 'Disconnect Phone.com? This clears the token and removes the webhook from Phone.com.' }))) return
  try {
    await api.del('/api/settings/integrations/phone-com/token')
    saveStatus.value = {
      ok: true,
      message: 'Disconnected.',
      class: 'status-ok',
    }
    await fetchSettings()
  } catch (err) {
    saveStatus.value = {
      ok: false,
      message: err.message || 'Disconnect failed',
      class: 'status-error',
    }
  }
}

onMounted(async () => {
  // Order matters: tests queue api.get mocks in [modules, settings] sequence.
  // Sequential awaits keep that ordering. Catalogs only fetched once a token
  // is set — empty card has nothing to populate dropdowns from anyway.
  await fetchModules()
  await fetchSettings()
  if (phoneSettings.value && phoneSettings.value.token_set) {
    await fetchCatalogs()
  }
})
</script>

<template>
  <div class="pc-card" data-testid="phone-com-integration-card">
    <div class="pc-card-header">
      <div>
        <h3>Phone.com Voice &amp; SMS</h3>
        <p class="muted">
          Per-tenant Phone.com integration. Bring your own Phone.com access token —
          we never share keys across tenants.
        </p>
      </div>
      <div class="toggle-row">
        <input
          type="checkbox"
          id="phone-com-module-toggle"
          class="toggle-input"
          data-test="phone-com-module-toggle"
          :checked="phoneEnabled"
          @change="toggleModule"
        />
        <label for="phone-com-module-toggle">Enabled</label>
      </div>
    </div>

    <div v-if="error" class="status-row status-error">{{ error }}</div>
    <div v-if="loading" class="muted">Loading…</div>

    <div v-else>
      <div v-if="adminError" class="status-row status-warn">
        Admin access required to manage the Phone.com token.
      </div>
      <div v-else class="key-section">
        <label for="pc-token-input">Phone.com Access Token</label>
        <div class="key-row">
          <input
            id="pc-token-input"
            type="password"
            v-model="tokenInput"
            data-test="pc-token-input"
            placeholder="paste your permanent token from api-client.cit-phone.com"
            class="key-input"
            autocomplete="new-password"
            spellcheck="false"
            autocapitalize="off"
          />
        </div>
        <div class="grid-row">
          <div>
            <label for="pc-voip-input">Account ID (voip_id)</label>
            <input
              id="pc-voip-input"
              type="text"
              v-model="voipIdInput"
              data-test="pc-voip-input"
              placeholder="1000000"
              class="key-input"
            />
          </div>
          <div>
            <label for="pc-default-ext">Default extension</label>
            <select
              v-if="extensionsCatalog.length > 0"
              id="pc-default-ext"
              v-model="defaultExtInput"
              data-test="pc-default-ext"
              class="key-input"
            >
              <option value="">— pick an extension —</option>
              <option
                v-for="x in extensionsCatalog"
                :key="x.phone_com_extension_id"
                :value="x.phone_com_extension_id"
              >
                {{ x.number || x.phone_com_extension_id }}{{ x.name ? ` · ${x.name}` : '' }}
              </option>
            </select>
            <input
              v-else
              id="pc-default-ext"
              type="text"
              v-model="defaultExtInput"
              data-test="pc-default-ext"
              placeholder="100"
              class="key-input"
            />
          </div>
          <div>
            <label for="pc-default-caller">Default caller ID</label>
            <select
              v-if="numbersCatalog.length > 0"
              id="pc-default-caller"
              v-model="defaultCallerInput"
              data-test="pc-default-caller"
              class="key-input"
            >
              <option value="">— pick a number —</option>
              <option
                v-for="n in numbersCatalog"
                :key="n.phone_com_number"
                :value="n.phone_com_number"
              >
                {{ n.phone_com_number }}{{ n.label ? ` · ${n.label}` : '' }}{{ n.is_default_outbound ? ' (default)' : '' }}
              </option>
            </select>
            <input
              v-else
              id="pc-default-caller"
              type="text"
              v-model="defaultCallerInput"
              data-test="pc-default-caller"
              placeholder="+18005550199"
              class="key-input"
            />
          </div>
        </div>

        <div class="action-row">
          <button
            type="button"
            data-test="pc-save-test"
            @click="saveAndTest"
            class="btn-primary"
          >
            Save &amp; Test
          </button>
          <button
            v-if="phoneSettings.token_set"
            type="button"
            data-test="pc-retest"
            @click="reTest"
            class="btn-secondary"
          >
            Re-test
          </button>
          <button
            v-if="phoneSettings.token_set"
            type="button"
            data-test="pc-sync-now"
            @click="syncNow"
            class="btn-secondary"
          >
            Sync now
          </button>
          <button
            v-if="phoneSettings.token_set"
            type="button"
            data-test="pc-disconnect"
            @click="disconnect"
            class="btn-link btn-link-danger"
          >
            Disconnect
          </button>
        </div>

        <div v-if="saveStatus" data-test="pc-save-status" :class="['status-row', saveStatus.class]">
          {{ saveStatus.message }}
        </div>

        <div v-if="!phoneSettings.token_set" class="oauth-row">
          <button
            type="button"
            class="btn-link"
            data-test="pc-oauth-toggle"
            @click="oauthOpen = !oauthOpen"
          >
            {{ oauthOpen ? 'Hide OAuth code-exchange' : 'Connect with OAuth code (alternative)' }}
          </button>
          <div v-if="oauthOpen" class="oauth-panel">
            <p class="muted">
              For Phone.com OAuth apps: paste the four values from your auth-redirect.
              The backend exchanges the code for an access token and stores it like a
              pasted token.
            </p>
            <div class="grid-row">
              <div>
                <label for="pc-oauth-client-id">client_id</label>
                <input id="pc-oauth-client-id" v-model="oauthInput.client_id" class="key-input" data-test="pc-oauth-client-id" />
              </div>
              <div>
                <label for="pc-oauth-client-secret">client_secret</label>
                <input id="pc-oauth-client-secret" v-model="oauthInput.client_secret" type="password" class="key-input" data-test="pc-oauth-client-secret" autocomplete="new-password" />
              </div>
            </div>
            <div class="grid-row">
              <div>
                <label for="pc-oauth-redirect">redirect_uri</label>
                <input id="pc-oauth-redirect" v-model="oauthInput.redirect_uri" class="key-input" data-test="pc-oauth-redirect" placeholder="https://gdx.example.com/settings" />
              </div>
              <div>
                <label for="pc-oauth-code">code</label>
                <input id="pc-oauth-code" v-model="oauthInput.code" class="key-input" data-test="pc-oauth-code" />
              </div>
            </div>
            <div class="action-row">
              <button
                type="button"
                class="btn-primary"
                :disabled="oauthBusy"
                @click="exchangeOAuthCode"
                data-test="pc-oauth-exchange"
              >
                {{ oauthBusy ? 'Exchanging…' : 'Exchange code & connect' }}
              </button>
            </div>
            <div v-if="oauthStatus" :class="['status-row', oauthStatus.ok ? 'status-ok' : 'status-error']">
              {{ oauthStatus.message }}
            </div>
          </div>
        </div>

        <div v-if="phoneSettings.token_set" class="key-meta">
          <span v-if="phoneSettings.last_error" class="status-error">
            Token on file, last validation failed: {{ phoneSettings.last_error }}
          </span>
          <span v-else class="muted">
            Token on file
            <span v-if="phoneSettings.last_validated_at">
              · validated {{ formatRelative(phoneSettings.last_validated_at) }}
            </span>
          </span>
          <div class="features muted">
            Webhook:
            <span :class="phoneSettings.webhook_status.registered ? 'status-ok' : 'status-warn'">
              {{ phoneSettings.webhook_status.registered ? 'registered' : 'not registered' }}
            </span>
            <span
              v-if="phoneSettings.webhook_status.registered && phoneSettings.webhook_status.callback_id"
              data-test="pc-callback-id"
            >
              (callback #{{ phoneSettings.webhook_status.callback_id }})
            </span>
            <span v-if="phoneSettings.account_features">
              · call-recording: {{ recordingOn ? 'on' : 'off' }}
            </span>
            <span v-if="phoneSettings.last_synced_at" data-test="pc-last-synced">
              · last synced: {{ formatRelative(phoneSettings.last_synced_at) }}
            </span>
            <span v-else class="status-warn" data-test="pc-last-synced">
              · never synced
            </span>
          </div>

          <div class="subpanel">
            <button
              type="button"
              class="btn-link"
              data-test="pc-token-details-toggle"
              @click="toggleTokenDetails"
            >
              {{ tokenDetails ? 'Hide token details' : 'Show token details' }}
            </button>
            <div v-if="tokenDetailsLoading" class="muted">Loading token details…</div>
            <div v-else-if="tokenDetailsError" class="status-row status-error">
              {{ tokenDetailsError }}
            </div>
            <div v-else-if="tokenDetails" class="token-details" data-test="pc-token-details-panel">
              <div v-if="!tokenDetails.available" class="muted">
                <span v-if="tokenDetails.reason === 'permanent_token'">
                  Permanent token — Phone.com does not expose introspection details for this type.
                </span>
                <span v-else>
                  Token details unavailable<span v-if="tokenDetails.error">: {{ tokenDetails.error }}</span>.
                </span>
              </div>
              <div v-else class="kv-grid">
                <div class="kv-row"><span class="kv-label">Scope</span><span>{{ tokenDetails.scope || '—' }}</span></div>
                <div class="kv-row"><span class="kv-label">Grant type</span><span>{{ tokenDetails.grant_type || '—' }}</span></div>
                <div class="kv-row"><span class="kv-label">Expires</span><span>{{ tokenDetails.expires_at || '—' }}</span></div>
                <div class="kv-row"><span class="kv-label">Last used</span><span>{{ tokenDetails.last_used_at || '—' }}</span></div>
              </div>
            </div>
          </div>

          <div class="subpanel">
            <button
              type="button"
              class="btn-link"
              data-test="pc-blocked-toggle"
              @click="toggleBlockedPanel"
            >
              {{ blockedOpen ? 'Hide blocked numbers' : 'Manage blocked numbers' }}
            </button>
            <div v-if="blockedOpen" class="blocked-panel" data-test="pc-blocked-panel">
              <div v-if="blockedLoading" class="muted">Loading…</div>
              <div v-if="blockedError" class="status-row status-error">{{ blockedError }}</div>
              <table v-if="blockedItems.length > 0" class="blocked-table">
                <thead>
                  <tr><th>Name</th><th>Number</th><th>Direction</th><th>Action</th><th></th></tr>
                </thead>
                <tbody>
                  <tr v-for="b in blockedItems" :key="b.id" data-test="pc-blocked-row">
                    <td>{{ b.name }}</td>
                    <td>{{ b.number }}</td>
                    <td>{{ b.direction }}</td>
                    <td>{{ b.action }}</td>
                    <td>
                      <button type="button" class="btn-link btn-link-danger" @click="removeBlockedNumber(b.id)" data-test="pc-blocked-remove">
                        Remove
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
              <p v-else-if="!blockedLoading && !blockedError" class="muted">No blocked numbers.</p>

              <div class="blocked-form">
                <h4 class="blocked-form-title">Block a number</h4>
                <div class="grid-row grid-row-4">
                  <div>
                    <label for="pc-blk-name">Name</label>
                    <input id="pc-blk-name" v-model="blockedForm.name" class="key-input" data-test="pc-blocked-name" placeholder="Spam caller" />
                  </div>
                  <div>
                    <label for="pc-blk-number">Number (E.164)</label>
                    <input id="pc-blk-number" v-model="blockedForm.number" class="key-input" data-test="pc-blocked-number" placeholder="+15555551234" />
                  </div>
                  <div>
                    <label for="pc-blk-direction">Direction</label>
                    <select id="pc-blk-direction" v-model="blockedForm.direction" class="key-input" data-test="pc-blocked-direction">
                      <option value="in">Inbound</option>
                      <option value="out">Outbound</option>
                    </select>
                  </div>
                  <div>
                    <label for="pc-blk-action">Action</label>
                    <select id="pc-blk-action" v-model="blockedForm.action" class="key-input" data-test="pc-blocked-action">
                      <option value="block">Block</option>
                      <option value="voicemail">Voicemail</option>
                    </select>
                  </div>
                </div>
                <div class="action-row">
                  <button
                    type="button"
                    class="btn-primary"
                    :disabled="blockedFormBusy"
                    @click="submitBlockedNumber"
                    data-test="pc-blocked-submit"
                  >
                    {{ blockedFormBusy ? 'Adding…' : 'Add to block list' }}
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div class="subpanel">
            <button
              type="button"
              class="btn-link"
              data-test="pc-diag-toggle"
              @click="toggleDiagnostics"
            >
              {{ diagOpen ? 'Hide diagnostics' : 'Run diagnostics' }}
            </button>
            <div v-if="diagOpen" class="diag-panel" data-test="pc-diag-panel">
              <div v-if="diagLoading" class="muted">Running…</div>
              <div v-if="diagError" class="status-row status-error">{{ diagError }}</div>
              <ul v-if="diagChecks.length" class="diag-list">
                <li
                  v-for="c in diagChecks"
                  :key="c.key"
                  :class="['diag-item', `diag-${c.status}`]"
                  data-test="pc-diag-row"
                >
                  <span class="diag-badge">{{ diagIcon(c.status) }}</span>
                  <span class="diag-label">{{ c.label }}</span>
                  <span class="diag-detail">{{ c.detail }}</span>
                </li>
              </ul>
            </div>
          </div>
        </div>
        <div v-else class="empty-state muted" data-test="pc-empty-state">
          <p class="empty-headline">No token yet — here's how to get one that works.</p>
          <ol class="empty-steps">
            <li>
              Open
              <a href="https://api-client.cit-phone.com/" target="_blank" rel="noopener">api-client.cit-phone.com</a>
              in a new tab.
            </li>
            <li>
              <strong>Before you sign in</strong>, click <em>Create permanent token</em> and grant the <code>account-owner</code> scope. (Tokens minted after sign-in have a narrower policy and will fail with <code>oauth2.access_denied</code>.)
            </li>
            <li>Paste the token above along with your <code>voip_id</code> (find it in your Phone.com account dashboard URL).</li>
          </ol>
          <p class="empty-foot">
            See
            <a href="https://apidocs.phone.com/" target="_blank" rel="noopener">Phone.com API docs</a>
            for the full reference.
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pc-card {
  background: var(--surface-panel);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.pc-card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}

.pc-card-header h3 {
  margin: 0;
  font-size: 1.05rem;
}

.muted {
  color: var(--text-muted);
  font-size: 0.85rem;
}

.toggle-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.toggle-input {
  width: 36px;
  height: 20px;
  cursor: pointer;
}

.key-section {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.key-section label {
  font-weight: 500;
  font-size: 0.85rem;
  color: var(--text-primary);
}

.key-row {
  display: flex;
  gap: 0.5rem;
}

.grid-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 0.5rem;
}

.grid-row label {
  display: block;
  margin-bottom: 0.2rem;
}

.key-input {
  width: 100%;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--surface-elevated);
  color: var(--text-primary);
  font-family: inherit;
}

.action-row {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  align-items: center;
}

.btn-primary {
  background: var(--interactive-primary);
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 0.45rem 0.9rem;
  cursor: pointer;
}

.btn-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-secondary {
  background: var(--surface-elevated);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  padding: 0.45rem 0.9rem;
  cursor: pointer;
}

.btn-link {
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0;
  text-decoration: underline;
  color: var(--interactive-primary);
}

.btn-link-danger {
  color: var(--color-danger-500);
}

.status-row {
  border: 1px solid;
  border-radius: 6px;
  padding: 0.4rem 0.75rem;
  font-size: 0.85rem;
}

.status-ok {
  color: var(--color-success-500);
  background: var(--color-success-bg);
  border-color: var(--color-success-border);
}

.status-warn {
  color: var(--color-warning-500);
  background: var(--color-warning-bg);
  border-color: var(--color-warning-border);
}

.status-error {
  color: var(--color-danger-500);
  background: var(--color-danger-bg);
  border-color: var(--color-danger-border);
}

.key-meta {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.features {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}

.empty-state {
  border-left: 3px solid var(--interactive-primary);
  background: var(--surface-elevated);
  padding: 0.75rem 1rem;
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.empty-headline {
  margin: 0;
  font-weight: 500;
  color: var(--text-primary);
}

.empty-steps {
  margin: 0;
  padding-left: 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.empty-steps code {
  font-size: 0.8rem;
  background: var(--surface-panel);
  padding: 0 0.25rem;
  border-radius: 3px;
}

.empty-foot {
  margin: 0;
}

.subpanel {
  margin-top: 0.6rem;
  padding-top: 0.6rem;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.token-details,
.blocked-panel {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.kv-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.4rem;
  font-size: 0.85rem;
}

.kv-row {
  display: flex;
  gap: 0.5rem;
}

.kv-label {
  color: var(--text-muted);
  min-width: 80px;
}

.blocked-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}

.blocked-table th,
.blocked-table td {
  text-align: left;
  padding: 0.35rem 0.5rem;
  border-bottom: 1px solid var(--border-subtle);
}

.blocked-table th {
  color: var(--text-muted);
  font-weight: 500;
}

.blocked-form {
  margin-top: 0.5rem;
  padding-top: 0.5rem;
  border-top: 1px dashed var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.blocked-form-title {
  margin: 0;
  font-size: 0.9rem;
  font-weight: 500;
}

.grid-row-4 {
  grid-template-columns: 1fr 1fr 1fr 1fr;
}

.oauth-row {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin-top: 0.5rem;
}

.oauth-panel {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.6rem 0.75rem;
  background: var(--surface-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
}

.diag-panel {
  margin-top: 0.5rem;
}
.diag-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.diag-item {
  display: grid;
  grid-template-columns: 1.25rem 9rem 1fr;
  gap: 0.5rem;
  align-items: start;
  padding: 0.4rem 0.55rem;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  font-size: 0.85rem;
}
.diag-badge {
  font-weight: 700;
  text-align: center;
}
.diag-label {
  font-weight: 600;
}
.diag-detail {
  color: var(--text-muted, inherit);
}
.diag-ok .diag-badge { color: var(--color-success-500); }
.diag-ok { background: var(--color-success-bg); border-color: var(--color-success-border); }
.diag-warn .diag-badge { color: var(--color-warning-500); }
.diag-warn { background: var(--color-warning-bg); border-color: var(--color-warning-border); }
.diag-fail .diag-badge { color: var(--color-danger-500); }
.diag-fail { background: var(--color-danger-bg); border-color: var(--color-danger-border); }
</style>

<!--
  SS-14 slice F — Personal Access Token (PAT) management UI.
  TODO integration: mount this view in gdx/frontend/src/router/index.js
  at path /settings/api-keys once SS-14 integration lands.
  The capability picker options are expected to come from an endpoint
  that returns derive_capability_options(app.openapi()) — wired in
  SS-14 integration alongside the /api/pats router.
-->
<template>
    <section class="settings-api-keys view-card" data-testid="settings-api-keys-view">
    <header class="header view-heading-row">
      <Button
        icon="pi pi-arrow-left"
        aria-label="Back"
        text
        severity="secondary"
        size="small"
        class="back-button"
        @click="$router.back()"
      />
      <h1 class="view-heading">API Keys</h1>
      <p class="muted">
        Personal Access Tokens (PATs) authenticate programmatic requests to the
        GDX API. Tokens are shown exactly once at creation — store them safely.
      </p>
    </header>

    <!-- New-token form -->
    <section class="create-card" aria-labelledby="create-pat-heading">
      <h3 id="create-pat-heading">Create a new token</h3>

      <div class="form-row">
        <label for="pat-name">Token name</label>
        <input
          id="pat-name"
          v-model="form.name"
          type="text"
          placeholder="e.g. ci-deployments"
          data-testid="pat-name-input"
        />
      </div>

      <div class="form-row">
        <label for="pat-expiry">Expires in (days)</label>
        <input
          id="pat-expiry"
          v-model.number="form.expiresInDays"
          type="number"
          min="1"
          max="366"
          data-testid="pat-expiry-input"
        />
      </div>

      <fieldset class="capabilities-grid" data-testid="capabilities-grid">
        <legend>Capabilities</legend>
        <p v-if="capsLoading" class="muted">Loading capabilities…</p>
        <p v-else-if="capabilities.length === 0" class="muted">
          No capabilities available for this account.
        </p>
        <div
          v-for="cap in capabilities"
          :key="cap.id"
          class="capability-row"
          :data-testid="`cap-row-${cap.id}`"
        >
          <label>
            <input
              type="checkbox"
              :value="cap.id"
              v-model="form.capabilityIds"
              :data-testid="`cap-check-${cap.id}`"
            />
            <span class="cap-label">{{ cap.label }}</span>
            <span class="cap-pair muted">{{ cap.action }}:{{ cap.resource_type }}</span>
          </label>
        </div>
      </fieldset>

      <div class="actions">
        <button
          type="button"
          :disabled="creating || !canSubmit"
          data-testid="pat-create-button"
          @click="createToken"
        >
          {{ creating ? 'Creating…' : 'Create token' }}
        </button>
      </div>

      <div
        v-if="lastSecret"
        class="secret-banner"
        role="alert"
        data-testid="pat-secret-banner"
      >
        <strong>Copy this token now — it will never be shown again:</strong>
        <code data-testid="pat-secret-value">{{ lastSecret }}</code>
      </div>

      <p v-if="errorMessage" class="error" role="alert" data-testid="pat-error">
        {{ errorMessage }}
      </p>
    </section>

    <!-- Existing tokens -->
    <section class="list-card" aria-labelledby="list-pats-heading">
      <h3 id="list-pats-heading">Existing tokens</h3>
      <p v-if="listLoading" class="muted">Loading…</p>
      <table v-else-if="tokens.length" data-testid="pat-list-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Prefix</th>
            <th>Created</th>
            <th>Expires</th>
            <th>Last used</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="t in tokens"
            :key="t.id"
            :data-testid="`pat-row-${t.id}`"
          >
            <td>{{ t.name }}</td>
            <td><code>{{ t.prefix }}</code></td>
            <td>{{ formatDate(t.created_at) }}</td>
            <td>{{ formatDate(t.expires_at) }}</td>
            <td>{{ formatDate(t.last_used_at) || 'never' }}</td>
            <td>
              <button
                type="button"
                :disabled="revokingId === t.id"
                :data-testid="`pat-revoke-${t.id}`"
                @click="revokeToken(t.id)"
              >
                {{ revokingId === t.id ? 'Revoking…' : 'Revoke' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted" data-testid="pat-list-empty">
        No active tokens.
      </p>
    </section>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import { createApiClient } from '../composables/useApi'

const api = createApiClient()
const CAPABILITIES_ENDPOINT = '/api/capabilities/available'
const PATS_ENDPOINT = '/api/pats'

const capabilities = ref([])
const capsLoading = ref(false)
const tokens = ref([])
const listLoading = ref(false)
const creating = ref(false)
const revokingId = ref(null)
const lastSecret = ref('')
const errorMessage = ref('')

const form = ref({
  name: '',
  expiresInDays: 90,
  capabilityIds: [],
})

const canSubmit = computed(() => form.value.name.trim().length > 0)

function formatDate(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString()
  } catch {
    return iso
  }
}

async function loadCapabilities() {
  capsLoading.value = true
  try {
    const raw = await api.get(CAPABILITIES_ENDPOINT)
    // Accept either a bare list or a list keyed under .options — the
    // backend integration can choose either shape.
    const list = Array.isArray(raw) ? raw : (raw.options || [])
    // The backend derive_capability_options() returns (action, resource_type)
    // tuples without DB ids; for the UI we need a stable key. Use the pair
    // as a synthetic id so the form can submit something round-trippable.
    capabilities.value = list.map((c, idx) => ({
      id: c.id || `${c.action}:${c.resource_type}:${idx}`,
      action: c.action,
      resource_type: c.resource_type,
      label: c.label || `${c.action} ${c.resource_type}`,
    }))
  } catch (err) {
    errorMessage.value = `Failed to load capabilities: ${err.message}`
  } finally {
    capsLoading.value = false
  }
}

async function loadTokens() {
  listLoading.value = true
  try {
    tokens.value = await api.get(PATS_ENDPOINT)
  } catch (err) {
    errorMessage.value = `Failed to load tokens: ${err.message}`
  } finally {
    listLoading.value = false
  }
}

async function createToken() {
  errorMessage.value = ''
  lastSecret.value = ''
  creating.value = true
  try {
    const body = await api.post(PATS_ENDPOINT, {
      name: form.value.name.trim(),
      expires_in_days: form.value.expiresInDays,
      capability_ids: form.value.capabilityIds,
    })
    lastSecret.value = body.secret
    form.value = { name: '', expiresInDays: 90, capabilityIds: [] }
    await loadTokens()
  } catch (err) {
    errorMessage.value = `Failed to create token: ${err.message}`
  } finally {
    creating.value = false
  }
}

async function revokeToken(id) {
  revokingId.value = id
  errorMessage.value = ''
  try {
    await api.del(`${PATS_ENDPOINT}/${id}`)
    await loadTokens()
  } catch (err) {
    errorMessage.value = `Failed to revoke token: ${err.message}`
  } finally {
    revokingId.value = null
  }
}

onMounted(async () => {
  await Promise.all([loadCapabilities(), loadTokens()])
})

// Exposed for unit tests.
defineExpose({
  form,
  capabilities,
  tokens,
  lastSecret,
  errorMessage,
  loadCapabilities,
  loadTokens,
  createToken,
  revokeToken,
})
</script>

<style scoped>
.settings-api-keys { display: flex; flex-direction: column; gap: 1.5rem; padding: 1rem; }
.muted { color: #888; font-size: 0.9rem; }
.form-row { display: flex; flex-direction: column; margin-bottom: 0.75rem; }
.form-row label { margin-bottom: 0.25rem; font-weight: 500; }
.capabilities-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 0.5rem; border: 1px solid #ddd; padding: 0.75rem; }
.capability-row label { display: flex; align-items: center; gap: 0.5rem; cursor: pointer; }
.cap-pair { font-family: monospace; font-size: 0.8rem; }
.actions { margin-top: 1rem; }
.secret-banner { margin-top: 1rem; padding: 0.75rem; background: #fff7d6; border: 1px solid #e0c240; }
.secret-banner code { display: block; margin-top: 0.5rem; font-size: 0.9rem; word-break: break-all; }
.error { color: #b00020; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #eee; }
</style>

<!--
  SS-31 slice F — tenant-admin Federation Providers UI.
  Backend: gdx/routers/federation.py — literal paths /api/federation/providers.

  CRUD on OIDC/SAML IdP providers for the caller's tenant. /auth/federation/*
  paths are the login flow, not wired here.
-->
<template>
    <section class="admin-view federation-providers view-card" data-testid="federation-providers-view">
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
      <h1 class="view-heading">Federation Providers</h1>
      <p class="muted">
        OIDC and SAML identity providers registered for your tenant. Tenant
        admins may register new providers and remove existing ones. Metadata
        URL must be HTTPS.
      </p>
    </header>

    <div v-if="loading" class="muted" data-testid="fed-loading">Loading…</div>
    <div v-if="error" class="error" role="alert" data-testid="error-message">
      {{ error }}
    </div>

    <section class="register-form" data-testid="fed-register-form">
      <h3>Register provider</h3>
      <form @submit.prevent="onRegister">
        <label>
          Kind
          <select v-model="form.kind" data-testid="fed-input-kind">
            <option value="oidc">OIDC</option>
            <option value="saml">SAML</option>
          </select>
        </label>
        <label>
          Display name
          <input v-model="form.display_name" type="text" required data-testid="fed-input-name" />
        </label>
        <label>
          Metadata URL (https)
          <input
            v-model="form.metadata_url"
            type="text"
            required
            placeholder="https://idp.example.com/.well-known/openid-configuration"
            data-testid="fed-input-metadata"
          />
        </label>
        <template v-if="form.kind === 'oidc'">
          <label>
            Client ID
            <input v-model="form.client_id" type="text" data-testid="fed-input-client-id" />
          </label>
          <label>
            Client secret
            <input v-model="form.client_secret" type="password" data-testid="fed-input-client-secret" />
          </label>
          <label>
            Redirect URI
            <input v-model="form.redirect_uri" type="text" data-testid="fed-input-redirect" />
          </label>
        </template>
        <template v-else>
          <label>
            SP Entity ID
            <input v-model="form.sp_entity_id" type="text" data-testid="fed-input-sp-entity" />
          </label>
          <label>
            ACS URL
            <input v-model="form.acs_url" type="text" data-testid="fed-input-acs" />
          </label>
        </template>
        <button type="submit" data-testid="fed-submit">Register</button>
      </form>
    </section>

    <section class="list">
      <h3>Registered providers ({{ providers.length }})</h3>
      <table v-if="providers.length" data-testid="fed-table">
        <thead>
          <tr>
            <th>Display name</th>
            <th>Kind</th>
            <th>Metadata URL</th>
            <th>Client ID</th>
            <th>Secret?</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="p in providers"
            :key="p.id"
            :data-testid="`fed-row-${p.id}`"
          >
            <td>{{ p.display_name }}</td>
            <td>{{ p.kind }}</td>
            <td><code>{{ p.metadata_url }}</code></td>
            <td>{{ p.client_id || '—' }}</td>
            <td>{{ p.has_client_secret ? 'yes' : '—' }}</td>
            <td>
              <button
                :data-testid="`fed-delete-${p.id}`"
                @click="onDelete(p.id)"
              >
                Delete
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!loading" class="muted" data-testid="fed-empty">
        No federation providers registered.
      </p>
    </section>
    </section>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import Button from 'primevue/button'
import { createApiClient } from '../composables/useApi'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = createApiClient()

const providers = ref([])
const loading = ref(false)
const error = ref('')
const form = ref({
  kind: 'oidc',
  display_name: '',
  metadata_url: '',
  client_id: '',
  client_secret: '',
  redirect_uri: '',
  sp_entity_id: '',
  acs_url: '',
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const body = await api.get('/api/federation/providers')
    providers.value = body.items || []
  } catch (e) {
    error.value = e?.message || 'Failed to load providers'
  } finally {
    loading.value = false
  }
}

async function onRegister() {
  error.value = ''
  const payload = {
    kind: form.value.kind,
    display_name: form.value.display_name,
    metadata_url: form.value.metadata_url,
  }
  if (form.value.kind === 'oidc') {
    if (form.value.client_id) payload.client_id = form.value.client_id
    if (form.value.client_secret) payload.client_secret = form.value.client_secret
    if (form.value.redirect_uri) payload.redirect_uri = form.value.redirect_uri
  } else {
    if (form.value.sp_entity_id) payload.sp_entity_id = form.value.sp_entity_id
    if (form.value.acs_url) payload.acs_url = form.value.acs_url
  }
  try {
    await api.post('/api/federation/providers', payload)
    form.value = {
      kind: 'oidc', display_name: '', metadata_url: '',
      client_id: '', client_secret: '', redirect_uri: '',
      sp_entity_id: '', acs_url: '',
    }
    await load()
  } catch (e) {
    error.value = e?.message || 'Register failed'
  }
}

async function onDelete(id) {
  error.value = ''
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete provider ${id}? This invalidates its trust bundle cache.` }))) return // eslint-disable-line no-alert
  try {
    await api.del(`/api/federation/providers/${encodeURIComponent(id)}`)
    await load()
  } catch (e) {
    error.value = e?.message || 'Delete failed'
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.admin-view { max-width: 1100px; margin: 0 auto; padding: 1rem; }
.muted { color: #666; }
.error { background: #fee; border: 1px solid #c33; padding: 0.5rem; margin: 0.5rem 0; }
.register-form label { display: block; margin: 0.25rem 0; }
.register-form input,
.register-form select { width: 100%; margin-top: 0.25rem; box-sizing: border-box; }
table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
th, td { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #eee; }
</style>

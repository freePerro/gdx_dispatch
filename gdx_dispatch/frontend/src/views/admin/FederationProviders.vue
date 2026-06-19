<!--
  SS-31 slice G — tenant-admin FederationProviders.vue.

  TODO: mount in gdx/frontend/src/router/index.js at
    /admin/federation-providers (tenant-admin gate).

  Backend contract (see gdx/routers/federation.py):
    GET    /api/federation/providers        -> { items, total }
    POST   /api/federation/providers        -> provider record
    DELETE /api/federation/providers/{id}   -> 204

  Plain HTML controls — no PrimeVue / router / toast deps so the
  view is standalone-testable. Follows the same fetchFn-injection
  pattern used by CutoverControl.vue / ShadowMigrations.vue for
  vitest parity.
-->
<template>
  <section class="federation-providers" data-testid="federation-providers">
    <header class="header">
      <h2>Federation (SSO) Providers</h2>
      <p class="muted">
        Connect an external identity provider (OIDC or SAML) so your
        users can sign in with their corporate IdP. Metadata URL must
        use <code>https://</code>.
      </p>
    </header>

    <!-- list -->
    <section class="list" data-testid="list">
      <h3>Registered providers</h3>
      <div v-if="loading" data-testid="loading">Loading…</div>
      <div v-else-if="providers.length === 0" data-testid="empty" class="empty">
        No providers registered yet.
      </div>
      <ul v-else data-testid="providers-list">
        <li
          v-for="p in providers"
          :key="p.id"
          :data-testid="`provider-${p.id}`"
          class="provider-row"
        >
          <div class="provider-main">
            <strong>{{ p.display_name }}</strong>
            <span class="kind">[{{ p.kind }}]</span>
            <div class="meta">{{ p.metadata_url }}</div>
          </div>
          <button
            type="button"
            class="danger"
            :data-testid="`delete-${p.id}`"
            @click="confirmDelete(p)"
          >
            Remove
          </button>
        </li>
      </ul>
    </section>

    <!-- register form -->
    <section class="register" data-testid="register-section">
      <h3>Register new provider</h3>
      <form data-testid="register-form" @submit.prevent="submit">
        <label>
          Kind
          <select v-model="form.kind" data-testid="kind">
            <option value="oidc">OIDC</option>
            <option value="saml">SAML</option>
          </select>
        </label>
        <label>
          Display name
          <input
            v-model="form.display_name"
            data-testid="display-name"
            placeholder="Corp Okta"
            required
          />
        </label>
        <label>
          Metadata URL
          <input
            v-model="form.metadata_url"
            data-testid="metadata-url"
            placeholder="https://idp.example.com/.well-known/openid-configuration"
            required
          />
        </label>

        <!-- OIDC-only -->
        <template v-if="form.kind === 'oidc'">
          <label>
            Client ID
            <input v-model="form.client_id" data-testid="client-id" />
          </label>
          <label>
            Client secret
            <input
              v-model="form.client_secret"
              data-testid="client-secret"
              type="password"
              autocomplete="new-password"
            />
          </label>
          <label>
            Redirect URI
            <input
              v-model="form.redirect_uri"
              data-testid="redirect-uri"
              placeholder="https://gdx.example.com/auth/federation/<id>/callback"
            />
          </label>
        </template>

        <!-- SAML-only -->
        <template v-else>
          <label>
            SP entityID
            <input v-model="form.sp_entity_id" data-testid="sp-entity-id" />
          </label>
          <label>
            ACS URL
            <input
              v-model="form.acs_url"
              data-testid="acs-url"
              placeholder="https://gdx.example.com/auth/federation/<id>/acs"
            />
          </label>
        </template>

        <div class="actions">
          <button
            type="submit"
            data-testid="submit"
            :disabled="!canSubmit || submitting"
          >
            {{ submitting ? "Registering…" : "Register provider" }}
          </button>
        </div>
      </form>
    </section>

    <!-- delete confirm -->
    <div
      v-if="pendingDelete"
      class="confirm"
      data-testid="delete-confirm"
    >
      Remove <strong>{{ pendingDelete.display_name }}</strong>? Users linked
      through this provider will no longer be able to sign in (their
      identity rows are retained).
      <button
        type="button"
        class="danger"
        data-testid="delete-confirm-yes"
        @click="doDelete"
      >
        Confirm remove
      </button>
      <button
        type="button"
        data-testid="delete-confirm-no"
        @click="pendingDelete = null"
      >
        Cancel
      </button>
    </div>

    <!-- error -->
    <div v-if="errorMessage" class="error" data-testid="error">
      <strong>Error:</strong> {{ errorMessage }}
    </div>
  </section>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";

const props = defineProps({
  fetchFn: { type: Function, default: null },
  initialProviders: { type: Array, default: () => [] },
});

const providers = ref([...props.initialProviders]);
const loading = ref(false);
const submitting = ref(false);
const errorMessage = ref("");
const pendingDelete = ref(null);

const form = ref({
  kind: "oidc",
  display_name: "",
  metadata_url: "",
  client_id: "",
  client_secret: "",
  redirect_uri: "",
  sp_entity_id: "",
  acs_url: "",
});

const canSubmit = computed(() => {
  if (!form.value.display_name || !form.value.metadata_url) return false;
  if (!/^https:\/\//i.test(form.value.metadata_url)) return false;
  return true;
});

async function api(url, { method = "GET", body = null } = {}) {
  const opts = { method };
  if (body) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  if (props.fetchFn) return props.fetchFn(url, opts);
  const res = await fetch(url, opts);
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const err = new Error(
      data?.detail?.error || data?.error || `HTTP ${res.status}`,
    );
    err.body = data;
    throw err;
  }
  return data;
}

async function load() {
  loading.value = true;
  errorMessage.value = "";
  try {
    const data = await api("/api/federation/providers");
    providers.value = data?.items || [];
  } catch (e) {
    errorMessage.value = e?.message || String(e);
  } finally {
    loading.value = false;
  }
}

async function submit() {
  if (!canSubmit.value) return;
  submitting.value = true;
  errorMessage.value = "";
  const body = {
    kind: form.value.kind,
    display_name: form.value.display_name,
    metadata_url: form.value.metadata_url,
  };
  if (form.value.kind === "oidc") {
    if (form.value.client_id) body.client_id = form.value.client_id;
    if (form.value.client_secret) body.client_secret = form.value.client_secret;
    if (form.value.redirect_uri) body.redirect_uri = form.value.redirect_uri;
  } else {
    if (form.value.sp_entity_id) body.sp_entity_id = form.value.sp_entity_id;
    if (form.value.acs_url) body.acs_url = form.value.acs_url;
  }
  try {
    const created = await api("/api/federation/providers", {
      method: "POST",
      body,
    });
    providers.value = [...providers.value, created];
    // reset sensitive fields (never keep plaintext secret in DOM state)
    form.value.client_secret = "";
    form.value.display_name = "";
    form.value.metadata_url = "";
    form.value.client_id = "";
    form.value.redirect_uri = "";
    form.value.sp_entity_id = "";
    form.value.acs_url = "";
  } catch (e) {
    errorMessage.value = e?.message || String(e);
  } finally {
    submitting.value = false;
  }
}

function confirmDelete(p) {
  pendingDelete.value = p;
}

async function doDelete() {
  const p = pendingDelete.value;
  if (!p) return;
  try {
    await api(`/api/federation/providers/${encodeURIComponent(p.id)}`, {
      method: "DELETE",
    });
    providers.value = providers.value.filter((x) => x.id !== p.id);
  } catch (e) {
    errorMessage.value = e?.message || String(e);
  } finally {
    pendingDelete.value = null;
  }
}

onMounted(() => {
  if (props.initialProviders.length === 0) load();
});

defineExpose({ load });
</script>

<style scoped>
.federation-providers {
  font-family: system-ui, sans-serif;
  max-width: 780px;
}
.muted {
  color: #666;
}
.empty {
  color: #888;
  font-style: italic;
}
.provider-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border: 1px solid #ddd;
  border-radius: 6px;
  padding: 10px 14px;
  margin: 6px 0;
}
.provider-main .kind {
  margin-left: 8px;
  color: #555;
  font-size: 0.9em;
}
.provider-main .meta {
  color: #888;
  font-size: 0.85em;
  margin-top: 2px;
}
.register form {
  display: grid;
  gap: 10px;
}
.register label {
  display: grid;
  gap: 4px;
}
.register input,
.register select {
  padding: 6px 8px;
  border: 1px solid #ccc;
  border-radius: 4px;
}
.actions {
  margin-top: 8px;
}
button.danger {
  background: #c0392b;
  color: white;
  border: none;
  padding: 6px 10px;
  border-radius: 4px;
  cursor: pointer;
}
button[disabled] {
  opacity: 0.5;
  cursor: not-allowed;
}
.confirm {
  border: 2px solid #c0392b;
  padding: 12px;
  margin-top: 12px;
  border-radius: 6px;
  background: #fff3f2;
}
.error {
  border-left: 3px solid #c0392b;
  padding: 8px 12px;
  margin-top: 10px;
  background: #fff3f2;
}
</style>

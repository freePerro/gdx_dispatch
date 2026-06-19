<script setup>
import { computed, ref, onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'

/*
 * Platform-host entry surface at app.example.com/login-picker.
 *
 * Two roles:
 *
 * 1. Unauthenticated arrival (the marketing-site "Sign in" CTA lands here):
 *    render a real credential form. Calls auth.login() which routes to
 *    /auth/platform-login on the platform host. Single-tenant success
 *    triggers a full-page redirect inside auth.login() — we never reach
 *    the success branch. Multi-tenant returns `{status:'select_tenant',
 *    tenants:[...]}` and we render the picker below the form. Sign-up
 *    escape hatch always visible.
 *
 * 2. Authenticated arrival (post-login redirect from /auth/login when the
 *    identity has 2+ memberships): render the workspace list from
 *    /api/me/tenants. Zero-tenant and single-tenant edge cases are
 *    handled defensively so a stale bookmark degrades sanely.
 *
 * Pre-2026-05-11 the unauthenticated branch was a "Sign In" link that
 * bounced to /login mid-load. Doug flagged: "this is not a page you can
 * put credentials in and sign in." Replaced with the inline form.
 */

const auth = useAuthStore()
const tenants = ref([])
const loading = ref(true)
const error = ref('')
const unauthenticated = ref(false)
const email = ref('')
const password = ref('')
const submitting = ref(false)

const tenantChoices = computed(() => auth.tenantChoices || [])
const showTenantChoices = computed(() => tenantChoices.value.length > 1)

async function loadTenants() {
  error.value = ''
  if (!auth.isAuthenticated) {
    unauthenticated.value = true
    loading.value = false
    return
  }
  unauthenticated.value = false
  loading.value = true
  try {
    const headers = { Accept: 'application/json' }
    if (auth.accessToken) {
      headers.Authorization = `Bearer ${auth.accessToken}`
    }
    const resp = await fetch('/api/me/tenants', {
      credentials: 'include',
      headers,
    })
    if (resp.status === 401 || resp.status === 403) {
      unauthenticated.value = true
      return
    }
    if (!resp.ok) {
      throw new Error(`Failed to load workspaces (${resp.status})`)
    }
    const body = await resp.json()
    tenants.value = Array.isArray(body) ? body : []
    if (tenants.value.length === 1) {
      goto(tenants.value[0].slug)
      return
    }
  } catch (e) {
    error.value = e && e.message ? e.message : 'Unable to load workspaces.'
  } finally {
    loading.value = false
  }
}

function goto(slug) {
  if (!slug) return
  window.location.href = `/t/${slug}/`
}

async function handleSubmit() {
  error.value = ''
  if (!email.value.trim()) { error.value = 'Email is required'; return }
  if (!password.value) { error.value = 'Password is required'; return }
  submitting.value = true
  try {
    const result = await auth.login({
      email: email.value.trim(),
      password: password.value,
    })
    if (result && result.status === 'select_tenant') {
      // Multi-tenant: tenantChoices is populated on the store; the
      // template will switch to the workspace-pick UI below.
      submitting.value = false
      return
    }
    // Single-tenant: auth.login() triggered a full-page redirect — we
    // won't reach here. Safety net: if we do, reload to pick up the
    // session-tenant state.
    window.location.reload()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Unable to sign in'
    submitting.value = false
  }
}

async function pickTenantChoice(tenantId) {
  error.value = ''
  submitting.value = true
  try {
    await auth.login({
      email: auth.pendingPlatformCreds?.email || email.value,
      password: auth.pendingPlatformCreds?.password || password.value,
      tenant_id: tenantId,
    })
    // Pick triggers a full-page redirect inside auth.login.
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Unable to sign in'
    submitting.value = false
  }
}

onMounted(loadTenants)
</script>

<template>
  <div class="login-picker" data-testid="login-picker">
    <div class="picker-card">
      <a href="https://example.com" class="brand" aria-label="DispatchApp">
        <span class="brand-mark" aria-hidden="true"></span>
        <span class="brand-text">Team<span class="brand-accent">GarageDoor</span></span>
      </a>

      <!-- Unauthenticated: inline credentials form -->
      <template v-if="unauthenticated && !showTenantChoices">
        <h1>Sign in</h1>
        <p class="sub">Welcome back. Enter your email and password to continue.</p>
        <form class="login-form" data-testid="login-form" @submit.prevent="handleSubmit">
          <label class="field">
            <span class="field-label">Email</span>
            <input
              v-model="email"
              type="email"
              autocomplete="username"
              placeholder="name@company.com"
              required
              data-testid="login-email"
            />
          </label>
          <label class="field">
            <span class="field-label">Password</span>
            <input
              v-model="password"
              type="password"
              autocomplete="current-password"
              placeholder="••••••••"
              required
              data-testid="login-password"
            />
          </label>
          <button type="submit" class="submit-btn" :disabled="submitting" data-testid="login-submit">
            <span v-if="!submitting">Sign in</span>
            <span v-else class="loader" aria-label="Signing in"></span>
          </button>
          <p v-if="error" class="error" data-testid="login-error">{{ error }}</p>
        </form>
        <p class="links">
          <router-link to="/forgot-password">Forgot your password?</router-link>
        </p>
        <p class="signup-cta">
          Don't have an account?
          <a href="/signup" data-testid="login-picker-signup-link">Start a free trial &rarr;</a>
        </p>
      </template>

      <!-- Multi-tenant: just authenticated, pick workspace before token issues -->
      <template v-else-if="showTenantChoices">
        <h1>Choose a workspace</h1>
        <p class="sub">You belong to multiple workspaces. Pick one to continue.</p>
        <ul class="tenant-list" data-testid="login-picker-list">
          <li v-for="t in tenantChoices" :key="t.tenant_id">
            <button
              type="button"
              class="tenant-btn"
              :disabled="submitting"
              :data-testid="`tenant-choice-${t.slug}`"
              @click="pickTenantChoice(t.tenant_id)"
            >
              <span class="tenant-name">{{ t.name }}</span>
              <span class="tenant-slug">{{ t.slug }}.example.com</span>
            </button>
          </li>
        </ul>
        <p v-if="error" class="error" data-testid="login-error">{{ error }}</p>
      </template>

      <!-- Authenticated: workspace switcher (existing flow, post-login redirect) -->
      <template v-else>
        <h1>Choose a workspace</h1>
        <p v-if="loading" class="sub" data-testid="login-picker-loading">Loading workspaces…</p>
        <p v-else-if="error" class="error" data-testid="login-picker-error">{{ error }}</p>
        <ul v-else-if="tenants.length > 0" class="tenant-list" data-testid="login-picker-list">
          <li v-for="t in tenants" :key="t.slug">
            <button
              type="button"
              class="tenant-btn"
              :data-testid="`tenant-${t.slug}`"
              @click="goto(t.slug)"
            >
              <span class="tenant-name">{{ t.name }}</span>
              <span class="tenant-role">{{ t.role }}</span>
            </button>
          </li>
        </ul>
        <p v-else class="empty" data-testid="login-picker-empty">
          You don't belong to any workspaces yet.
          <a href="/signup">Start a free trial</a>.
        </p>
        <p v-if="error" class="retry-row">
          <button type="button" class="retry-btn" @click="loadTenants">Try again</button>
        </p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.login-picker {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: radial-gradient(ellipse at top left, #1e293b, #0f172a 70%);
  padding: 24px;
  color: #f8fafc;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

.picker-card {
  width: 100%;
  max-width: 440px;
  background: #1e293b;
  color: #f8fafc;
  padding: 2.25rem;
  border-radius: 16px;
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
  border: 1px solid rgba(255, 255, 255, 0.06);
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 0.55rem;
  color: #f8fafc;
  text-decoration: none;
  margin-bottom: 1.5rem;
}
.brand-mark {
  width: 24px;
  height: 24px;
  border-radius: 6px;
  background: linear-gradient(135deg, #3b82f6, #22d3ee);
}
.brand-text {
  font-weight: 800;
  letter-spacing: -0.01em;
}
.brand-accent { color: #3b82f6; }

h1 {
  font-size: 1.5rem;
  margin: 0 0 0.4rem;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.sub {
  color: #94a3b8;
  margin: 0 0 1.5rem;
  font-size: 0.92rem;
  line-height: 1.5;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.field-label {
  font-size: 0.82rem;
  font-weight: 500;
  color: #cbd5e1;
}
input {
  width: 100%;
  padding: 0.7rem 0.85rem;
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 8px;
  color: #f8fafc;
  font: inherit;
  font-size: 0.95rem;
  transition: border-color 0.15s, box-shadow 0.15s;
  box-sizing: border-box;
}
input:focus {
  outline: none;
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.18);
}
input::placeholder { color: #475569; }

.submit-btn {
  margin-top: 0.4rem;
  padding: 0.75rem;
  background: #3b82f6;
  color: #fff;
  border: none;
  border-radius: 8px;
  font: inherit;
  font-size: 0.95rem;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
}
.submit-btn:hover:not(:disabled) { background: #2563eb; }
.submit-btn:disabled { opacity: 0.7; cursor: wait; }

.loader {
  width: 18px;
  height: 18px;
  border: 2px solid rgba(255, 255, 255, 0.35);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.error {
  color: #f87171;
  margin: 0.5rem 0 0;
  font-size: 0.88rem;
  text-align: center;
}

.empty {
  color: #94a3b8;
  margin: 0;
  font-size: 0.95rem;
}
.empty a, .links a, .signup-cta a {
  color: #22d3ee;
  text-decoration: none;
}
.empty a:hover, .links a:hover, .signup-cta a:hover {
  color: #3b82f6;
  text-decoration: underline;
}

.links {
  text-align: center;
  margin: 1rem 0 0;
  font-size: 0.85rem;
}

.signup-cta {
  text-align: center;
  margin: 1.5rem 0 0;
  padding-top: 1.25rem;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  font-size: 0.9rem;
  color: #94a3b8;
}

.tenant-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
.tenant-btn {
  width: 100%;
  text-align: left;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 10px;
  padding: 0.85rem 1rem;
  cursor: pointer;
  color: inherit;
  font: inherit;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  transition: background 0.15s, border-color 0.15s;
}
.tenant-btn:hover:not(:disabled) {
  background: rgba(59, 130, 246, 0.08);
  border-color: rgba(59, 130, 246, 0.4);
}
.tenant-btn:disabled { opacity: 0.6; cursor: wait; }
.tenant-name {
  font-weight: 600;
  font-size: 1rem;
  color: #f8fafc;
}
.tenant-slug, .tenant-role {
  font-size: 0.8rem;
  color: #94a3b8;
}
.tenant-slug { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.tenant-role { text-transform: capitalize; }

.retry-row {
  margin: 1rem 0 0;
  text-align: center;
}
.retry-btn {
  background: transparent;
  border: 1px solid #334155;
  color: #cbd5e1;
  border-radius: 6px;
  padding: 0.4rem 0.9rem;
  font: inherit;
  font-size: 0.85rem;
  cursor: pointer;
}
.retry-btn:hover { border-color: #3b82f6; color: #f8fafc; }
</style>

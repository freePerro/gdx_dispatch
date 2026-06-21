<script setup>
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { getPostLoginRedirect } from '../lib/auth-urls'
import PlatformRecovery from '../components/PlatformRecovery.vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const theme = useThemeStore()


const email = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)

// MH-0 (mobile hardening, audit P0 #1): show the recovery panel instead
// of the dead-end credential form when the host doesn't resolve to a
// tenant. Two entry conditions:
//  (a) the login attempt returns "Unknown tenant" — set in handleLogin
//  (b) the user clicks the "Wrong workspace?" escape link
// Either flips `showRecovery` true.
const showRecovery = ref(false)

// Heuristic: a literal "Unknown tenant" reply from the backend or any
// 404 originating from /auth/login indicates the host can never log in.
function _isTenantUnknownError(message) {
  if (!message) return false
  const m = String(message).toLowerCase()
  return m.includes('unknown tenant') || m.includes('no tenant context')
}

async function handleLogin() {
  error.value = ''

  if (!email.value.trim()) {
    error.value = 'Email is required'
    return
  }
  if (!password.value) {
    error.value = 'Password is required'
    return
  }

  submitting.value = true

  try {
    const result = await auth.login({
      email: email.value,
      password: password.value,
    })
    // Platform-host multi-tenant case: backend returned a list of tenants,
    // no token issued yet. The picker is rendered below; user clicks one
    // and we re-submit with tenant_id.
    if (result && result.status === 'select_tenant') {
      submitting.value = false
      return
    }
    // On platform host, single-tenant login triggered a full-page redirect
    // inside auth.login() — we won't reach here. The branch below is for
    // the per-tenant subdomain login path.
    await theme.loadBranding()
    const target = getPostLoginRedirect(route)
    if (target.startsWith('/oauth/')) {
      window.location.assign(target)
    } else {
      await router.push(target)
    }
  } catch (e) {
    const msg = e instanceof Error ? e.message : 'Unable to login'
    if (_isTenantUnknownError(msg)) {
      // Audit P0 #1: swap to the recovery panel rather than show an
      // unhelpful "Unknown tenant" inline error the user can't act on.
      showRecovery.value = true
      error.value = ''
    } else {
      error.value = msg
    }
  } finally {
    submitting.value = false
  }
}

async function pickTenant(tenantId) {
  error.value = ''
  submitting.value = true
  try {
    await auth.login({
      email: auth.pendingPlatformCreds?.email || email.value,
      password: auth.pendingPlatformCreds?.password || password.value,
      tenant_id: tenantId,
    })
    // Successful pick triggers a full-page redirect inside auth.login.
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Unable to login'
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <!-- MH-0: recovery panel — swapped in when the host has no tenant
           (audit P0 #1). Hides the credential form entirely; the user
           gets a workspace picker instead of a dead end. -->
      <PlatformRecovery v-if="showRecovery" />

      <template v-else>
      <div class="header">
        <h1>Sign In</h1>
        <p>Enter your credentials to access your workspace</p>
      </div>

      <div v-if="auth.tenantChoices && auth.tenantChoices.length > 1" class="tenant-picker" data-testid="tenant-picker">
        <p class="picker-prompt">You belong to multiple workspaces — pick one to continue:</p>
        <button
          v-for="t in auth.tenantChoices"
          :key="t.tenant_id"
          type="button"
          class="tenant-choice"
          :disabled="submitting"
          :data-testid="`tenant-choice-${t.slug}`"
          @click="pickTenant(t.tenant_id)"
        >
          <span class="tenant-name">{{ t.name }}</span>
          <span class="tenant-slug">{{ t.slug }}.example.com</span>
        </button>
        <p v-if="error" class="error-message" data-testid="login-error">{{ error }}</p>
      </div>

      <form @submit.prevent="handleLogin" class="login-form" data-testid="login-form">
        <div class="input-group">
          <label for="login-email">Email</label>
          <div class="input-wrapper">
            <svg class="input-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path>
              <polyline points="22,6 12,13 2,6"></polyline>
            </svg>
            <input
              id="login-email"
              v-model="email"
              type="email"
              placeholder="name@company.com"
              autocomplete="username"
              data-testid="login-email"
            />
          </div>
        </div>

        <div class="input-group">
          <label for="login-password">Password</label>
          <div class="input-wrapper">
            <svg class="input-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
              <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
            </svg>
            <input
              id="login-password"
              v-model="password"
              type="password"
              placeholder="••••••••"
              autocomplete="current-password"
              data-testid="login-password"
            />
          </div>
        </div>

        <button type="submit" class="submit-btn" :disabled="submitting" data-testid="login-submit">
          <span v-if="!submitting">Sign In</span>
          <span v-else class="loader"></span>
        </button>

        <p v-if="error" class="error-message" data-testid="login-error">{{ error }}</p>

        <p class="forgot-link">
          <router-link to="/forgot-password">Forgot your password?</router-link>
        </p>
        <!-- MH-0 escape: explicit way to reach the workspace picker
             without having to fail a login attempt first. Quiet styling
             so it doesn't compete with primary actions. -->
        <p class="wrong-workspace-link">
          <button
            type="button"
            class="link-button"
            data-testid="wrong-workspace"
            @click="showRecovery = true"
          >Wrong workspace?</button>
        </p>
      </form>
      </template>
    </div>
  </div>
</template>

<style scoped>
.tenant-picker {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-top: 8px;
}
.picker-prompt {
  margin: 0 0 8px;
  font-size: 0.95rem;
  text-align: center;
  opacity: 0.85;
}
.tenant-choice {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 12px 16px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  color: inherit;
  cursor: pointer;
  text-align: left;
  transition: background 0.15s, border-color 0.15s;
}
.tenant-choice:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
  border-color: var(--primary, #e94560);
}
.tenant-choice:disabled { opacity: 0.5; cursor: wait; }
.tenant-name { font-weight: 600; font-size: 1rem; }
.tenant-slug { font-size: 0.8rem; opacity: 0.7; font-family: monospace; }

.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: radial-gradient(ellipse at top left, #1e293b, #0f172a 70%);
  padding: 20px;
}

.login-card {
  width: 100%;
  max-width: 420px;
  background: var(--card, #1e293b);
  color: var(--text, #f8fafc);
  padding: 2.5rem;
  border-radius: 16px;
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
  border: 1px solid rgba(255, 255, 255, 0.06);
}

.header {
  text-align: center;
  margin-bottom: 2rem;
}

.header h1 {
  font-size: 1.75rem;
  font-weight: 700;
  margin: 0 0 0.5rem 0;
}

.header p {
  color: #94a3b8;
  font-size: 0.875rem;
  margin: 0;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.input-group {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.input-group label {
  font-size: 0.875rem;
  font-weight: 500;
}

.input-wrapper {
  position: relative;
  display: flex;
  align-items: center;
}

.input-icon {
  position: absolute;
  left: 12px;
  width: 18px;
  height: 18px;
  color: #64748b;
  pointer-events: none;
}

input {
  width: 100%;
  padding: 0.75rem 0.75rem 0.75rem 2.5rem;
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 8px;
  color: var(--text, #f8fafc);
  font: inherit;
  font-size: 0.9375rem;
  transition: border-color 0.2s, box-shadow 0.2s;
}

input:focus {
  outline: none;
  border-color: var(--primary, #3b82f6);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}

input::placeholder {
  color: #475569;
}

.submit-btn {
  margin-top: 0.5rem;
  padding: 0.75rem;
  background: var(--primary, #3b82f6);
  color: #fff;
  border: none;
  border-radius: 8px;
  font: inherit;
  font-size: 1rem;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  justify-content: center;
  align-items: center;
  transition: background 0.2s;
}

.submit-btn:hover:not(:disabled) {
  background: #2563eb;
}

.submit-btn:disabled {
  opacity: 0.7;
  cursor: not-allowed;
}

.error-message {
  color: #f87171;
  font-size: 0.875rem;
  text-align: center;
  margin: 0;
}

.loader {
  width: 20px;
  height: 20px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-radius: 50%;
  border-top-color: #fff;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.forgot-link {
  text-align: center;
  margin-top: 1rem;
  font-size: 0.85rem;
}
.forgot-link a {
  color: #3b82f6;
  text-decoration: none;
}
.forgot-link a:hover {
  text-decoration: underline;
}

.wrong-workspace-link {
  text-align: center;
  margin: 0.5rem 0 0;
  font-size: 0.8rem;
}
.link-button {
  background: none;
  border: none;
  padding: 0;
  color: #94a3b8;
  font: inherit;
  font-size: 0.8rem;
  cursor: pointer;
  text-decoration: underline dotted;
}
.link-button:hover {
  color: #cbd5e1;
}
</style>

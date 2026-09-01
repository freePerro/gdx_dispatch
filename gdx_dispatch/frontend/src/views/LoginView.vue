<script setup>
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { getPostLoginRedirect } from '../lib/auth-urls'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const theme = useThemeStore()


const email = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)

// The multi-tenant workspace picker that used to live here was removed
// 2026-09-01 (Phase D S2). It answered "Unknown tenant" — a reply Phase A
// deleted from the backend, so condition (a) could never fire — and its
// manual escape link sent single-tenant users to a subdomain picker for
// workspaces that do not exist. See docs/design/phase-d-saas-residue.md.

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
    await auth.login({
      email: email.value,
      password: password.value,
    })
    await theme.loadBranding()
    const target = getPostLoginRedirect(route)
    if (target.startsWith('/oauth/')) {
      window.location.assign(target)
    } else {
      await router.push(target)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Unable to login'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="header">
        <h1>Sign In</h1>
        <p>Enter your credentials to access your workspace</p>
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
      </form>
    </div>
  </div>
</template>

<style scoped>

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
  /* >=16px on purpose. iOS Safari auto-zooms any focused input below 16px and
     does NOT zoom back out, leaving the user on a zoomed, side-panning page —
     on the first screen of the app. This form is hand-rolled rather than
     PrimeVue (whose .p-inputtext is already 1rem), so it needs the literal. */
  font-size: 1rem;
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

</style>

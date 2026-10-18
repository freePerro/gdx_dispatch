<script setup>
import { onMounted, ref } from 'vue'
import { Capacitor } from '@capacitor/core'
import {
  checkBiometricAvailability,
  forgetFingerprintSignIn,
  isFingerprintSignInSaved,
  readFingerprintSignIn,
  saveFingerprintSignIn,
} from '../lib/nativeAuth'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { getPostLoginRedirect } from '../lib/auth-urls'
import MobileServerConfigDialog from '../components/MobileServerConfigDialog.vue'
import { getServerUrl } from '../lib/serverConfig'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const theme = useThemeStore()

const email = ref('')
const password = ref('')
const error = ref('')
// Sent here by a session that ended without the person choosing it
// (router/index.js, useApi): say so, instead of a login page that looks like
// they were never signed in.
const sessionExpired = route.query.reason === 'expired'
const submitting = ref(false)

// Native / self-hosted server configuration modal
const showServerConfig = ref(false)
// Server choice is an Android-app setting; the web app talks to its own host.
const isNativeApp = Capacitor.isNativePlatform()
const activeServerUrl = ref(isNativeApp ? getServerUrl() : '')

// Fingerprint sign-in (Android app only): the password lives in the Keystore
// behind a biometric check, saved per server — see lib/nativeAuth.js.
const fingerprintAvailable = ref(false)
const fingerprintSaved = ref(false)
const rememberFingerprint = ref(true)

async function refreshFingerprintState() {
  if (!Capacitor.isNativePlatform()) return
  fingerprintAvailable.value = (await checkBiometricAvailability()).available
  fingerprintSaved.value =
    fingerprintAvailable.value && (await isFingerprintSignInSaved(activeServerUrl.value))
}

onMounted(refreshFingerprintState)

function onServerSaved(savedUrl) {
  activeServerUrl.value = savedUrl
  refreshFingerprintState()
}

async function finishLogin() {
  await theme.loadBranding()
  // GDXA-1003: an account that must change its password (the seeded owner,
  // an invited user) goes to the change-password form on /profile — the
  // server refuses everything else — and not to the remembered target,
  // which for /oauth/ is a full-page load the router guard never sees.
  if (auth.user?.must_change_password) {
    await router.push('/profile')
    return
  }
  const target = getPostLoginRedirect(route)
  if (target.startsWith('/oauth/')) {
    window.location.assign(target)
  } else {
    await router.push(target)
  }
}

async function handleFingerprintLogin() {
  error.value = ''
  let creds
  try {
    creds = await readFingerprintSignIn(activeServerUrl.value)
  } catch (e) {
    // Code 21: the Keystore entry is gone (e.g. biometrics were reset).
    if (String(e?.code) === '21' || /no protected credentials/i.test(e?.message || '')) {
      await forgetFingerprintSignIn(activeServerUrl.value)
      fingerprintSaved.value = false
      error.value = 'Fingerprint sign-in was reset on this phone. Sign in with your password.'
    }
    return // otherwise the prompt was cancelled: stay on the form
  }

  submitting.value = true
  try {
    await auth.login(creds)
    await finishLogin()
  } catch (e) {
    if (e?.status === 401) {
      // The saved password no longer works (changed or account disabled).
      await forgetFingerprintSignIn(activeServerUrl.value)
      fingerprintSaved.value = false
      email.value = creds.email
      error.value = 'Your saved sign-in no longer works. Enter your password to set fingerprint sign-in up again.'
    } else {
      error.value = e instanceof Error ? e.message : 'Unable to login'
    }
  } finally {
    submitting.value = false
  }
}

async function handleForgetFingerprint() {
  await forgetFingerprintSignIn(activeServerUrl.value)
  fingerprintSaved.value = false
}

// The multi-tenant workspace picker that used to live here was removed
// 2026-09-01 (Phase D S2). It answered "Unknown tenant" — a reply Phase A
// deleted from the backend, so condition (a) could never fire — and its
// manual escape link sent single-tenant users to a subdomain picker for
// workspaces that do not exist.

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
  // The Android app serves its UI from the device, so with no server set the
  // login request hits the bundled files and fails with a JSON parse error.
  if (Capacitor.isNativePlatform() && !getServerUrl()) {
    error.value = 'Set your server first — tap the gear above.'
    showServerConfig.value = true
    return
  }

  submitting.value = true

  try {
    await auth.login({
      email: email.value,
      password: password.value,
    })
    // Not for a password about to be replaced (GDXA-1003): the saved one would
    // stop working the moment it is changed.
    if (fingerprintAvailable.value && !fingerprintSaved.value && rememberFingerprint.value
      && !auth.user?.must_change_password) {
      try {
        // Shows the system fingerprint prompt; declining it just skips setup.
        await saveFingerprintSignIn(activeServerUrl.value, email.value.trim(), password.value)
      } catch {
        /* declined or failed: sign-in itself already succeeded */
      }
    }
    await finishLogin()
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
      <button
        v-if="isNativeApp"
        type="button"
        class="server-gear-btn"
        @click="showServerConfig = true"
        aria-label="Server Settings"
        title="Server Settings"
        data-testid="server-settings-gear"
      >
        <svg class="gear-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3"></circle>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
        </svg>
      </button>

      <div class="header">
        <h1>Sign In</h1>
        <p>Enter your credentials to continue</p>
        <div v-if="activeServerUrl" class="active-server-indicator" @click="showServerConfig = true" title="Click to change server">
          <i class="pi pi-server"></i>
          <span>{{ activeServerUrl }}</span>
        </div>
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

        <label
          v-if="fingerprintAvailable && !fingerprintSaved"
          class="remember-fingerprint"
          data-testid="login-remember-fingerprint"
        >
          <input v-model="rememberFingerprint" type="checkbox" />
          <span>Use fingerprint to sign in next time</span>
        </label>

        <template v-if="fingerprintSaved">
          <div class="or-divider"><span>or</span></div>
          <button
            type="button"
            class="fingerprint-btn"
            :disabled="submitting"
            data-testid="login-fingerprint"
            @click="handleFingerprintLogin"
          >
            <i class="pi pi-lock-open" aria-hidden="true"></i>
            <span>Sign in with fingerprint</span>
          </button>
        </template>

        <p v-if="error" class="error-message" data-testid="login-error">{{ error }}</p>
        <p v-else-if="sessionExpired" class="session-expired-message" data-testid="login-session-expired" role="status">
          Your session expired. Sign in again to pick up where you left off.
        </p>

        <p v-if="fingerprintSaved" class="forgot-link">
          <button type="button" class="link-btn" data-testid="login-forget-fingerprint" @click="handleForgetFingerprint">
            Turn off fingerprint sign-in on this phone
          </button>
        </p>

        <p class="forgot-link">
          <router-link to="/forgot-password">Forgot your password?</router-link>
        </p>
      </form>

      <MobileServerConfigDialog v-if="isNativeApp" v-model:visible="showServerConfig" @saved="onServerSaved" />
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
  position: relative;
  width: 100%;
  max-width: 420px;
  background: var(--card, #1e293b);
  color: var(--text, #f8fafc);
  padding: 2.5rem;
  border-radius: 16px;
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
  border: 1px solid rgba(255, 255, 255, 0.06);
}

.server-gear-btn {
  position: absolute;
  top: 1.25rem;
  right: 1.25rem;
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 8px;
  width: 38px;
  height: 38px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #94a3b8;
  cursor: pointer;
  transition: all 0.2s;
}

.server-gear-btn:hover {
  background: rgba(255, 255, 255, 0.06);
  border-color: #3b82f6;
  color: #3b82f6;
}

.gear-icon {
  width: 18px;
  height: 18px;
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

.active-server-indicator {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  margin-top: 0.6rem;
  padding: 0.25rem 0.6rem;
  border-radius: 9999px;
  background: rgba(59, 130, 246, 0.12);
  border: 1px solid rgba(59, 130, 246, 0.25);
  color: #60a5fa;
  font-size: 0.75rem;
  cursor: pointer;
  max-width: 100%;
}

.active-server-indicator span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

/* The icon gutter. Scoped `input` compiles to (0,1,1), the same specificity as
   responsive.css's mobile `input[type="email"], input[type="password"]
   { padding: 8px 12px }`, which then wins below 768px and puts the text under
   the icon. The wrapper class lifts this to (0,2,1). */
.input-wrapper input {
  padding-left: 2.5rem;
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

.session-expired-message {
  color: #fbbf24;
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

.remember-fingerprint {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  color: #cbd5e1;
  font-size: 0.9rem;
  cursor: pointer;
}

.remember-fingerprint input {
  width: 1.1rem;
  height: 1.1rem;
  accent-color: var(--primary, #3b82f6);
}

.or-divider {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  color: #64748b;
  font-size: 0.8rem;
}

.or-divider::before,
.or-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: #334155;
}

.fingerprint-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.6rem;
  width: 100%;
  padding: 0.75rem;
  background: transparent;
  color: #e2e8f0;
  border: 1px solid #475569;
  border-radius: 8px;
  font-size: 1rem;
  font-weight: 600;
  cursor: pointer;
}

.fingerprint-btn:hover:not(:disabled) {
  border-color: var(--primary, #3b82f6);
  color: #fff;
}

.fingerprint-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.link-btn {
  background: none;
  border: none;
  padding: 0;
  color: #94a3b8;
  font-size: 0.85rem;
  text-decoration: underline;
  cursor: pointer;
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

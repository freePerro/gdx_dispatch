<script setup>
/**
 * PlatformRecovery — mounted when login fails with "Unknown tenant" (the
 * platform/unresolved-host case). Replaces the dead-end credential form
 * with an actionable workspace picker.
 *
 * Audit P0 #1 (mobile UX audit 2026-05-19): gdx.example.com
 * presented a fully-styled login that always 404'd. A user mistyping
 * a subdomain or following a stale bookmark/marketing link had no
 * recovery path. This component is that recovery path.
 *
 * Behavior:
 *  - User types their workspace slug (or full subdomain)
 *  - We normalize and redirect to `https://<slug>.example.com/login`
 *  - "Don't have a workspace yet?" → /signup
 *  - "Learn more" → https://example.com (marketing)
 */
import { ref, computed } from 'vue'

const slug = ref('')
const submitting = ref(false)

const BASE_DOMAIN = 'example.com'

// Permit `gdx`, `gdx.example.com`, `https://gdx.example.com`.
// Normalize to just the slug portion.
const normalizedSlug = computed(() => {
  const v = slug.value.trim().toLowerCase()
  if (!v) return ''
  // Strip protocol and trailing path
  let s = v.replace(/^https?:\/\//, '').replace(/\/.*$/, '')
  // If a full host was pasted, take the leftmost label
  if (s.includes('.')) s = s.split('.')[0]
  // Whitelist slug characters
  return s.replace(/[^a-z0-9-]/g, '')
})

const canSubmit = computed(() => normalizedSlug.value.length >= 2 && !submitting.value)

const targetUrl = computed(() => {
  if (!normalizedSlug.value) return ''
  return `https://${normalizedSlug.value}.${BASE_DOMAIN}/login`
})

function goToWorkspace() {
  if (!canSubmit.value) return
  submitting.value = true
  // Full-page navigation — leaves the current host (which has no tenant)
  // and lands on the tenant subdomain. window.location.assign so the
  // back button still gets us here.
  window.location.assign(targetUrl.value)
}
</script>

<template>
  <div class="recovery" data-testid="platform-recovery">
    <div class="header">
      <h1>Choose your workspace</h1>
      <p class="lede">
        This URL isn't tied to an active workspace.
        Enter your workspace name to continue.
      </p>
    </div>

    <form class="form" @submit.prevent="goToWorkspace">
      <div class="input-group">
        <label for="recovery-slug">Workspace</label>
        <div class="slug-row">
          <input
            id="recovery-slug"
            v-model="slug"
            type="text"
            placeholder="your-workspace"
            autocomplete="organization"
            spellcheck="false"
            autocapitalize="off"
            data-testid="recovery-slug"
          />
          <span class="domain-suffix">.{{ BASE_DOMAIN }}</span>
        </div>
      </div>

      <button
        type="submit"
        class="submit-btn"
        :disabled="!canSubmit"
        data-testid="recovery-submit"
      >
        <span v-if="!submitting">Go to my workspace</span>
        <span v-else class="loader" />
      </button>

      <p v-if="normalizedSlug" class="target-hint" data-testid="recovery-target">
        We'll send you to <code>{{ targetUrl }}</code>
      </p>
    </form>

    <div class="links">
      <p>
        Don't have a workspace yet?
        <router-link to="/signup" data-testid="recovery-signup">Create one</router-link>
      </p>
      <p>
        <a
          href="https://example.com"
          data-testid="recovery-marketing"
        >Learn about DispatchApp</a>
      </p>
    </div>
  </div>
</template>

<style scoped>
.recovery {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}
.header { text-align: center; }
.header h1 {
  font-size: 1.5rem;
  font-weight: 700;
  margin: 0 0 0.5rem 0;
}
.lede {
  color: #94a3b8;
  font-size: 0.9rem;
  margin: 0;
  line-height: 1.4;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 1rem;
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
.slug-row {
  display: flex;
  align-items: stretch;
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 8px;
  overflow: hidden;
}
.slug-row input {
  flex: 1 1 auto;
  min-width: 0;
  padding: 0.75rem;
  background: transparent;
  border: 0;
  color: inherit;
  font: inherit;
  font-size: 0.9375rem;
}
.slug-row input:focus {
  outline: none;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}
.domain-suffix {
  display: inline-flex;
  align-items: center;
  padding: 0 0.75rem;
  background: #1e293b;
  color: #94a3b8;
  font-size: 0.85rem;
  font-family: monospace;
  border-left: 1px solid #334155;
}
.submit-btn {
  padding: 0.75rem;
  /* Brand-blue primary (MH-2 contrast policy). Do not switch to
     PrimeVue severity="success" here — that emerald fails WCAG AA. */
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
  opacity: 0.55;
  cursor: not-allowed;
}
.target-hint {
  font-size: 0.8rem;
  color: #94a3b8;
  text-align: center;
  margin: 0;
}
.target-hint code {
  font-family: monospace;
  color: #cbd5e1;
  word-break: break-all;
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
.links {
  text-align: center;
  font-size: 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.links p { margin: 0; }
.links a {
  color: #3b82f6;
  text-decoration: none;
}
.links a:hover { text-decoration: underline; }
</style>

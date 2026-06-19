import { createApp } from 'vue';
import { createPinia } from 'pinia';
import PrimeVue from 'primevue/config';
import ToastService from 'primevue/toastservice';
import ConfirmationService from 'primevue/confirmationservice';
import Aura from '@primeuix/themes/aura';
import 'primeicons/primeicons.css';
import * as Sentry from '@sentry/vue';
import App from './App.vue';
import { createAppRouter } from './router';
import { installErrorCapture } from './plugins/errorCapture';
import './assets/base.css';
import './assets/responsive.css';
// MH-2: must load AFTER base.css and AFTER PrimeVue's own preset so its
// :root vars override the Aura defaults. Aliases button-success tokens
// to button-primary (brand-blue) to fix the WCAG-failing 2.53:1 white-
// on-emerald CTA contrast app-wide.
import './assets/primevue-cta-contrast.css';

// Platform-host login hand-off: when the user signs in at
// app.example.com they're redirected to <slug>.example.com
// with the access token in a URL fragment. Read it FIRST (before the
// auth store reads sessionStorage) so the SPA boots authenticated.
(function _consumeHandoffFragment() {
  try {
    const m = (window.location.hash || '').match(/[#&]gdx_handoff=([^&]+)/);
    if (!m) return;
    const decoded = JSON.parse(atob(decodeURIComponent(m[1])));
    if (decoded && decoded.t) {
      sessionStorage.setItem('gdx_access_token', decoded.t);
      if (decoded.s) sessionStorage.setItem('gdx_tenant_slug', decoded.s);
      if (decoded.u) sessionStorage.setItem('gdx_user', JSON.stringify(decoded.u));
    }
  } catch (_) { /* malformed fragment — ignore, user will land on login */ }
  if (window.location.hash.includes('gdx_handoff=')) {
    const cleanHash = window.location.hash.replace(/[#&]gdx_handoff=[^&]+/, '');
    history.replaceState(null, '', window.location.pathname + window.location.search + cleanHash);
  }
})();

const app = createApp(App);
installErrorCapture(app);
const pinia = createPinia();
const router = createAppRouter();

// Sentry error tracking — only init in production or when DSN is provided
const sentryDsn = import.meta.env.VITE_SENTRY_DSN || '';
if (sentryDsn) {
  Sentry.init({
    app,
    dsn: sentryDsn,
    integrations: [
      Sentry.browserTracingIntegration({ router }),
      Sentry.replayIntegration(),
    ],
    tracesSampleRate: 0.1,  // 10% of transactions
    replaysSessionSampleRate: 0.0,  // Don't record all sessions
    replaysOnErrorSampleRate: 1.0,  // Record 100% of sessions with errors
    environment: import.meta.env.MODE,
  });
}

app.use(pinia);
app.use(router);
app.use(PrimeVue, {
  theme: {
    preset: Aura,
    options: {
      darkModeSelector: '[data-theme="dark"]',
    },
  },
});
app.use(ToastService);
app.use(ConfirmationService);

import('./lib/analytics').then(({ installAnalytics }) => installAnalytics());

app.mount('#app');

// Phase 1.5 E1 — register the service worker. Idempotent; harmless if
// the browser doesn't support push (returns null). The actual Web Push
// subscribe step happens later, behind a user gesture (the
// "Enable notifications" CTA in the mobile shell).
import('./composables/usePushSubscription').then(({ registerServiceWorker }) => {
  registerServiceWorker().catch(() => {
    /* swallow — push is best-effort, never fail the app boot */
  });
});

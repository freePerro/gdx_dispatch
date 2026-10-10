import { createApp } from 'vue';
import { createPinia } from 'pinia';
import PrimeVue from 'primevue/config';
import ToastService from 'primevue/toastservice';
import ConfirmationService from 'primevue/confirmationservice';
import Tooltip from 'primevue/tooltip';
import Aura from '@primeuix/themes/aura';
import 'primeicons/primeicons.css';
import App from './App.vue';
import { createAppRouter } from './router';
import { startKeyboardInsetTracking } from './lib/keyboardInset';
import { installErrorCapture } from './plugins/errorCapture';
import { installSwNavigation } from './lib/swNavigation';
import './assets/base.css';
import './assets/responsive.css';
// MH-2: must load AFTER base.css and AFTER PrimeVue's own preset so its
// :root vars override the Aura defaults. Aliases button-success tokens
// to button-primary (brand-blue) to fix the WCAG-failing 2.53:1 white-
// on-emerald CTA contrast app-wide.
import './assets/primevue-cta-contrast.css';

const app = createApp(App);
installErrorCapture(app);
const pinia = createPinia();
const router = createAppRouter();

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
app.directive('tooltip', Tooltip);

import('./lib/analytics').then(({ installAnalytics }) => installAnalytics());

// Publishes --keyboard-inset so a fullscreen dialog's Save button can rise
// above the software keyboard on iOS, where the layout viewport does not shrink
// (index.html handles Chromium/Gecko via interactive-widget). No-op without
// visualViewport support.
startKeyboardInsetTracking();

// A tapped push notification routes in-app rather than reloading the tab
// (public/sw.js posts the route here; it falls back to navigate()).
installSwNavigation(router);

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

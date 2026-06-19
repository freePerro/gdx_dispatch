// Vitest global setup — registers the PrimeVue plugin and ConfirmationService
// so individual test mounts don't have to. Closes the 11-fail baseline that
// has been parked since S98: the failures are all variants of
// `Property "$primevue" was accessed during render but is not defined on
// instance` from PrimeVue components (InputNumber, Textarea, etc.) that the
// per-test stub map didn't cover. Globally installing the plugin removes the
// per-test boilerplate burden and makes new tests work out of the box.
//
// We register both PrimeVue (provides $primevue) and ConfirmationService
// (required by useDestructiveConfirm in many views). Tests that want a
// custom theme can override via per-mount `global.plugins`.
import { config } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';

config.global.plugins = [
  ...(config.global.plugins || []),
  [PrimeVue, { unstyled: true }],
  ConfirmationService,
  ToastService,
];

// jsdom doesn't ship matchMedia or ResizeObserver — PrimeVue v4
// components (Select uses matchMedia for orientation listener; Tabs
// uses ResizeObserver for tab width tracking) crash without them.
// Stub at the global level so per-test boilerplate disappears.
if (typeof window !== 'undefined') {
  if (!window.matchMedia) {
    window.matchMedia = (query) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    });
  }
  if (!window.ResizeObserver) {
    window.ResizeObserver = class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
}

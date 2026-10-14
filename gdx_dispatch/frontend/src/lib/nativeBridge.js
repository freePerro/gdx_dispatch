/**
 * nativeBridge.js — Hardware integration for Capacitor Android app.
 *
 * Owner Domain: mobile-tech (see .claude/agents/mobile-tech.md)
 *
 * Features:
 * - Hardware Back Button: Intercepts Android system back gesture to dismiss open
 *   PrimeVue modals/drawers first, navigate back in Vue Router second, or exit app
 *   if at the root view.
 * - App Lifecycle: Handles background/resume transitions.
 */

import { Capacitor } from '@capacitor/core';
import { App } from '@capacitor/app';

let _bridgeInstalled = false;

export function initNativeAppBridge(router) {
  if (_bridgeInstalled || !Capacitor.isNativePlatform()) {
    return;
  }

  // Intercept hardware / gesture back button
  App.addListener('backButton', ({ canGoBack }) => {
    // 1. Check if any modal dialog or drawer is currently open in the DOM
    const openMask = document.querySelector(
      '.p-dialog-mask:not([style*="display: none"]), .p-drawer-mask:not([style*="display: none"]), .server-config-mask'
    );
    if (openMask) {
      // Dispatch Escape key to dismiss dialog/drawer through standard PrimeVue handling
      const escEvent = new KeyboardEvent('keydown', {
        key: 'Escape',
        code: 'Escape',
        keyCode: 27,
        bubbles: true,
        cancelable: true,
      });
      document.dispatchEvent(escEvent);
      return;
    }

    // 2. Check Vue router path
    const currentPath = router.currentRoute.value.path;
    const isRootView = currentPath === '/mobile' || currentPath === '/login' || currentPath === '/dashboard';

    if (!isRootView && window.history.length > 1) {
      router.back();
    } else {
      App.exitApp();
    }
  });

  _bridgeInstalled = true;
}

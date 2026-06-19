import { computed, ref } from 'vue';
import { defineStore } from 'pinia';

const defaultBranding = {
  company_name: 'GDX Platform',
  logo_url: '',
  primary_color: '#e94560',
  accent_color: '#4fc3f7',
  sidebar_items: [
    { label: 'Dashboard', to: '/dashboard' },
    { label: 'Jobs', to: '/jobs' },
    { label: 'Customers', to: '/customers' },
    { label: 'Dispatch', to: '/dispatch' },
    { label: 'Estimates', to: '/estimates' },
    { label: 'Billing', to: '/billing' },
    { label: 'Settings', to: '/settings' },
  ],
};

export const useThemeStore = defineStore('theme', () => {
  const branding = ref({ ...defaultBranding });

  // Dark/light mode
  const savedTheme = typeof localStorage !== 'undefined' ? localStorage.getItem('gdx_theme') : null;
  const colorMode = ref(savedTheme || 'auto'); // 'dark' | 'light' | 'auto'

  const effectiveMode = computed(() => {
    if (colorMode.value !== 'auto') return colorMode.value;
    if (typeof window !== 'undefined' && window.matchMedia) {
      return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }
    return 'dark';
  });

  function setColorMode(mode) {
    colorMode.value = mode;
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('gdx_theme', mode);
    }
    applyColorMode();
  }

  function toggleColorMode() {
    const next = effectiveMode.value === 'dark' ? 'light' : 'dark';
    setColorMode(next);
  }

  function applyColorMode() {
    if (typeof document === 'undefined') return;
    const mode = effectiveMode.value;
    document.documentElement.setAttribute('data-theme', mode);
  }

  const sidebarItems = computed(() => branding.value.sidebar_items || []);

  function applyThemeVars() {
    const root = document.documentElement;
    root.style.setProperty('--primary', branding.value.primary_color || defaultBranding.primary_color);
    root.style.setProperty('--accent', branding.value.accent_color || defaultBranding.accent_color);
    applyColorMode();
  }

  async function loadBranding() {
    try {
      const token = sessionStorage.getItem('gdx_access_token');
      const stored = sessionStorage.getItem('gdx_tenant_slug');
      let tenantId;
      if (stored) {
        tenantId = stored;
      } else {
        const parts = window.location.hostname.split('.');
        const slug = parts.length >= 3 ? parts[0] : null;
        tenantId = slug || 'default';
      }

      const _hdrs = { 'x-tenant-id': tenantId };
      if (token) _hdrs.Authorization = `Bearer ${token}`;
      const response = await fetch('/api/settings/branding', {
        method: 'GET',
        credentials: 'include',
        headers: _hdrs,
      });

      if (!response.ok) {
        throw new Error('Branding request failed');
      }

      const data = await response.json();
      branding.value = {
        ...defaultBranding,
        ...data,
        sidebar_items: Array.isArray(data.sidebar_items)
          ? data.sidebar_items
          : defaultBranding.sidebar_items,
      };
    } catch (_error) {
      // Branding request failed. The previous fallback tried /api/tenant,
      // which was never routed on the backend — both branches resulted in
      // default branding. Dropping the dead fallback keeps behavior
      // identical while removing a phantom 404 per page load.
      branding.value = { ...defaultBranding };
    }

    applyThemeVars();
    return branding.value;
  }

  return {
    branding,
    sidebarItems,
    colorMode,
    effectiveMode,
    applyThemeVars,
    loadBranding,
    setColorMode,
    toggleColorMode,
    applyColorMode,
  };
});

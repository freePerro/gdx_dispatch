import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useThemeStore } from '../src/stores/theme';

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

describe('theme store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    document.documentElement.removeAttribute('style');
    document.documentElement.removeAttribute('data-theme');
  });

  it('loads branding from API', async () => {
    const branding = {
      company_name: 'Tenant Co',
      logo_url: '/logo.png',
      primary_color: '#111111',
      accent_color: '#222222',
      sidebar_items: [{ label: 'Dashboard', to: '/' }],
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, branding)));

    const theme = useThemeStore();
    const loaded = await theme.loadBranding();

    expect(loaded.company_name).toBe('Tenant Co');
    expect(theme.branding.company_name).toBe('Tenant Co');
    expect(theme.sidebarItems).toHaveLength(1);
  });

  it('applies CSS variables from branding', () => {
    const theme = useThemeStore();
    theme.branding = {
      company_name: 'Tenant Co',
      logo_url: '/logo.png',
      primary_color: '#123456',
      accent_color: '#abcdef',
      sidebar_items: [],
    };

    theme.applyThemeVars();

    expect(document.documentElement.style.getPropertyValue('--primary')).toBe('#123456');
    expect(document.documentElement.style.getPropertyValue('--accent')).toBe('#abcdef');
  });

  it('handles missing branding by using defaults', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(500, { detail: 'no branding' })));

    const theme = useThemeStore();
    const branding = await theme.loadBranding();

    expect(branding.company_name).toBe('GDX Platform');
    expect(branding.sidebar_items.length).toBeGreaterThan(0);
  });
});

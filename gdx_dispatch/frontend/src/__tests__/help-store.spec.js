// Help store unit tests — covers the slice-1 acceptance criteria.
// Drawer rendering + full search is covered by the component test;
// this file focuses on store invariants that any caller depends on.

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const SAMPLE_INDEX = {
  generated_at: '2026-05-10T00:00:00Z',
  articles: [
    {
      slug: 'welcome',
      title: 'Welcome to DispatchApp',
      role: 'all',
      tags: ['intro'],
      related: ['create-a-job'],
      module: null,
      body: '# Welcome\n\nHello world.',
    },
    {
      slug: 'create-a-job',
      title: 'Create a job',
      role: 'dispatcher',
      tags: ['jobs', 'dispatch'],
      related: ['welcome'],
      module: 'jobs',
      body: '## Create a job\n\nSteps.',
    },
  ],
};

beforeEach(() => {
  setActivePinia(createPinia());
  global.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(SAMPLE_INDEX) }),
  );
  // Reset module cache so the singleton index loader re-runs per test.
  vi.resetModules();
});

describe('help store', () => {
  it('loads the index on first open', async () => {
    const { useHelpStore } = await import('../stores/help');
    const store = useHelpStore();
    expect(store.isOpen).toBe(false);
    await store.open();
    expect(store.isOpen).toBe(true);
    expect(store.articles.length).toBe(2);
    expect(global.fetch).toHaveBeenCalledWith('/help-index.json', expect.any(Object));
  });

  it('open(slug) shows that article', async () => {
    const { useHelpStore } = await import('../stores/help');
    const store = useHelpStore();
    await store.open('create-a-job');
    expect(store.currentSlug).toBe('create-a-job');
    expect(store.currentArticle?.title).toBe('Create a job');
  });

  it('renders sanitized markdown HTML', async () => {
    const { useHelpStore } = await import('../stores/help');
    const store = useHelpStore();
    await store.open('welcome');
    const html = store.renderedHtml;
    expect(html).toContain('Welcome');
    expect(html).toContain('<h1');
    expect(html).toContain('<p>');
  });

  it('search returns ranked results', async () => {
    const { useHelpStore } = await import('../stores/help');
    const store = useHelpStore();
    await store.open();
    store.search('job');
    expect(store.searchResults.length).toBeGreaterThan(0);
    expect(store.searchResults[0].slug).toBe('create-a-job');
  });

  it('search with short query returns no results', async () => {
    const { useHelpStore } = await import('../stores/help');
    const store = useHelpStore();
    await store.open();
    store.search('j');
    expect(store.searchResults).toEqual([]);
  });

  it('backToSearch clears the current slug', async () => {
    const { useHelpStore } = await import('../stores/help');
    const store = useHelpStore();
    await store.open('create-a-job');
    store.backToSearch();
    expect(store.currentSlug).toBeNull();
    expect(store.currentArticle).toBeNull();
  });

  it('close sets isOpen false', async () => {
    const { useHelpStore } = await import('../stores/help');
    const store = useHelpStore();
    await store.open();
    store.close();
    expect(store.isOpen).toBe(false);
  });
});

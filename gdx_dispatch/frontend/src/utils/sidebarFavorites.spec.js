import { describe, it, expect } from 'vitest';
import { resolveInitialFavorites, DEFAULT_FAVORITES } from './sidebarFavorites';

describe('resolveInitialFavorites', () => {
  it('seeds defaults and asks to persist when localStorage has never been written', () => {
    const { favorites, shouldPersist } = resolveInitialFavorites(null);
    expect(favorites).toEqual(DEFAULT_FAVORITES);
    expect(shouldPersist).toBe(true);
  });

  it('seeds defaults when value is undefined (defensive)', () => {
    const { favorites, shouldPersist } = resolveInitialFavorites(undefined);
    expect(favorites).toEqual(DEFAULT_FAVORITES);
    expect(shouldPersist).toBe(true);
  });

  it('returns a fresh array (not the module-level constant) so callers can mutate safely', () => {
    const { favorites } = resolveInitialFavorites(null);
    expect(favorites).not.toBe(DEFAULT_FAVORITES);
  });

  it('respects an explicit empty list — does NOT re-seed defaults', () => {
    const { favorites, shouldPersist } = resolveInitialFavorites('[]');
    expect(favorites).toEqual([]);
    expect(shouldPersist).toBe(false);
  });

  it('returns user favorites verbatim when stored', () => {
    const stored = [{ to: '/jobs', label: 'Jobs', icon: 'pi pi-briefcase' }];
    const { favorites, shouldPersist } = resolveInitialFavorites(JSON.stringify(stored));
    expect(favorites).toEqual(stored);
    expect(shouldPersist).toBe(false);
  });

  it('falls back to empty list (no persist) when stored value is malformed JSON', () => {
    const { favorites, shouldPersist } = resolveInitialFavorites('{not-json');
    expect(favorites).toEqual([]);
    expect(shouldPersist).toBe(false);
  });

  it('falls back to empty list when stored value is JSON but not an array', () => {
    const { favorites, shouldPersist } = resolveInitialFavorites('{"foo":"bar"}');
    expect(favorites).toEqual([]);
    expect(shouldPersist).toBe(false);
  });

  it('default favorites contain Onboarding pointing to /onboarding', () => {
    const onboarding = DEFAULT_FAVORITES.find((f) => f.to === '/onboarding');
    expect(onboarding).toBeDefined();
    expect(onboarding.label).toBe('Onboarding');
  });
});

// End-to-end-ish: simulate the AppSidebar load path against jsdom's
// localStorage. Proves the seed actually lands in storage on first load and
// is read back unchanged on the second load (no double-seed, no clobber).
describe('sidebar favorites integration with localStorage', () => {
  const KEY = 'gdx_sidebar_favorites';

  it('first load seeds defaults into localStorage; second load reads them back', () => {
    localStorage.removeItem(KEY);

    // First load — empty storage
    let raw = localStorage.getItem(KEY);
    let { favorites, shouldPersist } = resolveInitialFavorites(raw);
    if (shouldPersist) localStorage.setItem(KEY, JSON.stringify(favorites));

    expect(favorites.map((f) => f.to)).toContain('/onboarding');
    expect(JSON.parse(localStorage.getItem(KEY))).toEqual(DEFAULT_FAVORITES);

    // Second load — storage now has the seeded value
    raw = localStorage.getItem(KEY);
    ({ favorites, shouldPersist } = resolveInitialFavorites(raw));
    expect(shouldPersist).toBe(false);
    expect(favorites).toEqual(DEFAULT_FAVORITES);
  });

  it('explicit empty list survives reload (does NOT re-seed)', () => {
    localStorage.setItem(KEY, '[]');
    const { favorites, shouldPersist } = resolveInitialFavorites(localStorage.getItem(KEY));
    expect(favorites).toEqual([]);
    expect(shouldPersist).toBe(false);
    // Storage is unchanged
    expect(localStorage.getItem(KEY)).toBe('[]');
  });
});

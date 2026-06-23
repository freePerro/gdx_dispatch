import { describe, it, expect, beforeEach, vi } from 'vitest';
import { getIdleTimeoutMin, setIdleTimeoutMin } from '../useIdleLogout';

describe('useIdleLogout storage', () => {
  beforeEach(() => localStorage.clear());

  it('returns 0 when unset, blank, non-numeric, or non-positive', () => {
    expect(getIdleTimeoutMin()).toBe(0);
    localStorage.setItem('gdx_idle_timeout_min', '');
    expect(getIdleTimeoutMin()).toBe(0);
    localStorage.setItem('gdx_idle_timeout_min', 'abc');
    expect(getIdleTimeoutMin()).toBe(0);
    localStorage.setItem('gdx_idle_timeout_min', '-5');
    expect(getIdleTimeoutMin()).toBe(0);
  });

  it('round-trips a positive value, floored and never negative', () => {
    setIdleTimeoutMin(15);
    expect(getIdleTimeoutMin()).toBe(15);
    setIdleTimeoutMin(15.9);
    expect(getIdleTimeoutMin()).toBe(15);
    setIdleTimeoutMin(-3);
    expect(getIdleTimeoutMin()).toBe(0);
  });

  it('dispatches the config-changed event so a mounted watcher re-arms', () => {
    const spy = vi.fn();
    window.addEventListener('gdx:idle-config-changed', spy);
    setIdleTimeoutMin(10);
    expect(spy).toHaveBeenCalledOnce();
    window.removeEventListener('gdx:idle-config-changed', spy);
  });
});

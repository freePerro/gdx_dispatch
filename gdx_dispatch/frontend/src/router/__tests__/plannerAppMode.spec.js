/**
 * In the GDX Planner Android app (lib/appMode.js) the router shows the
 * planner page and nothing of the dispatch app.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createAppRouter } from '../index';
import { useAuthStore } from '../../stores/auth';
import { _setAppMode } from '../../lib/appMode';

async function navigate(path) {
  const router = createAppRouter();
  await router.push(path);
  return router.currentRoute.value;
}

describe('GDX Planner app routes', () => {
  let auth;
  beforeEach(() => {
    sessionStorage.clear();
    setActivePinia(createPinia());
    auth = useAuthStore();
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ permissions: [] }) })));
    _setAppMode('planner');
  });
  afterEach(() => {
    _setAppMode('dispatch');
    vi.unstubAllGlobals();
  });

  it('sends a signed-in office user from the dashboard to the planner page', async () => {
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Office', role: 'dispatcher' };
    expect((await navigate('/dashboard')).path).toBe('/planner-app');
  });

  it('lands an already signed-in /login on the planner page, not the dashboard', async () => {
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Office', role: 'dispatcher' };
    expect((await navigate('/login')).path).toBe('/planner-app');
  });

  it('a tapped reminder (/mobile/planner?tab=tasks) opens the Tasks tab of the planner page', async () => {
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Office', role: 'dispatcher' };
    const current = await navigate('/mobile/planner?tab=tasks');
    expect(current.path).toBe('/planner-app');
    expect(current.query.tab).toBe('tasks');
  });

  it('a technician is not bounced to /mobile: the planner page tells them it is for office accounts', async () => {
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Tech', role: 'technician' };
    expect((await navigate('/planner')).path).toBe('/planner-app');
  });

  it('signed out, the planner page asks for a sign-in first', async () => {
    const current = await navigate('/planner-app');
    expect(current.path).toBe('/login');
  });

  it('keeps the forced password change on /profile', async () => {
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Office', role: 'dispatcher' };
    auth.mustChangePassword = true;
    expect((await navigate('/planner-app')).path).toBe('/profile');
  });

  it('the dispatch app is unchanged: an office user stays on the dashboard', async () => {
    _setAppMode('dispatch');
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Office', role: 'dispatcher' };
    expect((await navigate('/dashboard')).path).toBe('/dashboard');
  });
});

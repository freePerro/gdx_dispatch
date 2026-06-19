import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createAppRouter } from '../src/router';
import { useAuthStore } from '../src/stores/auth';

describe('router guards', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('redirects to /login when unauthenticated', async () => {
    const router = createAppRouter();

    await router.push('/dashboard');
    await router.isReady();

    expect(router.currentRoute.value.path).toBe('/login');
  });

  it('allows private route when authenticated', async () => {
    const auth = useAuthStore();
    auth.accessToken = 'valid';
    const router = createAppRouter();

    await router.push('/dashboard');
    await router.isReady();

    expect(router.currentRoute.value.path).toBe('/dashboard');
  });
});

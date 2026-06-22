import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { mount, flushPromises } from '@vue/test-utils';
import LoginView from '../src/views/LoginView.vue';

const pushMock = vi.fn();
const routeQuery = { value: {} };

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: pushMock }),
  useRoute: () => ({ query: routeQuery.value }),
}));

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

describe('LoginView', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    pushMock.mockClear();
    sessionStorage.clear();
    routeQuery.value = {};
  });

  it('renders email and password fields', () => {
    const wrapper = mount(LoginView);
    expect(wrapper.find('input[type="email"]').exists()).toBe(true);
    expect(wrapper.find('input[type="password"]').exists()).toBe(true);
    expect(wrapper.find('button[type="submit"]').exists()).toBe(true);
  });

  it('successful login stores token and redirects', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn()
        .mockResolvedValueOnce(jsonResponse(200, { access_token: 'tok-123', user: { name: 'Admin' } }))
        .mockResolvedValueOnce(jsonResponse(200, { company_name: 'Test Co' })),
    );

    const wrapper = mount(LoginView);
    await wrapper.find('input[type="email"]').setValue('admin@test.com');
    await wrapper.find('input[type="password"]').setValue('password123');
    await wrapper.find('form').trigger('submit');
    await flushPromises();

    expect(pushMock).toHaveBeenCalledWith('/dashboard');
    expect(sessionStorage.getItem('gdx_access_token')).toBe('tok-123');
  });

  it('failed login shows error message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'bad creds' })));

    const wrapper = mount(LoginView);
    await wrapper.find('input[type="email"]').setValue('bad@test.com');
    await wrapper.find('input[type="password"]').setValue('wrong');
    await wrapper.find('form').trigger('submit');
    await flushPromises();

    expect(wrapper.find('.error-message').text()).toContain('bad creds');
    expect(pushMock).not.toHaveBeenCalled();
  });

  it('disables submit button while submitting', async () => {
    let resolveLogin;
    const loginPromise = new Promise((resolve) => { resolveLogin = resolve; });
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(loginPromise));

    const wrapper = mount(LoginView);
    await wrapper.find('input[type="email"]').setValue('admin@test.com');
    await wrapper.find('input[type="password"]').setValue('pw');
    await wrapper.find('form').trigger('submit');
    await flushPromises();

    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined();

    resolveLogin(jsonResponse(200, { access_token: 'tok' }));
    await flushPromises();
  });

  it('redirects to query redirect param on success', async () => {
    routeQuery.value = { redirect: '/jobs' };

    vi.stubGlobal(
      'fetch',
      vi.fn()
        .mockResolvedValueOnce(jsonResponse(200, { access_token: 'tok-456' }))
        .mockResolvedValueOnce(jsonResponse(200, {})),
    );

    const wrapper = mount(LoginView);
    await wrapper.find('input[type="email"]').setValue('admin@test.com');
    await wrapper.find('input[type="password"]').setValue('pw');
    await wrapper.find('form').trigger('submit');
    await flushPromises();

    expect(pushMock).toHaveBeenCalledWith('/jobs');
  });
});

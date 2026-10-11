/**
 * GDXA-579 — onboarding step 2 used to POST {name, email, phone} straight to
 * /api/technicians, which answers 400 "user_id is required" for any payload
 * without one, so entering a technician stalled the wizard. Step 2 now creates
 * the technician's login with POST /api/users and hands its id to
 * /api/technicians. The backend half of the contract (these keys are fields
 * the endpoints accept, and the payload really gets a 201) is
 * gdx_dispatch/tests/test_onboarding_wizard_technician_contract.py.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const post = vi.fn();
const patch = vi.fn(async () => ({}));
const get = vi.fn(async () => []);

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ post, patch, get }),
}));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));

import OnboardingView from '../OnboardingView.vue';

async function toStep2() {
  const w = mount(OnboardingView);
  await w.find('.btn-primary').trigger('click');
  await flushPromises();
  expect(w.find('h2').text()).toBe('Add your first technician');
  return w;
}

async function fillTech(w, { name = 'Pat Tech', email = 'pat@example.com', phone = '555-0100', password = 'longenough1' } = {}) {
  const inputs = w.findAll('input');
  await inputs[0].setValue(name);
  await inputs[1].setValue(email);
  await inputs[2].setValue(phone);
  if (inputs[3]) await inputs[3].setValue(password);
}

const next = (w) => w.findAll('.btn-primary').at(-1);

describe('OnboardingView step 2 — add a technician', () => {
  beforeEach(() => {
    post.mockReset();
    patch.mockClear();
    get.mockReset();
    get.mockImplementation(async () => []);
  });

  it('creates the login first and posts the technician with its user_id', async () => {
    post.mockImplementation(async (url) => (url === '/api/users' ? { id: 'u-42' } : { id: 't-1' }));
    const w = await toStep2();
    await fillTech(w);
    await next(w).trigger('click');
    await flushPromises();

    expect(post.mock.calls.map((c) => c[0])).toEqual(['/api/users', '/api/technicians']);
    expect(post.mock.calls[0][1]).toEqual({
      name: 'Pat Tech', email: 'pat@example.com', password: 'longenough1', role: 'technician',
    });
    expect(post.mock.calls[1][1]).toEqual({
      user_id: 'u-42', name: 'Pat Tech', email: 'pat@example.com', phone: '555-0100',
    });
    expect(w.find('h2').text()).toBe('Import customers');
  });

  it('a retry after the technician POST fails does not create the login twice', async () => {
    let techCalls = 0;
    post.mockImplementation(async (url) => {
      if (url === '/api/users') return { id: 'u-42' };
      techCalls += 1;
      if (techCalls === 1) throw new Error('boom');
      return { id: 't-1' };
    });
    const w = await toStep2();
    await fillTech(w);
    await next(w).trigger('click');
    await flushPromises();
    expect(w.find('.error').text()).toBe('boom');
    expect(w.find('input[type="password"]').exists()).toBe(false);

    await next(w).trigger('click');
    await flushPromises();
    expect(post.mock.calls.filter((c) => c[0] === '/api/users')).toHaveLength(1);
    expect(post.mock.calls.at(-1)[1].user_id).toBe('u-42');
    expect(w.find('h2').text()).toBe('Import customers');
  });

  it('refuses a short password before calling anything', async () => {
    const w = await toStep2();
    await fillTech(w, { password: 'short' });
    await next(w).trigger('click');
    await flushPromises();
    expect(post).not.toHaveBeenCalled();
    expect(w.find('.error').text()).toMatch(/at least 8/);
    expect(w.find('h2').text()).toBe('Add your first technician');
  });

  it('refuses a missing email before calling anything', async () => {
    const w = await toStep2();
    await fillTech(w, { email: '' });
    await next(w).trigger('click');
    await flushPromises();
    expect(post).not.toHaveBeenCalled();
    expect(w.find('.error').text()).toMatch(/Email is required/);
  });

  it('Next with no name still skips the step without posting', async () => {
    const w = await toStep2();
    await next(w).trigger('click');
    await flushPromises();
    expect(post).not.toHaveBeenCalled();
    expect(w.find('h2').text()).toBe('Import customers');
  });
  it('Back then Next updates the technician it made instead of adding a second', async () => {
    post.mockImplementation(async (url) => (url === '/api/users' ? { id: 'u-42' } : { id: 't-1' }));
    const w = await toStep2();
    await fillTech(w);
    await next(w).trigger('click');
    await flushPromises();
    await w.find('.btn-back').trigger('click');
    await w.findAll('input')[2].setValue('555-0199');
    await next(w).trigger('click');
    await flushPromises();

    expect(post.mock.calls.map((c) => c[0])).toEqual(['/api/users', '/api/technicians']);
    const techPatches = patch.mock.calls.filter((c) => c[0].startsWith('/api/technicians/'));
    expect(techPatches).toEqual([
      ['/api/technicians/t-1', { name: 'Pat Tech', email: 'pat@example.com', phone: '555-0199' }],
    ]);
    expect(w.find('h2').text()).toBe('Import customers');
  });

  it('an email that already has a login (the owner as tech) attaches to that user', async () => {
    post.mockImplementation(async (url) => {
      if (url === '/api/users') throw Object.assign(new Error('A user with this email already exists'), { status: 409 });
      return { id: 't-1' };
    });
    get.mockImplementation(async () => [{ id: 'u-owner', email: 'Owner@Example.com' }]);
    const w = await toStep2();
    await fillTech(w, { email: 'owner@example.com' });
    await next(w).trigger('click');
    await flushPromises();

    expect(post.mock.calls.at(-1)).toEqual(['/api/technicians', {
      user_id: 'u-owner', name: 'Pat Tech', email: 'owner@example.com', phone: '555-0100',
    }]);
    expect(w.find('h2').text()).toBe('Import customers');
  });

  it('a blank password with an existing login attaches without creating a user', async () => {
    post.mockImplementation(async () => ({ id: 't-1' }));
    get.mockImplementation(async () => [{ id: 'u-owner', email: 'owner@example.com' }]);
    const w = await toStep2();
    await fillTech(w, { email: 'owner@example.com', password: '' });
    await next(w).trigger('click');
    await flushPromises();

    expect(post.mock.calls.map((c) => c[0])).toEqual(['/api/technicians']);
    expect(post.mock.calls[0][1].user_id).toBe('u-owner');
  });

  it('a blank password with a new email asks for one and posts nothing', async () => {
    const w = await toStep2();
    await fillTech(w, { email: 'new@example.com', password: '' });
    await next(w).trigger('click');
    await flushPromises();
    expect(post).not.toHaveBeenCalled();
    expect(w.find('.error').text()).toMatch(/at least 8/);
  });
  it('a different email on a return trip creates a new login and technician', async () => {
    let n = 0;
    post.mockImplementation(async (url) => {
      n += 1;
      return url === '/api/users' ? { id: `u-${n}` } : { id: `t-${n}` };
    });
    const w = await toStep2();
    await fillTech(w);
    await next(w).trigger('click');
    await flushPromises();
    await w.find('.btn-back').trigger('click');
    await w.findAll('input')[1].setValue('someone.else@example.com');
    expect(w.find('input[type="password"]').exists()).toBe(true);
    await w.find('input[type="password"]').setValue('anotherpass1');
    await next(w).trigger('click');
    await flushPromises();

    expect(post.mock.calls.map((c) => c[0])).toEqual(['/api/users', '/api/technicians', '/api/users', '/api/technicians']);
    expect(post.mock.calls[3][1].user_id).toBe('u-3');
    expect(patch.mock.calls.filter((c) => c[0].startsWith('/api/technicians/'))).toEqual([]);
  });

  it('an existing login that already has a technician row is left alone, not duplicated', async () => {
    post.mockImplementation(async (url) => {
      if (url === '/api/users') throw Object.assign(new Error('exists'), { status: 409 });
      return { id: 't-new' };
    });
    get.mockImplementation(async (url) => (url.startsWith('/api/users')
      ? [{ id: 'u-owner', email: 'owner@example.com' }]
      : [{ id: 't-old', user_id: 'u-owner', name: 'Owner' }]));
    const w = await toStep2();
    await fillTech(w, { email: 'owner@example.com' });
    await next(w).trigger('click');
    await flushPromises();

    expect(post.mock.calls.map((c) => c[0])).toEqual(['/api/users']);
    expect(patch.mock.calls.filter((c) => c[0].startsWith('/api/technicians/'))).toEqual([]);
    expect(w.find('h2').text()).toBe('Import customers');

    // Back, fix the name, Next: still no write to a row the wizard did not make.
    await w.find('.btn-back').trigger('click');
    await w.findAll('input')[0].setValue('Pat T.');
    await next(w).trigger('click');
    await flushPromises();
    expect(post.mock.calls.map((c) => c[0])).toEqual(['/api/users']);
    expect(patch.mock.calls.filter((c) => c[0].startsWith('/api/technicians/'))).toEqual([]);
    expect(w.find('h2').text()).toBe('Import customers');
  });

  it('a failed existing-row lookup is retried, not skipped into a twin POST', async () => {
    post.mockImplementation(async (url) => {
      if (url === '/api/users') throw Object.assign(new Error('exists'), { status: 409 });
      return { id: 't-new' };
    });
    let techGets = 0;
    get.mockImplementation(async (url) => {
      if (url.startsWith('/api/users')) return [{ id: 'u-owner', email: 'owner@example.com' }];
      techGets += 1;
      if (techGets === 1) throw new Error('network blip');
      return [{ id: 't-old', user_id: 'u-owner', name: 'Owner' }];
    });
    const w = await toStep2();
    await fillTech(w, { email: 'owner@example.com' });
    await next(w).trigger('click');
    await flushPromises();
    expect(w.find('.error').text()).toBe('network blip');

    await next(w).trigger('click');
    await flushPromises();
    expect(techGets).toBe(2);
    expect(post.mock.calls.filter((c) => c[0] === '/api/technicians')).toEqual([]);
    expect(w.find('h2').text()).toBe('Import customers');
  });

  it('a retry after a lost technician response finds the committed row, not a twin', async () => {
    post.mockImplementation(async (url) => {
      if (url === '/api/users') return { id: 'u-42' };
      throw new Error('timeout'); // the row committed; the response never came
    });
    const w = await toStep2();
    await fillTech(w);
    await next(w).trigger('click');
    await flushPromises();
    expect(w.find('.error').text()).toBe('timeout');

    get.mockImplementation(async (url) => (url.startsWith('/api/technicians')
      ? [{ id: 't-1', user_id: 'u-42', name: 'Technician' }] : []));
    await next(w).trigger('click');
    await flushPromises();
    expect(get.mock.calls.at(-1)[0]).toBe('/api/technicians?include_verification=true');
    expect(post.mock.calls.filter((c) => c[0] === '/api/technicians')).toHaveLength(1);
    expect(w.find('h2').text()).toBe('Import customers');

    // That row is the wizard's own (its login was made here): a later edit lands.
    await w.find('.btn-back').trigger('click');
    await w.findAll('input')[2].setValue('555-0199');
    await next(w).trigger('click');
    await flushPromises();
    expect(patch.mock.calls.filter((c) => c[0].startsWith('/api/technicians/')).at(-1)).toEqual(
      ['/api/technicians/t-1', { name: 'Pat Tech', email: 'pat@example.com', phone: '555-0199' }],
    );
  });

  it('a 409 whose user cannot be found says so, not "password too short"', async () => {
    post.mockImplementation(async () => { throw Object.assign(new Error('exists'), { status: 409 }); });
    const w = await toStep2();
    await fillTech(w, { email: 'owner@example.com', password: 'validpass99' });
    await next(w).trigger('click');
    await flushPromises();
    expect(w.find('.error').text()).toMatch(/already has a login/);
    expect(post.mock.calls.filter((c) => c[0] === '/api/technicians')).toEqual([]);
  });
});

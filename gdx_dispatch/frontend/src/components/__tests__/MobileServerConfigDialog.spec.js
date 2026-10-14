import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import MobileServerConfigDialog from '../MobileServerConfigDialog.vue';
import * as serverConfig from '../../lib/serverConfig';

describe('MobileServerConfigDialog', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it('does not render dialog content when visible is false', () => {
    const wrapper = mount(MobileServerConfigDialog, {
      props: { visible: false },
    });
    expect(wrapper.find('.server-config-dialog').exists()).toBe(false);
  });

  it('renders input with current server URL when visible', async () => {
    await serverConfig.setServerUrl('https://mycompany.com');
    const wrapper = mount(MobileServerConfigDialog, {
      props: { visible: true },
    });
    const input = wrapper.find('[data-testid="server-url-input"]');
    expect(input.exists()).toBe(true);
    expect(input.element.value).toBe('https://mycompany.com');
  });

  it('selecting a preset updates the input value', async () => {
    const wrapper = mount(MobileServerConfigDialog, {
      props: { visible: true },
    });
    const demoChip = wrapper.findAll('.preset-chip').find((c) => c.text().includes('Public Demo'));
    expect(demoChip).toBeDefined();
    await demoChip.trigger('click');

    const input = wrapper.find('[data-testid="server-url-input"]');
    expect(input.element.value).toBe('https://gdxdispatch.com');
  });

  it('clicking Save calls setServerUrl and emits saved and close', async () => {
    const setSpy = vi.spyOn(serverConfig, 'setServerUrl');
    const wrapper = mount(MobileServerConfigDialog, {
      props: { visible: true },
    });
    const input = wrapper.find('[data-testid="server-url-input"]');
    await input.setValue('dispatch.mywork.com');

    const saveBtn = wrapper.find('[data-testid="server-save-btn"]');
    await saveBtn.trigger('click');
    await flushPromises();

    expect(setSpy).toHaveBeenCalledWith('dispatch.mywork.com');
    expect(wrapper.emitted('saved')).toBeTruthy();
    expect(wrapper.emitted('saved')[0]).toEqual(['https://dispatch.mywork.com']);
    expect(wrapper.emitted('update:visible')[0]).toEqual([false]);
  });

  it('test connection shows success feedback when server responds ok', async () => {
    vi.spyOn(serverConfig, 'testServerConnection').mockResolvedValue({
      ok: true,
      message: 'Connected successfully',
      status: 'ok',
    });

    const wrapper = mount(MobileServerConfigDialog, {
      props: { visible: true },
    });
    const input = wrapper.find('[data-testid="server-url-input"]');
    await input.setValue('https://good.example.com');

    const testBtn = wrapper.findAll('.btn-secondary').find((b) => b.text().includes('Test Connection'));
    await testBtn.trigger('click');
    await flushPromises();

    expect(wrapper.find('.test-feedback.is-ok').exists()).toBe(true);
    expect(wrapper.find('.test-feedback').text()).toContain('Connected successfully');
  });
});

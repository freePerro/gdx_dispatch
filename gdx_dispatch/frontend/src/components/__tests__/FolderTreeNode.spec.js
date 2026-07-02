/**
 * FolderTreeNode — recursive folder tree with chevron expand/collapse.
 * Covers: render at depth, children visibility toggling, select/delete emits.
 */
import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

import FolderTreeNode from '../FolderTreeNode.vue';


const globalConfig = { plugins: [PrimeVue] };


function makeNode(name, children = [], id = name) {
  return { id, name, parent_id: null, description: null, children };
}


describe('FolderTreeNode', () => {
  it('renders a leaf node with no toggle button', () => {
    const node = makeNode('Leaf', []);
    const wrapper = mount(FolderTreeNode, { props: { node }, global: globalConfig });
    expect(wrapper.text()).toContain('Leaf');
    expect(wrapper.find('.folder-toggle').exists()).toBe(false);
    expect(wrapper.find('.folder-toggle-spacer').exists()).toBe(true);
  });

  it('renders children when expanded and hides them when collapsed', async () => {
    const tree = makeNode('Top', [
      makeNode('Sub', [makeNode('Inner')]),
    ]);
    const wrapper = mount(FolderTreeNode, { props: { node: tree }, global: globalConfig });
    // Default: depth 0 expanded, depth 1 collapsed.
    expect(wrapper.text()).toContain('Top');
    expect(wrapper.text()).toContain('Sub');
    expect(wrapper.text()).not.toContain('Inner');

    // Click the chevron on the Sub child.
    const subToggle = wrapper.findAll('.folder-toggle')[1];
    await subToggle.trigger('click');
    expect(wrapper.text()).toContain('Inner');

    // Collapse again.
    await subToggle.trigger('click');
    expect(wrapper.text()).not.toContain('Inner');
  });

  it('emits select with the clicked node', async () => {
    const node = makeNode('Pick');
    const wrapper = mount(FolderTreeNode, { props: { node }, global: globalConfig });
    await wrapper.find('.folder-item').trigger('click');
    expect(wrapper.emitted('select')).toBeTruthy();
    expect(wrapper.emitted('select')[0][0].name).toBe('Pick');
  });

  it('emits delete only when canDelete is true', async () => {
    const node = makeNode('Killable');
    const wrapper = mount(FolderTreeNode, {
      props: { node, canDelete: true },
      global: globalConfig,
    });
    await wrapper.find('.folder-delete-btn').trigger('click');
    expect(wrapper.emitted('delete')).toBeTruthy();
    expect(wrapper.emitted('delete')[0][0].name).toBe('Killable');
  });

  it('hides delete button when canDelete is false', () => {
    const node = makeNode('Safe');
    const wrapper = mount(FolderTreeNode, {
      props: { node, canDelete: false },
      global: globalConfig,
    });
    expect(wrapper.find('.folder-delete-btn').exists()).toBe(false);
  });

  it('indents children by depth', () => {
    const node = makeNode('Deep');
    const wrapper = mount(FolderTreeNode, {
      props: { node, depth: 3 },
      global: globalConfig,
    });
    const item = wrapper.find('.folder-item');
    // depth 3 → padding-left = 0.4 + 3*0.9 = 3.1rem
    expect(item.attributes('style')).toContain('padding-left: 3.1rem');
  });

  it('renders a doc_count badge when > 0', () => {
    const node = { ...makeNode('Has Files'), doc_count: 7 };
    const wrapper = mount(FolderTreeNode, { props: { node }, global: globalConfig });
    const badge = wrapper.find('.folder-count');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toBe('7');
  });

  it('hides the doc_count badge when zero', () => {
    const node = { ...makeNode('Empty'), doc_count: 0 };
    const wrapper = mount(FolderTreeNode, { props: { node }, global: globalConfig });
    expect(wrapper.find('.folder-count').exists()).toBe(false);
  });

  it('marks the active node when selectedId matches', () => {
    const node = makeNode('Hi', [], 'abc-123');
    const wrapper = mount(FolderTreeNode, {
      props: { node, selectedId: 'abc-123' },
      global: globalConfig,
    });
    expect(wrapper.find('.folder-item.active').exists()).toBe(true);
  });

  it('emits move with folderId + newParentId on drop', async () => {
    const node = makeNode('Drop Target', [], 'target-1');
    const wrapper = mount(FolderTreeNode, {
      props: { node, canMove: true },
      global: globalConfig,
    });
    const item = wrapper.find('.folder-item');
    const dataMap = { 'application/x-gdx-folder-id': 'dragged-99' };
    const dataTransfer = {
      types: Object.keys(dataMap),
      getData: (t) => dataMap[t] || '',
      setData: () => {},
      effectAllowed: '',
      dropEffect: '',
    };
    await item.trigger('drop', { dataTransfer });
    const events = wrapper.emitted('move');
    expect(events).toBeTruthy();
    expect(events[0][0]).toEqual({ folderId: 'dragged-99', newParentId: 'target-1' });
  });

  it('does not emit move when dropping a folder onto itself', async () => {
    const node = makeNode('Self', [], 'same-1');
    const wrapper = mount(FolderTreeNode, {
      props: { node, canMove: true },
      global: globalConfig,
    });
    const dataMap = { 'application/x-gdx-folder-id': 'same-1' };
    const dataTransfer = {
      types: Object.keys(dataMap),
      getData: (t) => dataMap[t] || '',
      setData: () => {},
      effectAllowed: '',
      dropEffect: '',
    };
    await wrapper.find('.folder-item').trigger('drop', { dataTransfer });
    expect(wrapper.emitted('move')).toBeFalsy();
  });

  it('does not emit move when drag mime type is missing', async () => {
    const node = makeNode('Target', [], 'target-1');
    const wrapper = mount(FolderTreeNode, {
      props: { node, canMove: true },
      global: globalConfig,
    });
    const dataTransfer = {
      types: [],
      getData: () => '',
      setData: () => {},
      effectAllowed: '',
      dropEffect: '',
    };
    await wrapper.find('.folder-item').trigger('drop', { dataTransfer });
    expect(wrapper.emitted('move')).toBeFalsy();
  });

  it('exposes the row as a focusable treeitem with aria-expanded on folders', () => {
    const tree = makeNode('Top', [makeNode('Sub')]);
    const wrapper = mount(FolderTreeNode, { props: { node: tree }, global: globalConfig });
    const items = wrapper.findAll('.folder-item');
    // Folder row: treeitem, tabbable, expanded by default at depth 0.
    expect(items[0].attributes('role')).toBe('treeitem');
    expect(items[0].attributes('tabindex')).toBe('0');
    expect(items[0].attributes('aria-expanded')).toBe('true');
    // Leaf row: no aria-expanded at all.
    expect(items[1].attributes('aria-expanded')).toBeUndefined();
  });

  it('emits select on Enter and Space keydown like click', async () => {
    const node = makeNode('KeyPick');
    const wrapper = mount(FolderTreeNode, { props: { node }, global: globalConfig });
    const item = wrapper.find('.folder-item');
    await item.trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('select')).toHaveLength(1);
    expect(wrapper.emitted('select')[0][0].name).toBe('KeyPick');
    await item.trigger('keydown', { key: ' ' });
    expect(wrapper.emitted('select')).toHaveLength(2);
    // Same payload as the click path.
    await item.trigger('click');
    expect(wrapper.emitted('select')[2][0]).toBe(wrapper.emitted('select')[0][0]);
  });

  it('ArrowLeft collapses and ArrowRight expands a folder row', async () => {
    const tree = makeNode('Top', [makeNode('Sub')]);
    const wrapper = mount(FolderTreeNode, { props: { node: tree }, global: globalConfig });
    const item = wrapper.find('.folder-item');
    // Depth 0 starts expanded.
    expect(wrapper.text()).toContain('Sub');
    await item.trigger('keydown', { key: 'ArrowLeft' });
    expect(wrapper.text()).not.toContain('Sub');
    expect(item.attributes('aria-expanded')).toBe('false');
    await item.trigger('keydown', { key: 'ArrowRight' });
    expect(wrapper.text()).toContain('Sub');
    expect(item.attributes('aria-expanded')).toBe('true');
    // Arrow keys never emit select.
    expect(wrapper.emitted('select')).toBeFalsy();
  });

  it('ArrowRight on a leaf row does nothing', async () => {
    const node = makeNode('Leafy');
    const wrapper = mount(FolderTreeNode, { props: { node }, global: globalConfig });
    const item = wrapper.find('.folder-item');
    await item.trigger('keydown', { key: 'ArrowRight' });
    expect(item.attributes('aria-expanded')).toBeUndefined();
    expect(wrapper.emitted('select')).toBeFalsy();
  });

  it('propagates select events from nested children', async () => {
    const tree = makeNode('Top', [makeNode('Child', [], 'child-1')]);
    const wrapper = mount(FolderTreeNode, { props: { node: tree }, global: globalConfig });
    // Top is expanded by default; click the child folder-item.
    const items = wrapper.findAll('.folder-item');
    await items[1].trigger('click');
    const events = wrapper.emitted('select');
    expect(events).toBeTruthy();
    expect(events.at(-1)[0].id).toBe('child-1');
  });
});

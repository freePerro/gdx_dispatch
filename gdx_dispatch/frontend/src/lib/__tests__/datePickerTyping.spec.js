// GDXA-423: a half-typed date in a typeable DatePicker must not be re-formatted
// under the user's caret. These mount the real PrimeVue component with the
// patch tests/setup.js installs, exactly as main.js installs it in the app.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { defineComponent, h, ref } from 'vue';
import DatePicker from 'primevue/datepicker';
import Calendar from 'primevue/calendar';
import { isDatePickerTypingInstalled } from '../datePickerTyping';

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

function mountPicker(props = {}, initial = null, component = DatePicker) {
  const model = ref(initial);
  const Host = defineComponent({
    setup: () => () =>
      h(component, {
        modelValue: model.value,
        'onUpdate:modelValue': (v) => { model.value = v; },
        dateFormat: 'yy-mm-dd',
        ...props,
      }),
  });
  const w = mount(Host, { attachTo: document.body });
  return { w, model, input: w.find('input').element };
}

// One key at a time, inserted at the caret the component left, the way a
// browser does — so a re-format between keys moves where the next key lands.
async function typeKeys(input, keys) {
  const seen = [];
  // Let the focus re-render (overlay opens) land first, as it does before a
  // person's first key.
  input.focus();
  await flushPromises();
  for (const ch of keys) {
    const at = input.selectionStart ?? input.value.length;
    input.value = input.value.slice(0, at) + ch + input.value.slice(input.selectionEnd ?? at);
    input.setSelectionRange(at + 1, at + 1);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await flushPromises();
    seen.push(input.value);
  }
  return seen;
}

const ymd = (d) => d && `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;

describe('DatePicker typing (GDXA-423)', () => {
  it('is installed on the shared component', () => {
    expect(isDatePickerTypingInstalled(DatePicker)).toBe(true);
  });

  it('typing 2026-07-31 key by key stores the 31st, and no key is re-formatted', async () => {
    const { w, model, input } = mountPicker();
    const seen = await typeKeys(input, '2026-07-31');
    expect(seen).toEqual(['2', '20', '202', '2026', '2026-', '2026-0', '2026-07', '2026-07-', '2026-07-3', '2026-07-31']);
    expect(ymd(model.value)).toBe('2026-7-31');
    w.unmount();
  });

  it('a half-typed day is not written while typing, and is committed on blur', async () => {
    const { w, model, input } = mountPicker({}, new Date(2026, 0, 15));
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await flushPromises();
    expect(model.value).toBeNull();
    await typeKeys(input, '2026-07-3');
    expect(model.value).toBeNull();
    expect(input.value).toBe('2026-07-3');
    input.dispatchEvent(new FocusEvent('blur'));
    await flushPromises();
    expect(ymd(model.value)).toBe('2026-7-3');
    expect(input.value).toBe('2026-07-03');
    w.unmount();
  });

  it('Enter commits a half-typed date before a form could submit', async () => {
    const { w, model, input } = mountPicker();
    await typeKeys(input, '2026-07-3');
    input.dispatchEvent(new KeyboardEvent('keydown', { code: 'Enter', key: 'Enter', bubbles: true }));
    expect(ymd(model.value)).toBe('2026-7-3');
    w.unmount();
  });

  // Edits an existing 2026-07-31 to "2026-07-5" (two backspaces, then "5").
  async function editDayTo5(input) {
    input.focus();
    await flushPromises();
    for (const text of ['2026-07-3', '2026-07-', '2026-07-5']) {
      input.value = text;
      input.setSelectionRange(text.length, text.length);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await flushPromises();
    }
  }

  it('Escape between typing and blur keeps the edit (audit: silent loss)', async () => {
    const { w, model, input } = mountPicker({}, new Date(2026, 6, 31));
    await editDayTo5(input);
    input.dispatchEvent(new KeyboardEvent('keydown', { code: 'Escape', key: 'Escape', bubbles: true }));
    await flushPromises();
    expect(input.value).toBe('2026-07-5');
    input.dispatchEvent(new FocusEvent('blur'));
    await flushPromises();
    expect(ymd(model.value)).toBe('2026-7-5');
    expect(input.value).toBe('2026-07-05');
    w.unmount();
  });

  it('a prop change re-rendering the picker keeps the typed text', async () => {
    const model = ref(new Date(2026, 6, 31));
    const invalid = ref(false);
    const Host = defineComponent({
      setup: () => () =>
        h(DatePicker, {
          modelValue: model.value,
          'onUpdate:modelValue': (v) => { model.value = v; },
          dateFormat: 'yy-mm-dd',
          invalid: invalid.value,
        }),
    });
    const w = mount(Host, { attachTo: document.body });
    const input = w.find('input').element;
    await editDayTo5(input);
    invalid.value = true;
    await flushPromises();
    expect(input.value).toBe('2026-07-5');
    input.dispatchEvent(new FocusEvent('blur'));
    await flushPromises();
    expect(ymd(model.value)).toBe('2026-7-5');
    w.unmount();
  });

  it('the parent setting the model drops the typed text', async () => {
    const { w, model, input } = mountPicker({}, new Date(2026, 6, 31));
    await editDayTo5(input);
    model.value = new Date(2026, 8, 1);
    await flushPromises();
    expect(input.value).toBe('2026-09-01');
    input.dispatchEvent(new FocusEvent('blur'));
    await flushPromises();
    expect(ymd(model.value)).toBe('2026-9-1');
    w.unmount();
  });

  it('NumpadEnter commits like Enter', async () => {
    const { w, model, input } = mountPicker();
    await typeKeys(input, '2026-07-3');
    input.dispatchEvent(new KeyboardEvent('keydown', { code: 'NumpadEnter', key: 'Enter', bubbles: true }));
    expect(ymd(model.value)).toBe('2026-7-3');
    w.unmount();
  });

  it('text that never parses leaves the model alone and blur restores it', async () => {
    const { w, model, input } = mountPicker({}, new Date(2026, 6, 31));
    await flushPromises();
    input.value = '2026-07-x';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new FocusEvent('blur'));
    await flushPromises();
    expect(ymd(model.value)).toBe('2026-7-31');
    expect(input.value).toBe('2026-07-31');
    w.unmount();
  });

  it('yy-mm: typing 2026-12 stores December, not January', async () => {
    const { w, model, input } = mountPicker({ dateFormat: 'yy-mm', view: 'month' });
    const seen = await typeKeys(input, '2026-12');
    expect(seen.at(-1)).toBe('2026-12');
    expect(model.value.getMonth()).toBe(11);
    w.unmount();
  });

  it('mm/dd/yy and M d, yy still store a fully typed date', async () => {
    for (const [fmt, text, want] of [
      ['mm/dd/yy', '07/31/2026', '2026-7-31'],
      ['M d, yy', 'Jul 31, 2026', '2026-7-31'],
    ]) {
      const { w, model, input } = mountPicker({ dateFormat: fmt });
      await typeKeys(input, text);
      expect(ymd(model.value)).toBe(want);
      w.unmount();
    }
  });

  it('updateModelType="string" emits the formatted text', async () => {
    const { w, model, input } = mountPicker({ updateModelType: 'string' });
    await typeKeys(input, '2026-07-31');
    expect(model.value).toBe('2026-07-31');
    w.unmount();
  });

  it('the deprecated Calendar inherits the fix', async () => {
    const { w, model, input } = mountPicker({}, null, Calendar);
    await typeKeys(input, '2026-07-31');
    expect(ymd(model.value)).toBe('2026-7-31');
    w.unmount();
  });

  it('main.js installs the patch before the app is created', () => {
    const main = fs.readFileSync(path.join(SRC, 'main.js'), 'utf8');
    const install = main.indexOf('installDatePickerTyping();');
    expect(install).toBeGreaterThan(-1);
    expect(install).toBeLessThan(main.indexOf('createApp(App)'));
  });

  it('no file reaches the picker except through the patched component', () => {
    // The patch lives on the object 'primevue/datepicker' exports (Calendar and
    // the 'primevue' barrel re-export it). Any other path — the .vue source, a
    // copy, another date library's picker — would type unpatched.
    const allowed = new Set(['primevue/datepicker', 'primevue/calendar']);
    const offenders = [];
    const walk = (dir) => {
      for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, ent.name);
        if (ent.isDirectory()) walk(p);
        else if (/\.(vue|js|ts)$/.test(ent.name)) {
          const src = fs.readFileSync(p, 'utf8');
          for (const m of src.matchAll(/from\s+['"]([^'"]*(?:datepicker|calendar)[^'"]*)['"]/gi)) {
            const spec = m[1];
            if (allowed.has(spec) || spec.startsWith('.') || spec.startsWith('@/')) continue;
            offenders.push(`${path.relative(SRC, p)}: ${spec}`);
          }
        }
      }
    };
    walk(SRC);
    expect(offenders).toEqual([]);
  });
});

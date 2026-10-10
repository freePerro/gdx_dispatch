// Typed dates in PrimeVue's DatePicker commit only when the text is complete
// (GDXA-423).
//
// PrimeVue 4.5.5 (and 5.0.2, checked 2026-10-10) parses the input on every
// keystroke and writes the model as soon as the text parses. With a format that
// ends in a short field, a half-typed date already parses: under "yy-mm-dd",
// "2026-07-3" is July 3rd. The model changes, the input re-renders as
// "2026-07-03" with the caret before the "3", and the next key lands as
// "2026-07-013", which no longer parses. A fast typist stores the 3rd instead of
// the 31st. "yy-mm" has the same shape ("2026-1" becomes January).
//
// The fix patches the one component object every `import ... from
// 'primevue/datepicker'` (and the deprecated 'primevue/calendar', which
// extends it) shares, so no call site changes:
//   - while typing, the model is written only when the text is exactly what the
//     picker would display for the date it parses to ("2026-07-31", or "" to
//     clear). Anything shorter stays as typed, and nothing re-renders it.
//   - text typed but not yet committed is what the input shows on a re-render
//     (Escape closing the overlay, a prop change), instead of the old date
//     PrimeVue would write back; it is dropped when the model changes by any
//     other path (an overlay pick, the clear icon, the parent setting it).
//   - on blur and on Enter, whatever parses is committed, so "2026-07-3" left in
//     the box still means the 3rd and a form submitted with Enter sees it.
// Picking from the overlay is untouched.
//
// main.js installs this before the app mounts; tests/setup.js installs it for
// vitest. datePickerTyping.spec.js fails if either stops doing so, or if a
// file imports the picker from anywhere else.
import DatePicker from 'primevue/datepicker';

const PATCHED = Symbol.for('gdx.datePickerTyping');

function parseTyped(vm, text) {
  try {
    const value = vm.parseValue(text);
    return vm.isValidSelection(value) ? { value } : null;
  } catch {
    return null;
  }
}

function commitTyped(vm, value) {
  vm.updateModel(vm.updateModelType === 'string' ? vm.formatValue(value) : value);
  vm.updateCurrentMetaData();
}

// Commit whatever the box holds if it parses and differs from the model.
// Sets rawValue too, so PrimeVue's own onBlur re-formats the new date rather
// than the old one; the modelValue watcher sets the same value afterwards.
function commitPending(vm, text) {
  if (text == null || vm.inline) return;
  const parsed = parseTyped(vm, text);
  if (!parsed) return;
  if (vm.formatValue(parsed.value) === vm.formatValue(vm.rawValue)) return;
  vm.typeUpdate = false;
  vm.rawValue = parsed.value;
  commitTyped(vm, parsed.value);
}

export function installDatePickerTyping(component = DatePicker) {
  const methods = component.methods;
  if (!methods || methods[PATCHED]) return component;
  const { onBlur, onKeyDown, updateModel } = methods;
  const { data } = component;
  const { inputFieldValue } = component.computed;
  const modelWatch = component.watch.modelValue;
  const modelHandler = modelWatch.handler;
  const { updated } = component;

  // The uncommitted text, reactive so a re-render reads it (see the header).
  // It has to reach InputText through `defaultValue`: DatePicker does not
  // pass InputText a modelValue, so InputText's own value never follows typing
  // and its next render writes back whatever defaultValue last said.
  component.data = function patchedData() {
    return { ...data.call(this), gdxTypedText: null };
  };
  // PrimeVue's updated() schedules updateFocus() on every render with the
  // overlay open; two of those queued before the first runs make the second
  // focus a calendar cell, which blurs the input mid-typing. A render caused
  // only by a keystroke skips that scheduling (the caret restore still runs).
  component.updated = function patchedUpdated() {
    if (!this.gdxTypingRender) return updated.call(this);
    this.gdxTypingRender = false;
    const { overlay } = this;
    this.overlay = null;
    try {
      return updated.call(this);
    } finally {
      this.overlay = overlay;
    }
  };
  component.computed.inputFieldValue = function patchedInputFieldValue() {
    return this.gdxTypedText ?? inputFieldValue.call(this);
  };
  methods.updateModel = function patchedUpdateModel(value) {
    this.gdxTypedText = null;
    return updateModel.call(this, value);
  };
  modelWatch.handler = function patchedModelHandler(newValue, oldValue) {
    if (!this.typeUpdate) this.gdxTypedText = null;
    return modelHandler.call(this, newValue, oldValue);
  };

  methods.onInput = function onInput(event) {
    const text = event.target.value;
    this.selectionStart = this.input?.selectionStart;
    this.selectionEnd = this.input?.selectionEnd;
    if (this.$refs.clearIcon?.$el?.style) {
      this.$refs.clearIcon.$el.style.display = text ? 'block' : 'none';
    }
    const parsed = parseTyped(this, text);
    if (parsed && this.formatValue(parsed.value) === (text ?? '')) {
      this.typeUpdate = true;
      commitTyped(this, parsed.value);
    } else if (this.gdxTypedText !== (text ?? '')) {
      this.gdxTypedText = text ?? '';
      this.gdxTypingRender = true;
      this.$nextTick(() => { this.gdxTypingRender = false; });
    }
    this.$emit('input', event);
  };

  methods.onBlur = function patchedOnBlur(event) {
    if (this.manualInput) commitPending(this, event.target.value);
    this.gdxTypedText = null;
    return onBlur.call(this, event);
  };

  methods.onKeyDown = function patchedOnKeyDown(event) {
    const enter = event.code === 'Enter' || event.code === 'NumpadEnter' || event.key === 'Enter';
    if (enter && this.manualInput) commitPending(this, event.target.value);
    return onKeyDown.call(this, event);
  };

  methods[PATCHED] = true;
  return component;
}

export function isDatePickerTypingInstalled(component = DatePicker) {
  return Boolean(component.methods?.[PATCHED]);
}

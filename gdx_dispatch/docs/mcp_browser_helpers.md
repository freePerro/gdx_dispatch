# MCP Browser-Walk Helpers

Workarounds for chrome-devtools / playwright MCP tools when they don't quite drive PrimeVue or other components correctly.

## PrimeVue InputNumber — `fill` doesn't update the model

**Symptom.** `mcp__chrome-devtools__fill` (and `mcp__playwright__browser_fill_form`) writes the visible text into a `<InputNumber>` but the bound `v-model` never updates. The form looks filled, save sends the old value (or NaN).

**Cause.** PrimeVue `<InputNumber>` keeps its numeric value in component state and renders a separate formatted string in the DOM input. `fill` sets `el.value` and dispatches `input`, but the React-style native setter is bypassed and Vue's reactivity never sees it.

**Workaround.** Drive the input via the prototype's value setter, then dispatch `input` so PrimeVue's internal handler fires.

```js
// pass to mcp__chrome-devtools__evaluate_script with the input's selector
const el = document.querySelector(SELECTOR);
const setter = Object.getOwnPropertyDescriptor(
  window.HTMLInputElement.prototype, 'value'
).set;
setter.call(el, String(VALUE));
el.dispatchEvent(new Event('input', { bubbles: true }));
el.dispatchEvent(new Event('change', { bubbles: true }));
```

Reusable snippet — call once per InputNumber field in a walk.

**When to revisit.** If we hit this on >2 more components, switch the wrappers to also re-emit `@input` so MCP fill works directly. Until then this snippet is cheaper than touching prod components.

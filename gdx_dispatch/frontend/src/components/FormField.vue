<!--
  FormField — single canonical wrapper for labeled form inputs.

  Replaces the 43 raw <input> + 13 raw <button> + 3 bare <select> regressions
  flagged in audit_ux_2026-05-05_forms_and_validation.md. Provides:
    - PrimeVue InputText / Textarea / Select under the hood (depending on `as` prop)
    - Required-asterisk indicator (zero views had this; audit §B)
    - Per-field error binding via `<small class="p-error">` (zero views had this; audit §C)
    - Proper label `for` / input `id` association for screen readers
    - aria-describedby wiring when an error is present
    - aria-invalid on the input when an error is present

  Usage:
    <FormField v-model="form.email" label="Email" type="email" required :error="errors.email" />
    <FormField v-model="form.notes" label="Notes" as="textarea" :rows="4" />
    <FormField v-model="form.role" label="Role" as="select" :options="roleOptions" optionLabel="label" optionValue="value" />
-->
<template>
  <div class="form-field" :class="{ 'has-error': !!error }">
    <label v-if="label" :for="fieldId">
      {{ label }}<span v-if="required" class="required-asterisk" aria-hidden="true">*</span>
    </label>

    <Textarea
      v-if="as === 'textarea'"
      :id="fieldId"
      :model-value="modelValue"
      :rows="rows"
      :placeholder="placeholder"
      :disabled="disabled"
      :readonly="readonly"
      :required="required"
      :aria-invalid="!!error"
      :aria-describedby="error ? `${fieldId}-error` : undefined"
      class="w-full"
      @update:model-value="$emit('update:modelValue', $event)"
    />

    <Select
      v-else-if="as === 'select'"
      :id="fieldId"
      :model-value="modelValue"
      :options="options"
      :option-label="optionLabel"
      :option-value="optionValue"
      :placeholder="placeholder"
      :disabled="disabled"
      :aria-invalid="!!error"
      :aria-describedby="error ? `${fieldId}-error` : undefined"
      class="w-full"
      @update:model-value="$emit('update:modelValue', $event)"
    />

    <InputText
      v-else
      :id="fieldId"
      :model-value="modelValue"
      :type="type"
      :placeholder="placeholder"
      :disabled="disabled"
      :readonly="readonly"
      :required="required"
      :minlength="minlength"
      :maxlength="maxlength"
      :autocomplete="autocomplete"
      :aria-invalid="!!error"
      :aria-describedby="error ? `${fieldId}-error` : undefined"
      class="w-full"
      @update:model-value="$emit('update:modelValue', $event)"
    />

    <small v-if="error" :id="`${fieldId}-error`" class="p-error" role="alert">
      {{ error }}
    </small>
    <small v-else-if="hint" class="form-hint">{{ hint }}</small>
  </div>
</template>

<script setup>
import { computed } from 'vue';
import InputText from 'primevue/inputtext';
import Textarea from 'primevue/textarea';
import Select from 'primevue/select';

const props = defineProps({
  modelValue: { type: [String, Number, Boolean, Object, Array, null], default: '' },
  label: { type: String, default: '' },
  // Visual + a11y indicator. Does NOT add browser `required` enforcement
  // unless `as` is the default (InputText) — explicit prop respected by all.
  required: { type: Boolean, default: false },
  type: { type: String, default: 'text' },     // for InputText
  placeholder: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
  readonly: { type: Boolean, default: false },
  minlength: { type: [Number, String], default: undefined },
  maxlength: { type: [Number, String], default: undefined },
  autocomplete: { type: String, default: undefined },
  // 'input' (default) | 'textarea' | 'select'
  as: { type: String, default: 'input' },
  rows: { type: [Number, String], default: 3 },
  // Select-specific
  options: { type: Array, default: () => [] },
  optionLabel: { type: String, default: 'label' },
  optionValue: { type: String, default: 'value' },
  // Field-level error message — renders as <small class="p-error">
  error: { type: String, default: '' },
  // Optional hint shown below input when no error
  hint: { type: String, default: '' },
  // Pass-through id; if omitted, auto-generated from label
  id: { type: String, default: '' },
});

defineEmits(['update:modelValue']);

let _autoIdCounter = 0;
const fieldId = computed(() => {
  if (props.id) return props.id;
  if (props.label) {
    return `ff-${props.label.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')}-${++_autoIdCounter}`;
  }
  return `ff-${++_autoIdCounter}`;
});
</script>

<style scoped>
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.form-field label {
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--p-text-muted-color);
}
.required-asterisk {
  color: var(--p-red-500, #dc2626);
  margin-left: 0.15rem;
}
.form-field :deep(.p-inputtext),
.form-field :deep(.p-select),
.form-field :deep(.p-textarea) {
  width: 100%;
}
.form-field.has-error :deep(.p-inputtext),
.form-field.has-error :deep(.p-select),
.form-field.has-error :deep(.p-textarea) {
  border-color: var(--p-red-500, #dc2626);
}
.p-error {
  color: var(--p-red-600, #b91c1c);
  font-size: 0.8rem;
}
.form-hint {
  color: var(--p-text-muted-color);
  font-size: 0.8rem;
}
</style>

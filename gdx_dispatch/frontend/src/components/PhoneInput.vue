<!--
  PhoneInput — the one phone entry field. Wraps PrimeVue InputMask so every
  form auto-formats to "(111)222-3333" as the user types, from a single place
  (change the mask here, every form follows). Drop-in for a phone <InputText>:

    <PhoneInput v-model="form.phone" data-testid="..." class="w-full" />

  Emits the masked value on v-model (so what's stored matches what's shown).
  A legacy number in any shape ("555-123-4567", "15551234567") is re-fitted to
  the mask when the field mounts, normalizing it on the next save. Attributes
  (id / class / placeholder / data-testid) fall through to the input.
-->
<template>
  <InputMask
    :modelValue="modelValue"
    @update:modelValue="(v) => $emit('update:modelValue', v)"
    mask="(999)999-9999"
    :unmask="false"
    :autoClear="false"
  />
</template>

<script setup>
import InputMask from 'primevue/inputmask';

defineProps({
  modelValue: { type: [String, null], default: '' },
});
defineEmits(['update:modelValue']);
</script>

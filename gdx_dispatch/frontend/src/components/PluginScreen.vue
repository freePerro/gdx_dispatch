<template>
  <div class="plugin-screen" data-testid="plugin-screen">
    <p v-if="error" class="plugin-screen__error">{{ error }}</p>
    <section v-for="screen in screens" :key="screen.title" class="plugin-screen__screen">
      <h3>{{ screen.title }}</h3>

      <DataTable v-if="screen.type === 'list'" :value="rows" :loading="loading" dataKey="id">
        <Column v-for="c in screen.columns" :key="c.field" :field="c.field" :header="c.label" />
      </DataTable>

      <form
        v-if="screen.create"
        class="plugin-screen__create"
        @submit.prevent="onCreate"
      >
        <span v-for="f in screen.create.fields" :key="f.name" class="p-field">
          <label :for="`pf-${f.name}`">{{ f.label }}</label>
          <InputText :id="`pf-${f.name}`" v-model="formState[f.name]" :required="f.required" />
        </span>
        <Button type="submit" label="Add" size="small" />
      </form>
    </section>
  </div>
</template>

<script setup>
// Generic host renderer for a plugin's declarative UI manifest (ADR-013 step 4).
// All logic lives in usePluginScreen so it's unit-tested without the DOM; this
// component is the PrimeVue template over it. No plugin-supplied JavaScript ever
// runs in the browser — only data the host renders.
import { onMounted, reactive } from 'vue';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import InputText from 'primevue/inputtext';
import Button from 'primevue/button';
import { useApiWithToast } from '../composables/useApiWithToast';
import { usePluginScreen } from '../composables/usePluginScreen';

const props = defineProps({ pluginKey: { type: String, required: true } });

const api = useApiWithToast();
const { screens, rows, loading, error, load, create } = usePluginScreen(props.pluginKey, api);
const formState = reactive({});

async function onCreate() {
  await create({ ...formState });
  for (const k of Object.keys(formState)) delete formState[k];
}

onMounted(load);
</script>

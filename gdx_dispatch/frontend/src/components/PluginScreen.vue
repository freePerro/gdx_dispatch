<template>
  <div class="plugin-screen" data-testid="plugin-screen">
    <p v-if="error" class="plugin-screen__error">{{ error }}</p>
    <section v-for="screen in screens" :key="screen.title" class="plugin-screen__screen">
      <h3>{{ screen.title }}</h3>

      <DataTable v-if="screen.type === 'list'" :value="rows" :loading="loading" dataKey="id">
        <Column v-for="c in screen.columns" :key="c.field" :field="c.field" :header="c.label" />
      </DataTable>

      <!-- Phase 2 (ADR-014): streamed headless browser, gated by the "browser"
           permission + owner consent on the backend. -->
      <BrowserStream
        v-if="screen.type === 'browser'"
        :plugin-key="pluginKey"
        :url="screen.url"
      />

      <!-- Settings screen: per-field toggles. GET endpoint -> {fields:[{name,on_quote}]};
           Save PUTs {fields:[names that are on]}. -->
      <div v-if="screen.type === 'settings'" class="plugin-screen__settings">
        <div v-for="f in settingsFields" :key="f.name" class="plugin-screen__toggle">
          <Checkbox :inputId="`set-${f.name}`" v-model="f.on" :binary="true" />
          <label :for="`set-${f.name}`">{{ f.name }}</label>
        </div>
        <Button label="Save" size="small" :loading="savingSettings" @click="saveSettings(screen)" />
      </div>

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
import { onMounted, reactive, ref } from 'vue';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import InputText from 'primevue/inputtext';
import Checkbox from 'primevue/checkbox';
import Button from 'primevue/button';
import BrowserStream from './BrowserStream.vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { usePluginScreen } from '../composables/usePluginScreen';

const props = defineProps({ pluginKey: { type: String, required: true } });

const api = useApiWithToast();
const { screens, rows, loading, error, load, create } = usePluginScreen(props.pluginKey, api);
const formState = reactive({});
const settingsFields = ref([]);   // [{ name, on }]
const savingSettings = ref(false);

async function onCreate() {
  await create({ ...formState });
  for (const k of Object.keys(formState)) delete formState[k];
}

async function loadSettings(screen) {
  const data = await api.get(screen.endpoint);
  settingsFields.value = (data?.fields || []).map((f) => ({ name: f.name, on: !!f.on_quote }));
}

async function saveSettings(screen) {
  savingSettings.value = true;
  try {
    const fields = settingsFields.value.filter((f) => f.on).map((f) => f.name);
    await api.put(screen.endpoint, { fields }, { successMessage: 'Saved' });
  } finally {
    savingSettings.value = false;
  }
}

onMounted(async () => {
  await load();
  const s = screens.value.find((x) => x.type === 'settings');
  if (s?.endpoint) await loadSettings(s);
});
</script>

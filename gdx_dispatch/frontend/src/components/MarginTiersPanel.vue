<!--
  MarginTiersPanel — the editable tier-set body. Used both as a standalone
  page (MarginTiersView wraps it in AppLayout) and as a tab in Settings.
-->
<template>
  <div class="margin-tiers-panel">
    <Toolbar>
      <template #start>
        <h3 class="panel-title">Margin Tiers</h3>
      </template>
      <template #end>
        <Button label="Reload" icon="pi pi-refresh" text size="small" @click="loadAll" />
      </template>
    </Toolbar>

    <p class="lede">
      Editable cost→margin tiers used by the pricing engine. Each
      (category, class) combination has its own tier table.
      Range semantics: <code>[cost_min, cost_max)</code> — lower inclusive,
      upper exclusive. Leave the highest tier's <code>cost_max</code> blank
      for "and above". Margin is profit/sell as a decimal (0.35 = 35%).
    </p>

    <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

    <template v-else>
      <Tabs v-model:value="activeCategory">
        <TabList>
          <Tab v-for="cat in categories" :key="cat" :value="cat">
            {{ titleCase(cat) }}
          </Tab>
        </TabList>
        <TabPanels>
          <TabPanel v-for="cat in categories" :key="cat" :value="cat">
            <Tabs v-model:value="activeClass">
              <TabList>
                <Tab v-for="cls in classes" :key="cls" :value="cls">
                  {{ titleCase(cls) }}
                </Tab>
              </TabList>
              <TabPanels>
                <TabPanel v-for="cls in classes" :key="cls" :value="cls">
                  <TierEditor
                    v-if="getSet(cat, cls)"
                    :tier-set="getSet(cat, cls)"
                    :saving="savingKey === `${cat}-${cls}`"
                    @save="onSaveTiers(cat, cls, $event)"
                  />
                  <p v-else class="muted">No tier set found for {{ cat }} / {{ cls }}.</p>
                </TabPanel>
              </TabPanels>
            </Tabs>
          </TabPanel>
        </TabPanels>
      </Tabs>

      <Divider />

      <h3>Loaded Labor Cost</h3>
      <p class="lede">
        Your fully-loaded technician cost per hour — base wage plus burden
        (taxes, benefits, insurance, workers' comp). Industry burden runs
        25–35% on top of base wage. Used to derive cost on labor-matrix lines
        so labor shows up in the profit-margin calculator. Set to <code>0</code>
        to treat labor as pure profit (lines still appear; margin will read
        100%).
      </p>
      <div class="labor-cost-controls">
        <span>$</span>
        <InputNumber
          v-model="loadedLaborRate"
          :min="0" :max="999"
          :min-fraction-digits="2" :max-fraction-digits="2"
          mode="decimal"
          data-testid="loaded-labor-rate-input"
          input-style="width: 8rem"
        />
        <span>/hr</span>
        <Button label="Save Labor Rate" size="small" :loading="savingLaborRate"
          data-testid="save-loaded-labor-rate" @click="saveLoadedLaborRate" />
      </div>

      <Divider />

      <h3>Customer Loyalty Discount</h3>
      <p class="lede">
        Rewards customers based on their trailing 365-day paid invoice volume.
        A customer who has paid <code>$100k</code> in the last 12 months might
        earn a 2% discount on every new estimate; <code>$300k</code> might
        earn 4%. Wholesale accounts doing real annual volume can stack this
        on top of their already-lower wholesale margin. The customer's volume
        is computed from cash collected (not invoiced), so unpaid jobs don't
        inflate the discount.
      </p>
      <div class="vol-controls">
        <ToggleSwitch v-model="volumeEnabled" data-testid="volume-discount-toggle" />
        <span>{{ volumeEnabled ? 'Enabled' : 'Disabled' }} — master switch</span>
        <Button label="Save Toggle" size="small" :loading="savingToggle"
          data-testid="save-volume-toggle" @click="saveVolumeToggle" />
      </div>

      <template v-if="volumeEnabled">
        <h4 class="subhead">Per-Class Eligibility</h4>
        <p class="muted small">
          Choose which pricing classes participate. Both gates (master + class)
          must be on for a customer to earn a discount.
        </p>
        <div class="class-toggles" data-testid="class-toggles">
          <div v-for="cls in classes" :key="cls" class="class-toggle-row">
            <ToggleSwitch
              v-model="classEnabled[cls]"
              :data-testid="`class-toggle-${cls}`"
            />
            <span class="class-name">{{ titleCase(cls) }}</span>
          </div>
          <Button label="Save Class Settings" size="small"
            :loading="savingClassSettings"
            data-testid="save-class-settings"
            @click="saveClassSettings" />
        </div>

        <h4 class="subhead">12-Month Paid Volume Tiers</h4>
        <VolumeTierEditor
          :tiers="volumeTiers"
          :saving="savingVolumeTiers"
          @save="onSaveVolumeTiers"
        />
      </template>
    </template>

    <Toast />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue';
import { useToast } from 'primevue/usetoast';
import { useApiWithToast } from '../composables/useApiWithToast';
import TierEditor from './TierEditor.vue';
import VolumeTierEditor from './VolumeTierEditor.vue';
import Button from 'primevue/button';
import Divider from 'primevue/divider';
import InputNumber from 'primevue/inputnumber';
import ProgressSpinner from 'primevue/progressspinner';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Toast from 'primevue/toast';
import ToggleSwitch from 'primevue/toggleswitch';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();
const toast = useToast();

const categories = ['doors', 'openers', 'parts', 'labor', 'other'];
const classes = ['retail', 'contractor', 'wholesale'];

const tierSets = ref([]);
const volumeEnabled = ref(false);
const volumeTiers = ref([]);
const classEnabled = ref({ retail: true, contractor: true, wholesale: true });
const loadedLaborRate = ref(0);
const loading = ref(true);
const activeCategory = ref('doors');
const activeClass = ref('retail');
const savingKey = ref(null);
const savingToggle = ref(false);
const savingVolumeTiers = ref(false);
const savingClassSettings = ref(false);
const savingLaborRate = ref(false);

function getSet(category, cls) {
  return tierSets.value.find(s => s.pricing_category === category && s.pricing_class === cls);
}

async function loadAll() {
  loading.value = true;
  try {
    const [sets, settings] = await Promise.all([
      api.get('/api/pricing-engine/tier-sets'),
      api.get('/api/pricing-engine/settings'),
    ]);
    // Defensive coercion — API may return [], {items: []}, or {} on
    // empty/uninitialized tenants; downstream getSet/findIndex assume
    // an array and threw 'tierSets.value.find is not a function' under
    // mocked-empty fixtures.
    tierSets.value = Array.isArray(sets) ? sets : (sets?.items || []);
    const settingsObj = settings && typeof settings === 'object' ? settings : {};
    volumeEnabled.value = !!settingsObj.volume_discount_enabled;
    volumeTiers.value = settingsObj.volume_tiers || [];
    loadedLaborRate.value = Number(settingsObj.loaded_labor_cost_per_hour) || 0;
    const map = { retail: true, contractor: true, wholesale: true };
    for (const c of (settingsObj.class_settings || [])) {
      map[c.pricing_class] = !!c.rolling_volume_discount_enabled;
    }
    classEnabled.value = map;
  } finally {
    loading.value = false;
  }
}

async function onSaveTiers(category, cls, tiers) {
  const set = getSet(category, cls);
  if (!set) return;
  savingKey.value = `${category}-${cls}`;
  try {
    const updated = await api.put(`/api/pricing-engine/tier-sets/${set.id}`,
      { tiers }, { successMessage: `Saved ${titleCase(category)} / ${titleCase(cls)} tiers` });
    const idx = tierSets.value.findIndex(s => s.id === set.id);
    if (idx >= 0) tierSets.value[idx] = updated;
  } finally {
    savingKey.value = null;
  }
}

async function saveLoadedLaborRate() {
  savingLaborRate.value = true;
  try {
    await api.patch('/api/pricing-engine/settings',
      { loaded_labor_cost_per_hour: Number(loadedLaborRate.value) || 0 },
      { successMessage: `Loaded labor cost saved ($${(Number(loadedLaborRate.value) || 0).toFixed(2)}/hr)` });
  } finally {
    savingLaborRate.value = false;
  }
}

async function saveVolumeToggle() {
  savingToggle.value = true;
  try {
    await api.patch('/api/pricing-engine/settings',
      { volume_discount_enabled: volumeEnabled.value },
      { successMessage: `Volume discount ${volumeEnabled.value ? 'enabled' : 'disabled'}` });
  } finally {
    savingToggle.value = false;
  }
}

async function onSaveVolumeTiers(tiers) {
  savingVolumeTiers.value = true;
  try {
    const updated = await api.put('/api/pricing-engine/volume-tiers',
      { tiers }, { successMessage: 'Volume tiers saved' });
    volumeTiers.value = updated.volume_tiers || [];
  } finally {
    savingVolumeTiers.value = false;
  }
}

async function saveClassSettings() {
  savingClassSettings.value = true;
  try {
    const payload = {
      classes: classes.map(c => ({
        pricing_class: c,
        rolling_volume_discount_enabled: classEnabled.value[c],
      })),
    };
    await api.put('/api/pricing-engine/class-settings', payload,
      { successMessage: 'Per-class settings saved' });
  } finally {
    savingClassSettings.value = false;
  }
}

function titleCase(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

onMounted(loadAll);
</script>

<style scoped>
.margin-tiers-panel { padding: 4px 0; }
.panel-title { margin: 0; font-size: 1.1em; }
.lede { color: var(--p-text-muted-color); margin: 8px 0 16px; max-width: 720px; }
.lede code { background: var(--p-content-hover-background); padding: 1px 4px; border-radius: 3px; font-size: 0.9em; }
.spinner-wrap { display: flex; justify-content: center; padding: 32px; }
.vol-controls { display: flex; gap: 12px; align-items: center; padding: 8px 0 16px; }
.labor-cost-controls { display: flex; gap: 8px; align-items: center; padding: 8px 0 16px; }
.muted { color: var(--p-text-muted-color); }
.muted.small { font-size: 0.9em; }
.subhead { margin: 20px 0 6px; font-size: 1em; }
.class-toggles { display: flex; gap: 24px; align-items: center; padding: 8px 0 16px; flex-wrap: wrap; }
.class-toggle-row { display: flex; gap: 8px; align-items: center; }
.class-name { min-width: 88px; }
</style>

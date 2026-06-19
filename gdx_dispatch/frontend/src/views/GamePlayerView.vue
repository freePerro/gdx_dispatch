<template>
    <section class="game-player-view view-card">
      <div v-if="loading" class="game-player-view__loading">
        <ProgressSpinner />
        <div>Loading game...</div>
      </div>

      <div v-else-if="error" class="game-player-view__error">
        <h2>Couldn't load this game</h2>
        <p>{{ error }}</p>
        <Button label="Back to catalog" icon="pi pi-arrow-left" @click="$router.push('/admin/games')" />
      </div>

      <div v-else-if="definition && state" class="game-player-view__panel">
        <header class="game-player-view__header">
          <Button
            icon="pi pi-arrow-left"
            text
            aria-label="Back to catalog"
            @click="$router.push('/admin/games')"
          />
          <div class="game-player-view__titles">
            <h1>{{ definition.layout_json.title || definition.name }}</h1>
            <p v-if="definition.layout_json.subtitle">{{ definition.layout_json.subtitle }}</p>
          </div>
          <div class="game-player-view__publisher">
            <span class="badge">{{ definition.publisher }}</span>
          </div>
        </header>

        <div v-if="state.current_phase" class="game-player-view__phase">
          Phase: <strong>{{ formatPhase(state.current_phase) }}</strong>
        </div>

        <div class="game-player-view__widgets">
          <component
            v-for="(widget, idx) in (definition.layout_json.widgets || [])"
            :key="`${widget.type}-${idx}`"
            :is="widgetMap[widget.type]"
            :state="state"
            :config="widget"
            @event="onWidgetEvent"
          />
          <div
            v-for="(widget, idx) in unknownWidgets"
            :key="`unknown-${idx}`"
            class="game-player-view__unknown"
          >
            ⚠ Unknown widget type: <code>{{ widget.type }}</code>
          </div>
        </div>
      </div>
    </section>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import Button from 'primevue/button';
import ProgressSpinner from 'primevue/progressspinner';
import { useToast } from 'primevue/usetoast';
import { useApi } from '../composables/useApi';
import { widgetMap } from '../components/games/widgets';

const route = useRoute();
const api = useApi();
const toast = useToast();

const slug = computed(() => route.params.slug || route.meta.slug || 'coop');
const actor = computed(() => route.query.actor || route.meta.actor || 'claude');

const definition = ref(null);
const state = ref(null);
const loading = ref(true);
const error = ref('');

const unknownWidgets = computed(() => {
  if (!definition.value) return [];
  const widgets = definition.value.layout_json?.widgets || [];
  return widgets.filter(w => !widgetMap[w.type]);
});

async function loadAll() {
  loading.value = true;
  error.value = '';
  try {
    const [catalog, currentState] = await Promise.all([
      api.get(`/api/games/catalog`),
      api.get(`/api/games/state?actor=${encodeURIComponent(actor.value)}&game=${encodeURIComponent(slug.value)}`),
    ]);
    const def = (catalog || []).find(d => d.slug === slug.value);
    if (!def) {
      error.value = `Game "${slug.value}" not found in the catalog.`;
      return;
    }
    definition.value = def;
    state.value = currentState;
  } catch (e) {
    error.value = e.message || 'Failed to load game.';
  } finally {
    loading.value = false;
  }
}

async function onWidgetEvent(payload) {
  // payload = { event_type, value, value_string?, reason? }
  try {
    const updated = await api.post('/api/games/event', {
      actor_id: actor.value,
      game_slug: slug.value,
      event_type: payload.event_type,
      value: payload.value ?? null,
      value_string: payload.value_string ?? null,
      reason: payload.reason ?? null,
    });
    state.value = updated;
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Failed to record event', detail: e.message || 'unknown error', life: 4000 });
  }
}

function formatPhase(phase) {
  return (phase || '').replace(/_/g, ' ');
}

onMounted(loadAll);
</script>

<style scoped>
.game-player-view {
  max-width: 720px;
  margin: 0 auto;
  padding: 1rem;
}
.game-player-view__loading,
.game-player-view__error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1rem;
  padding: 3rem 1rem;
}
.game-player-view__panel {
  background: white;
  border-radius: 0.75rem;
  padding: 1.25rem 1.5rem 1.5rem;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
}
.game-player-view__header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  border-bottom: 1px solid #e2e8f0;
  padding-bottom: 1rem;
  margin-bottom: 0.5rem;
}
.game-player-view__titles { flex: 1; }
.game-player-view__titles h1 {
  margin: 0;
  font-size: 1.25rem;
  color: #0f172a;
}
.game-player-view__titles p {
  margin: 0.125rem 0 0 0;
  font-size: 0.875rem;
  color: #64748b;
}
.game-player-view__publisher .badge {
  background: #f1f5f9;
  color: #475569;
  padding: 0.25rem 0.625rem;
  border-radius: 9999px;
  font-size: 0.75rem;
  font-weight: 600;
}
.game-player-view__phase {
  font-size: 0.875rem;
  color: #64748b;
  margin-bottom: 0.5rem;
  padding: 0.5rem 0.75rem;
  background: #f8fafc;
  border-radius: 0.375rem;
}
.game-player-view__phase strong {
  color: #0f172a;
  text-transform: capitalize;
}
.game-player-view__widgets {
  display: flex;
  flex-direction: column;
}
.game-player-view__widgets > * + * {
  border-top: 1px solid #f1f5f9;
}
.game-player-view__unknown {
  color: #b45309;
  background: #fffbeb;
  padding: 0.5rem 0.75rem;
  border-radius: 0.375rem;
  font-size: 0.875rem;
  margin-top: 0.5rem;
}
</style>

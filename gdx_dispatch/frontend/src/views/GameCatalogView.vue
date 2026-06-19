<template>
    <section class="game-catalog-view view-card">
      <header class="game-catalog-view__header">
        <h1>🎮 Games</h1>
        <p class="game-catalog-view__subtitle">
          Pick a game. Each one motivates a different kind of player. More games coming as GDX grows.
        </p>
      </header>

      <div v-if="loading" class="game-catalog-view__loading">
        <ProgressSpinner />
      </div>

      <div v-else-if="error" class="game-catalog-view__error">
        Couldn't load the catalog: {{ error }}
      </div>

      <div v-else class="game-catalog-view__grid">
        <!-- Live games from the API -->
        <div
          v-for="game in liveGames"
          :key="game.slug"
          class="game-card game-card--live"
          @click="play(game)"
          @keyup.enter="play(game)"
          tabindex="0"
          role="button"
          :aria-label="`Play ${game.name}`"
        >
          <div class="game-card__icon">{{ iconFor(game) }}</div>
          <div class="game-card__body">
            <div class="game-card__title-row">
              <h2 class="game-card__name">{{ game.name }}</h2>
              <span class="game-card__publisher">{{ game.publisher }}</span>
            </div>
            <p class="game-card__description">{{ game.description }}</p>
            <div class="game-card__actor-type">For: {{ formatActor(game.actor_type) }}</div>
          </div>
          <Button label="Play" icon="pi pi-play" class="game-card__play" />
        </div>

        <!-- Coming soon placeholders (static, roadmap preview) -->
        <div
          v-for="placeholder in placeholders"
          :key="placeholder.slug"
          class="game-card game-card--coming-soon"
          :aria-label="`${placeholder.name} — coming soon`"
        >
          <div class="game-card__icon">{{ placeholder.icon }}</div>
          <div class="game-card__body">
            <div class="game-card__title-row">
              <h2 class="game-card__name">{{ placeholder.name }}</h2>
              <span class="game-card__publisher">House</span>
            </div>
            <p class="game-card__description">{{ placeholder.description }}</p>
            <div class="game-card__actor-type">For: {{ formatActor(placeholder.actor_type) }}</div>
          </div>
          <div class="game-card__coming-soon">Coming soon</div>
        </div>
      </div>
    </section>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import Button from 'primevue/button';
import ProgressSpinner from 'primevue/progressspinner';
import { useApi } from '../composables/useApi';

const router = useRouter();
const api = useApi();

const liveGames = ref([]);
const loading = ref(true);
const error = ref('');

// Roadmap preview — these don't exist in the database yet. They're hardcoded
// here to show the future shape of the catalog. As each one ships, it moves
// from this list into the database and gets removed from here.
const placeholders = [
  {
    slug: 'owner_garden',
    name: "Owner's Garden",
    icon: '🪴',
    description: "Every GDX module is a plant in your garden. Walk in each morning and see the health of your business at a glance — sick plants are problems, thriving plants are wins.",
    actor_type: 'owner',
  },
  {
    slug: 'tech_helper',
    name: "Tech's Helper",
    icon: '🤖',
    description: "Your tech gets a small AI companion in the dispatch app. Feed it by completing jobs cleanly. It levels up over weeks of consistent work and earns abilities like pre-filling repeat-customer notes.",
    actor_type: 'tech',
  },
  {
    slug: 'dispatcher_board',
    name: "Dispatcher's Board",
    icon: '🧩',
    description: "Tetris-like view of the dispatch board. Unscheduled jobs are blocks. Trucks are columns. Streaks for high-utilization days.",
    actor_type: 'dispatcher',
  },
  {
    slug: 'customer_tracker',
    name: 'Customer Job Tracker',
    icon: '📍',
    description: "When a homeowner books a repair, they see job progress with milestones and a tiny AI helper that answers 'when will the tech arrive?'",
    actor_type: 'customer',
  },
];

async function loadCatalog() {
  loading.value = true;
  error.value = '';
  try {
    const games = await api.get('/api/games/catalog');
    liveGames.value = games || [];
  } catch (e) {
    error.value = e.message || 'Failed to load catalog.';
  } finally {
    loading.value = false;
  }
}

function iconFor(game) {
  if (game.icon === 'garage_door') return '🚪';
  return game.icon || '🎮';
}

function formatActor(actorType) {
  const map = {
    claude: 'Claude',
    owner: 'Shop owner',
    tech: 'Field technician',
    dispatcher: 'Dispatcher',
    customer: 'End customer',
  };
  return map[actorType] || actorType;
}

function play(game) {
  router.push({ name: 'GamePlayer', params: { slug: game.slug } });
}

onMounted(loadCatalog);
</script>

<style scoped>
.game-catalog-view {
  max-width: 960px;
  margin: 0 auto;
  padding: 1.5rem 1rem;
}
.game-catalog-view__header {
  margin-bottom: 1.5rem;
}
.game-catalog-view__header h1 {
  margin: 0 0 0.25rem 0;
  font-size: 1.75rem;
  color: #0f172a;
}
.game-catalog-view__subtitle {
  margin: 0;
  color: #64748b;
  font-size: 0.9375rem;
}
.game-catalog-view__loading,
.game-catalog-view__error {
  display: flex;
  justify-content: center;
  padding: 3rem 1rem;
  color: #94a3b8;
}
.game-catalog-view__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 1rem;
}

.game-card {
  background: white;
  border: 1px solid #e2e8f0;
  border-radius: 0.75rem;
  padding: 1rem 1.25rem 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  transition: all 0.15s;
}
.game-card--live {
  cursor: pointer;
}
.game-card--live:hover {
  border-color: #3b82f6;
  box-shadow: 0 4px 12px rgba(59, 130, 246, 0.15);
  transform: translateY(-2px);
}
.game-card--live:focus {
  outline: 2px solid #3b82f6;
  outline-offset: 2px;
}
.game-card--coming-soon {
  opacity: 0.65;
  background: #f8fafc;
}
.game-card__icon {
  font-size: 2.5rem;
}
.game-card__body {
  flex: 1;
}
.game-card__title-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.5rem;
  margin-bottom: 0.25rem;
}
.game-card__name {
  margin: 0;
  font-size: 1.0625rem;
  color: #0f172a;
}
.game-card__publisher {
  background: #f1f5f9;
  color: #475569;
  padding: 0.125rem 0.5rem;
  border-radius: 9999px;
  font-size: 0.6875rem;
  font-weight: 600;
  white-space: nowrap;
}
.game-card__description {
  margin: 0.25rem 0;
  font-size: 0.875rem;
  color: #475569;
  line-height: 1.4;
}
.game-card__actor-type {
  font-size: 0.75rem;
  color: #94a3b8;
  margin-top: 0.5rem;
}
.game-card__play {
  align-self: flex-start;
}
.game-card__coming-soon {
  align-self: flex-start;
  background: #fef3c7;
  color: #92400e;
  padding: 0.375rem 0.75rem;
  border-radius: 0.375rem;
  font-size: 0.75rem;
  font-weight: 600;
}
</style>

<template>
  <Drawer
    v-model:visible="visible"
    position="right"
    class="help-drawer"
    :show-close-icon="true"
    :pt="{ root: { 'data-test': 'help-drawer' } }"
  >
    <template #header>
      <div class="help-header">
        <button
          v-if="store.currentSlug"
          type="button"
          class="help-back"
          aria-label="Back to help index"
          @click="store.backToSearch()"
        >
          <i class="pi pi-arrow-left" aria-hidden="true" />
        </button>
        <h2>{{ headerLabel }}</h2>
      </div>
    </template>

    <!-- Loading -->
    <div v-if="store.loading" class="help-loading">
      <i class="pi pi-spinner pi-spin" aria-hidden="true" /> Loading help…
    </div>

    <!-- Error -->
    <div v-else-if="store.error" class="help-error">
      <i class="pi pi-exclamation-triangle" aria-hidden="true" />
      Couldn't load help right now: {{ store.error }}
    </div>

    <!-- Article view -->
    <article v-else-if="store.currentSlug && store.currentArticle" class="help-article">
      <div class="help-article-body" v-html="store.renderedHtml" />
      <section v-if="related.length" class="help-related">
        <h3>Related</h3>
        <ul>
          <li v-for="rel in related" :key="rel.slug">
            <a href="#" data-test="help-related-link" @click.prevent="store.showArticle(rel.slug, 'related')">
              {{ rel.title }}
            </a>
          </li>
        </ul>
      </section>
    </article>

    <!-- Search + browse view -->
    <div v-else class="help-browse">
      <div class="help-search">
        <i class="pi pi-search" aria-hidden="true" />
        <InputText
          v-model="searchQuery"
          placeholder="Search help articles…"
          aria-label="Search help"
          data-test="help-search-input"
          @input="onSearchInput"
        />
      </div>

      <div v-if="store.searchResults.length" class="help-results">
        <h3>Results</h3>
        <ul>
          <li v-for="r in store.searchResults" :key="r.id">
            <a href="#" data-test="help-result-link" @click.prevent="store.showArticle(r.slug, 'search')">
              <strong>{{ r.title }}</strong>
              <span v-if="r.tags?.length" class="help-tags">{{ r.tags.join(' · ') }}</span>
            </a>
          </li>
        </ul>
      </div>

      <div v-else-if="searchQuery.trim().length >= 2" class="help-empty">
        No results for "{{ searchQuery }}". Try different words.
      </div>

      <div v-else class="help-featured">
        <h3>Browse by role</h3>
        <ul>
          <li v-for="a in roleFeatured" :key="a.slug">
            <a href="#" @click.prevent="store.showArticle(a.slug, 'browse')">
              {{ a.title }}
              <span v-if="a.role" class="help-tag-pill">{{ a.role }}</span>
            </a>
          </li>
        </ul>
        <h3 v-if="otherArticles.length">Everything else</h3>
        <ul>
          <li v-for="a in otherArticles" :key="a.slug">
            <a href="#" @click.prevent="store.showArticle(a.slug, 'browse')">{{ a.title }}</a>
          </li>
        </ul>
      </div>
    </div>
  </Drawer>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import Drawer from 'primevue/drawer';
import InputText from 'primevue/inputtext';
import { useHelpStore } from '../stores/help';
import { useAuthStore } from '../stores/auth';

const store = useHelpStore();
const auth = useAuthStore();

const searchQuery = ref('');

const visible = computed({
  get: () => store.isOpen,
  set: (v) => { if (!v) store.close(); },
});

const headerLabel = computed(() => {
  if (store.currentArticle) return store.currentArticle.title;
  return 'Help';
});

function onSearchInput() {
  store.search(searchQuery.value);
}

watch(
  () => store.isOpen,
  (open) => {
    if (open) {
      searchQuery.value = store.searchQuery || '';
    }
  },
);

// Bridge: a tour step's "Learn more" button dispatches `gdx:help-open`
// with `{ slug, source }`. Open the drawer to that article.
function handleHelpOpen(e) {
  const detail = e?.detail || {};
  if (!detail.slug) return;
  store.open(detail.slug);
}
onMounted(() => {
  window.addEventListener('gdx:help-open', handleHelpOpen);
});
onBeforeUnmount(() => {
  window.removeEventListener('gdx:help-open', handleHelpOpen);
});

const currentRole = computed(() => String(auth.user?.role || '').toLowerCase());

const roleFeatured = computed(() => {
  const r = currentRole.value;
  if (!r) return [];
  return store.articles.filter((a) => {
    const aRole = String(a.role || 'all').toLowerCase();
    return aRole === r || aRole === 'all';
  }).slice(0, 6);
});

const otherArticles = computed(() => {
  const featured = new Set(roleFeatured.value.map((a) => a.slug));
  return store.articles.filter((a) => !featured.has(a.slug)).slice(0, 20);
});

const related = computed(() => {
  const a = store.currentArticle;
  if (!a || !a.related?.length) return [];
  return a.related
    .map((slug) => store.articles.find((x) => x.slug === slug))
    .filter(Boolean);
});
</script>

<style scoped>
.help-drawer {
  width: min(480px, 96vw);
}

.help-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.help-header h2 {
  font-size: 1.125rem;
  font-weight: 600;
  margin: 0;
}
.help-back {
  border: none;
  background: transparent;
  color: var(--text-primary);
  cursor: pointer;
  padding: var(--space-1) var(--space-2);
  border-radius: 0.5rem;
}
.help-back:hover {
  background: var(--surface-hover);
}

.help-loading,
.help-error,
.help-empty {
  padding: var(--space-4);
  color: var(--text-muted);
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.help-search {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: 0.5rem;
  padding: 0 var(--space-2);
  margin-bottom: var(--space-4);
}
.help-search .pi-search {
  color: var(--text-muted);
}
.help-search :deep(.p-inputtext) {
  border: none;
  background: transparent;
  width: 100%;
}

.help-results h3,
.help-featured h3 {
  font-size: 0.875rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  margin: var(--space-4) 0 var(--space-2);
}

.help-results ul,
.help-featured ul {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.help-results li a,
.help-featured li a {
  display: block;
  padding: var(--space-2) var(--space-3);
  border-radius: 0.5rem;
  color: var(--text-primary);
  text-decoration: none;
}
.help-results li a:hover,
.help-featured li a:hover {
  background: var(--surface-hover);
}
.help-tags {
  display: block;
  font-size: 0.8125rem;
  color: var(--text-muted);
  margin-top: var(--space-1);
}
.help-tag-pill {
  margin-left: var(--space-2);
  padding: 0.125rem 0.5rem;
  background: var(--surface-elevated);
  border-radius: 999px;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.help-article {
  padding-bottom: var(--space-6);
}
.help-article-body :deep(h1) {
  font-size: 1.375rem;
  font-weight: 700;
  margin: 0 0 var(--space-3);
}
.help-article-body :deep(h2) {
  font-size: 1.125rem;
  font-weight: 600;
  margin: var(--space-4) 0 var(--space-2);
}
.help-article-body :deep(p),
.help-article-body :deep(li) {
  line-height: 1.55;
}
.help-article-body :deep(code) {
  background: var(--surface-elevated);
  padding: 0.0625rem 0.375rem;
  border-radius: 0.25rem;
  font-size: 0.9em;
}
.help-article-body :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 0.5rem;
  margin: var(--space-2) 0;
}

.help-related {
  margin-top: var(--space-6);
  padding-top: var(--space-4);
  border-top: 1px solid var(--border-subtle);
}
.help-related h3 {
  font-size: 0.875rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  margin: 0 0 var(--space-2);
}
.help-related ul {
  list-style: none;
  padding: 0;
  margin: 0;
}
.help-related li a {
  display: block;
  padding: var(--space-1) 0;
  color: var(--interactive-primary);
  text-decoration: none;
}
.help-related li a:hover {
  text-decoration: underline;
}
</style>

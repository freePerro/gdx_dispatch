<template>
  <section class="release-notes-view view-card" data-testid="release-notes-view">
    <div class="view-heading-row">
      <h1 class="view-heading">What's new</h1>
      <span v-if="installed" class="muted" data-testid="release-notes-installed">
        You're on v{{ installed }}
      </span>
    </div>
    <p class="muted intro">
      What changed in each release, newest first. If something works
      differently than it used to, search for it here to see which release
      changed it.
    </p>

    <Message
      v-if="updateInfo && updateInfo.update_available"
      severity="info"
      :closable="false"
      class="update-notice"
      data-testid="release-notes-update-available"
    >
      Version v{{ updateInfo.latest }} is available (you're on v{{ updateInfo.current }}).
      Updates are installed on the server: run <code>docker/update.sh</code>.
      <a
        v-if="updateInfo.notes_url"
        :href="updateInfo.notes_url"
        target="_blank"
        rel="noopener noreferrer"
      >Read the v{{ updateInfo.latest }} notes</a>
    </Message>

    <div class="search-row">
      <span class="search-box">
        <i class="pi pi-search" aria-hidden="true" />
        <InputText
          v-model="search"
          placeholder="Search all release notes…"
          aria-label="Search all release notes"
          data-testid="release-notes-search"
          autocomplete="off"
        />
      </span>
    </div>

    <Message v-if="missingVersion" severity="warn" :closable="false" data-testid="release-notes-missing">
      There are no notes for v{{ missingVersion }}. The releases this build knows about are listed below.
    </Message>

    <div v-if="loading" class="state" data-testid="release-notes-loading">Loading release notes…</div>
    <Message v-else-if="error" severity="error" :closable="false" data-testid="release-notes-error">
      {{ error }}
    </Message>
    <div v-else-if="!filtered.length" class="state" data-testid="release-notes-empty">
      <template v-if="search">No release mentions "{{ search }}".</template>
      <template v-else>No release notes are bundled with this build.</template>
    </div>

    <article
      v-for="release in shown"
      :id="`v${release.version}`"
      :key="release.version"
      class="release"
      :class="{ targeted: release.version === targetVersion }"
      :data-testid="`release-${release.version}`"
    >
      <header class="release-header">
        <h2 class="release-title">
          <RouterLink :to="`/release-notes/${release.version}`" class="release-version">
            v{{ release.version }}
          </RouterLink>
          <span v-if="release.title" class="release-name">{{ release.title }}</span>
        </h2>
        <div class="release-meta">
          <span class="muted">{{ formatReleaseDate(release.date) }}</span>
          <Tag v-if="release.version === installed" value="Installed" severity="success" data-testid="release-installed-tag" />
          <Tag v-if="release.warning" value="Warning" severity="warn" />
        </div>
      </header>

      <Message v-if="release.warning" severity="warn" :closable="false" class="release-warning">
        {{ release.warning }}
      </Message>

      <div v-for="(section, si) in release.sections" :key="si" class="release-section">
        <h3 v-if="section.title" class="section-title">{{ section.title }}</h3>
        <template v-for="(block, bi) in section.blocks" :key="bi">
          <h4 v-if="block.kind === 'heading'" class="block-heading">{{ block.text }}</h4>
          <p v-else-if="block.kind === 'paragraph'" class="block-paragraph">{{ block.text }}</p>
          <ul v-else-if="block.kind === 'list'" class="block-list">
            <li v-for="(item, ii) in block.items" :key="ii">{{ item }}</li>
          </ul>
        </template>
      </div>
      <p v-if="!release.sections.length && !release.changes.length" class="muted">
        No notes were written for this release.
      </p>

      <div v-if="release.changes.length" class="release-changes">
        <Button
          :label="`${expanded.has(release.version) ? 'Hide' : 'Show'} all changes (${release.changes.length})`"
          :icon="expanded.has(release.version) ? 'pi pi-chevron-up' : 'pi pi-chevron-down'"
          text
          size="small"
          :aria-expanded="expanded.has(release.version)"
          :data-testid="`release-changes-toggle-${release.version}`"
          @click="toggle(release.version)"
        />
        <ul v-if="expanded.has(release.version)" class="changes-list" :data-testid="`release-changes-${release.version}`">
          <li v-for="(change, ci) in release.changes" :key="ci">{{ change }}</li>
        </ul>
      </div>
    </article>

    <div v-if="filtered.length > shown.length" class="more-row">
      <Button
        :label="`Show older releases (${filtered.length - shown.length} more)`"
        severity="secondary"
        outlined
        data-testid="release-notes-show-more"
        @click="limit += PAGE"
      />
    </div>
  </section>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Tag from 'primevue/tag';
import { useAuthStore } from '../stores/auth';
import {
  loadReleaseNotes,
  markReleaseSeen,
  useUpdateCheck,
} from '../composables/useReleaseNotes';

const PAGE = 20;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const route = useRoute();
const auth = useAuthStore();
const { updateInfo, loadUpdateCheck } = useUpdateCheck(auth);

const releases = ref([]);
const installed = ref(null);
const loading = ref(true);
const error = ref('');
const missingVersion = ref(null);
const search = ref('');
const limit = ref(PAGE);
const expanded = reactive(new Set());

const targetVersion = computed(() => {
  const raw = String(route.params.version || '').replace(/^v/i, '');
  return raw || null;
});

function releaseText(r) {
  const parts = [r.version, `v${r.version}`, r.date, r.title, r.warning, ...r.changes];
  for (const s of r.sections) {
    parts.push(s.title);
    for (const b of s.blocks) {
      if (b.kind === 'list') parts.push(...b.items);
      else parts.push(b.text);
    }
  }
  return parts.join('\n').toLowerCase();
}

const indexed = computed(() => releases.value.map((r) => ({ release: r, text: releaseText(r) })));

const filtered = computed(() => {
  const words = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return releases.value;
  return indexed.value
    .filter(({ text }) => words.every((w) => text.includes(w)))
    .map(({ release }) => release);
});

const shown = computed(() => filtered.value.slice(0, limit.value));

watch(search, () => { limit.value = PAGE; });

function toggle(version) {
  if (expanded.has(version)) expanded.delete(version);
  else expanded.add(version);
}

// "2026-10-12" → "Oct 12, 2026". Formatted by hand: new Date('2026-10-12')
// is UTC midnight, which renders as the day before west of Greenwich.
function formatReleaseDate(date) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date || '');
  if (!m) return date || '';
  return `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}, ${m[1]}`;
}

function afterNextFrame() {
  if (typeof requestAnimationFrame !== 'function') return Promise.resolve();
  return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}

async function showTarget() {
  const version = targetVersion.value;
  if (!version) return;
  const index = filtered.value.findIndex((r) => r.version === version);
  if (index === -1) {
    if (!search.value) missingVersion.value = version;
    return;
  }
  if (index >= limit.value) limit.value = index + 1;
  if (filtered.value[index].changes.length) expanded.add(version);
  await nextTick();
  // The router resets .layout-content to the top on an animation frame after
  // every navigation (lib/scrollRestore.js applyScroll). With the notes
  // already cached this runs first and is undone; scroll after that frame.
  await afterNextFrame();
  document.getElementById(`v${version}`)?.scrollIntoView?.({ block: 'start' });
}

watch(targetVersion, () => {
  missingVersion.value = null;
  showTarget();
});

onMounted(async () => {
  try {
    const data = await loadReleaseNotes();
    releases.value = data?.releases || [];
    installed.value = data?.installed_version || null;
  } catch (err) {
    error.value = err?.message || 'Could not load the release notes.';
  } finally {
    loading.value = false;
  }
  await showTarget();
  // Opening the notes for the installed release counts as reading them.
  if (installed.value && releases.value.some((r) => r.version === installed.value)) {
    markReleaseSeen(installed.value);
  }
  loadUpdateCheck();
});
</script>

<style scoped>
.release-notes-view {
  max-width: 860px;
}
.intro {
  margin: 0 0 var(--space-3);
}
.update-notice {
  margin-bottom: var(--space-3);
}
.update-notice code {
  font-size: 0.9em;
}
.update-notice a {
  margin-left: var(--space-1);
}
.search-row {
  margin-bottom: var(--space-4);
}
.search-box {
  position: relative;
  display: block;
}
.search-box .pi-search {
  position: absolute;
  left: 0.75rem;
  top: 50%;
  transform: translateY(-50%);
  color: var(--p-text-muted-color);
  pointer-events: none;
}
.search-box :deep(input) {
  width: 100%;
  padding-left: 2.25rem;
}
.state {
  padding: var(--space-4) 0;
  color: var(--p-text-muted-color);
}
.release {
  padding: var(--space-4) 0;
  border-top: 1px solid var(--p-content-border-color);
  scroll-margin-top: var(--space-4);
}
.release.targeted {
  border-left: 3px solid var(--p-primary-color);
  padding-left: var(--space-3);
}
.release-header {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}
.release-title {
  margin: 0;
  font-size: 1.15rem;
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: baseline;
  min-width: 0;
}
.release-version {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--p-primary-color);
  text-decoration: none;
}
.release-version:hover {
  text-decoration: underline;
}
.release-name {
  overflow-wrap: anywhere;
}
.release-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: 0.85rem;
}
.release-warning {
  margin: var(--space-3) 0 0;
}
.section-title {
  margin: var(--space-3) 0 var(--space-1);
  font-size: 1rem;
}
.block-heading {
  margin: var(--space-2) 0 var(--space-1);
  font-size: 0.95rem;
}
.block-paragraph {
  margin: var(--space-1) 0;
  overflow-wrap: anywhere;
}
.block-list,
.changes-list {
  margin: var(--space-1) 0;
  padding-left: 1.25rem;
}
.block-list li,
.changes-list li {
  margin: 0.2rem 0;
  overflow-wrap: anywhere;
}
.release-changes {
  margin-top: var(--space-2);
}
.changes-list {
  font-size: 0.875rem;
  color: var(--p-text-muted-color);
}
.more-row {
  display: flex;
  justify-content: center;
  padding: var(--space-4) 0;
}
</style>

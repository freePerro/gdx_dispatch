// Help drawer store — owns the in-app help panel state.
//
// One drawer per app; opening to a specific articleId scrolls there.
// Opening with no articleId surfaces the search + featured-articles view.
//
// Article content + search index are loaded once on first open from
// /help-index.json (built by scripts/build_help_index.mjs). The index
// is a JSON blob shipped with the SPA — zero backend load for help search.

import { defineStore } from 'pinia';
import MiniSearch from 'minisearch';
import markdownIt from 'markdown-it';
import DOMPurify from 'dompurify';

const md = markdownIt({ html: false, linkify: true, breaks: false });

let _searchEngine = null;
let _indexLoaded = false;
let _indexLoadPromise = null;

async function loadIndex() {
  if (_indexLoaded) return;
  if (_indexLoadPromise) return _indexLoadPromise;
  _indexLoadPromise = fetch('/help-index.json', { credentials: 'omit' })
    .then((r) => {
      if (!r.ok) throw new Error(`help-index.json fetch failed: ${r.status}`);
      return r.json();
    })
    .then((blob) => {
      _searchEngine = new MiniSearch({
        fields: ['title', 'tags', 'body'],
        storeFields: ['slug', 'title', 'role', 'tags', 'module'],
        searchOptions: { boost: { title: 3, tags: 2 }, fuzzy: 0.2, prefix: true },
      });
      _searchEngine.addAll(blob.articles.map((a, i) => ({ id: i, ...a })));
      _indexLoaded = true;
      return blob;
    });
  return _indexLoadPromise;
}

export const useHelpStore = defineStore('help', {
  state: () => ({
    isOpen: false,
    currentSlug: null, // null = search/landing view
    searchQuery: '',
    searchResults: [],
    articles: [], // all articles, for landing-page featured list + slug lookup
    loading: false,
    error: null,
  }),

  getters: {
    currentArticle(state) {
      if (!state.currentSlug) return null;
      return state.articles.find((a) => a.slug === state.currentSlug) || null;
    },
    renderedHtml(state) {
      const a = state.currentSlug
        ? state.articles.find((x) => x.slug === state.currentSlug)
        : null;
      if (!a || !a.body) return '';
      return DOMPurify.sanitize(md.render(a.body));
    },
  },

  actions: {
    async ensureLoaded() {
      if (_indexLoaded) {
        if (this.articles.length === 0) {
          // Index already loaded by another store instance — copy refs.
          const blob = await fetch('/help-index.json').then((r) => r.json());
          this.articles = blob.articles;
        }
        return;
      }
      this.loading = true;
      this.error = null;
      try {
        const blob = await loadIndex();
        this.articles = blob.articles;
      } catch (e) {
        this.error = String(e?.message || e);
      } finally {
        this.loading = false;
      }
    },

    async open(articleSlug = null) {
      await this.ensureLoaded();
      this.currentSlug = articleSlug;
      this.searchQuery = '';
      this.searchResults = [];
      this.isOpen = true;
      this._trackEvent('help_opened', { slug: articleSlug, source: articleSlug ? 'direct' : 'browse' });
    },

    close() {
      this.isOpen = false;
    },

    showArticle(slug, source = 'related') {
      this.currentSlug = slug;
      this.searchQuery = '';
      this.searchResults = [];
      this._trackEvent('help_article_viewed', { slug, source });
    },

    backToSearch() {
      this.currentSlug = null;
    },

    search(query) {
      this.searchQuery = query;
      if (!_searchEngine || !query || query.trim().length < 2) {
        this.searchResults = [];
        return;
      }
      this.searchResults = _searchEngine.search(query.trim()).slice(0, 12);
    },

    _trackEvent(name, payload) {
      // Slim wrapper — analytics.js will replace this in Slice 6.
      try {
        window.dispatchEvent(new CustomEvent('gdx:analytics', { detail: { name, payload } }));
      } catch (_e) {
        /* swallow */
      }
    },
  },
});

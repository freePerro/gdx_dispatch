// A PrimeVue <Select filter> whose options come from the server as the user
// types, instead of one capped list fetch filtered in the browser.
//
// The capped shape (`/api/jobs?page_size=200` + `filter`) silently made every
// row past the cap unpickable: the user types a job number that exists and is
// told "No results found" (GDXA-1965). Here each keystroke (debounced) asks the
// server, and the row currently selected is pinned into the options so its
// label still renders when it is not in the latest result page.
//
// Wire it as:
//   <Select v-model="id" :options="picker.options.value" optionLabel="label"
//     optionValue="value" filter :filterFields="['searchText']"
//     resetFilterOnHide :loading="picker.loading.value"
//     @filter="picker.onFilter" @hide="picker.onHide" />
//   watch(() => id, (v) => picker.ensure(v), { immediate: true })
//
// `toOption(row)` must return { value, label, searchText }. Select still runs
// its own client-side "contains" filter over the options. Each row the server
// returned carries the exact text that fetched it in front of its searchText,
// so Select never hides a server match (a customer found by a phone typed as
// "555-123-4567" against a stored "(555) 123-4567", a job found by its
// customer's name). The row's own fields narrow instantly while the next
// keystroke's request is out.
import { computed, ref } from 'vue';

export function useServerSearchSelect({ search, fetchOne = null, toOption, delay = 250 }) {
  const results = ref([]);
  const pinned = ref(null);
  const loading = ref(false);
  let timer = null;
  let seq = 0;
  let pinSeq = 0;
  let lastQuery = null;
  // The untrimmed filter text behind `results` — what Select filters on.
  const resultsTyped = ref('');

  async function run(query, typed = query) {
    const mine = ++seq;
    lastQuery = query;
    loading.value = true;
    try {
      const rows = await search(query);
      if (mine === seq) {
        results.value = Array.isArray(rows) ? rows : [];
        resultsTyped.value = typed;
      }
    } catch {
      // The api composable already toasted; an empty list is the honest state.
      if (mine === seq) {
        results.value = [];
        resultsTyped.value = typed;
      }
    } finally {
      if (mine === seq) loading.value = false;
    }
  }

  /** Load the unfiltered first page (the list shown before anything is typed). */
  function load() {
    clearTimeout(timer);
    timer = null;
    return run('');
  }

  function onFilter(event) {
    const typed = String(event?.value ?? '');
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      run(typed.trim(), typed);
    }, delay);
  }

  /**
   * resetFilterOnHide clears the box on close; put the list back to match it.
   * On hide, not on show: Select emits `show` only after its open animation,
   * so a reload there wiped a search typed before the animation ended.
   */
  function onHide() {
    // A search typed and closed inside the debounce would land after the box
    // was cleared; load() cancels it.
    if (timer !== null || lastQuery !== '') load();
  }

  function find(id) {
    if (id === null || id === undefined || id === '') return null;
    const key = String(id);
    if (pinned.value && String(pinned.value.id) === key) return pinned.value;
    return results.value.find((row) => String(row.id) === key) || null;
  }

  /** Keep the row behind `id` listed, fetching it when no page holds it. */
  async function ensure(id) {
    const mine = ++pinSeq;
    if (id === null || id === undefined || id === '') {
      pinned.value = null;
      return;
    }
    const known = find(id);
    if (known) {
      pinned.value = known;
      return;
    }
    let row = null;
    if (fetchOne) {
      try {
        row = await fetchOne(id);
      } catch {
        row = null;
      }
    }
    // The value moved on while this fetch was out; the newer call pins.
    if (mine !== pinSeq) return;
    // A row the server will not return (deleted, no access) still keeps its
    // value selected; toOption's fallback label names it by id.
    pinned.value = row && row.id !== undefined ? row : { id };
  }

  const options = computed(() => {
    const seen = new Set();
    const out = [];
    const tag = resultsTyped.value;
    for (const row of [pinned.value, ...results.value]) {
      if (!row) continue;
      let option = toOption(row);
      if (!option) continue;
      if (tag && row !== pinned.value) option = { ...option, searchText: `${tag} ${option.searchText || ''}` };
      const key = String(option.value);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(option);
    }
    return out;
  });

  return { options, loading, load, onFilter, onHide, ensure, find };
}

# GDX Frontend View Pattern — Canonical Reference

Every view in this app shares a layout shell. Phone.com views were the
canonical example of *not* doing this — bare `<table>`, custom CSS,
no sidebar, no breadcrumb. an earlier session fixed them. Don't regress.

**Reference implementation:** [`gdx_dispatch/frontend/src/views/_ViewTemplate.vue`](../frontend/src/views/_ViewTemplate.vue) — a working list-with-detail-dialog view you can copy.
**Live examples:** `CustomersView.vue` (the gold standard), `JobsView.vue`, `BillingView.vue`, `PhoneComCallsView.vue`.

---

## The shell: every view starts with this

```vue
<template>
  <AppLayout>
    <section class="my-feature view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">My Feature</h1>
        </template>
        <template #end>
          <!-- filters, primary actions -->
          <Button label="Refresh" icon="pi pi-refresh" severity="secondary" @click="fetch" />
        </template>
      </Toolbar>

      <!-- error banner / spinner / content -->
    </section>
  </AppLayout>
</template>
```

`AppLayout` provides the sidebar + topbar + globally-mounted `<ConfirmDialog/>`. Without it the view renders full-bleed, looks broken, and Doug calls it out (correctly). The `view-card`, `view-heading`, and `view-heading-row` classes are global (defined in `assets/base.css`); don't redeclare them.

**Breadcrumbs were removed sitewide 2026-05-03.** Detail views must include their own back button — see the `view-heading-row` pattern (back button + h1.view-heading) used by AuditLogViewer, BillingUsage, FederationProviders, etc.

---

## Component choices

| Need | Use | NOT |
|---|---|---|
| Page wrapper | `<AppLayout>` | bare `<div>` |
| Header bar | PrimeVue `Toolbar` with `#start`/`#end` slots | custom `<div class="header">` |
| Tabular list | PrimeVue `DataTable` + `Column` | `<table><tr>` |
| Modal | PrimeVue `Dialog` | custom overlay `<div>` |
| Status pill | PrimeVue `Tag` with `severity` prop | custom `<span class="badge">` |
| Form input | PrimeVue `InputText` / `Textarea` / `Select` | `<input>` / `<select>` |
| Button | PrimeVue `Button` (label + icon) | `<button>` |
| Loading | PrimeVue `ProgressSpinner` | text "Loading…" |
| Card group (e.g. tile list) | PrimeVue `Card` with `#title` / `#content` slots | custom panel divs |

---

## Tag severity → status color (consistent across the app)

PrimeVue v4 vocabulary differs by component (verified from .d.ts in v4.5.5):
  - **Tag / Button:** `secondary | success | info | warn | danger | contrast` (Button also: `help`)
  - **Toast / Message:** `success | info | warn | error | secondary | contrast`

Use `warn` (NOT `warning`) — `warning` is silently invalid and falls through to default styling. Use `danger` for Tag/Button and `error` for Toast/Message — they are different vocabularies. The `lint:ux` script in `gdx_dispatch/frontend/scripts/ux_gate.mjs` enforces this; run via `npm run lint:ux`.

| Status | Tag severity | Color |
|---|---|---|
| Voicemail / pending / queued | `warn` | amber |
| Forwarded / info / scheduled | `info` | blue |
| Answered / Completed / paid / success | `success` | green |
| Missed / Canceled / failed / overdue | `danger` | red |
| Inbound (direction) | `info` | blue |
| Outbound (direction) | `success` | green |
| Anything else | `secondary` | gray |

Map raw values to severity in a small `statusSeverity()` helper local to the view (or in a shared `utils/` module if reused — see `phoneComLabels.js`).

---

## CSS variables — use design tokens

Use `--p-*` PrimeVue design tokens, not hand-mixed colors:

| Need | Token |
|---|---|
| Card / dialog / panel surface | `var(--p-content-background)` |
| Body text | `var(--p-text-color)` |
| Muted text | `var(--p-text-muted-color)` |
| Subtle border | `var(--p-content-border-color)` |
| Hover background | `var(--p-content-hover-background)` |
| Hover text | `var(--p-content-hover-color)` |
| Selected / active highlight | `var(--p-highlight-background)` |
| Selected / active text | `var(--p-highlight-color)` |
| Primary action | `var(--p-primary-color)` |
| Error background | `var(--p-red-50)` |
| Error text | `var(--p-red-700)` |
| Error border | `var(--p-red-200)` |
| Success background | `var(--p-green-50)` |

**Use semantic role tokens, not numbered surface tokens.** PrimeVue v4's `--p-surface-50/-100/-200/-300` are *light gray* in BOTH themes — using them for hover or surface backgrounds inverts contrast in dark mode (Doug 2026-05-10: the Re-open/Warranty popup rendered white-on-white). Always reach for the role token first (`--p-content-background`, `--p-content-hover-background`, `--p-highlight-background`).

Custom variables like `--surface-100`, `--primary-50`, `--red-500`, `--text-color-secondary` (the pre-v4 PrimeVue names) and hard-coded hex fallbacks like `var(--surface-0, #fff)` are anti-patterns. The hex fallback ignores theme entirely. They produce the visual drift Doug flagged 2026-04-28 and the dark-mode breakage 2026-05-10. The codemod at `gdx_dispatch/frontend/scripts/migrate_legacy_tokens.py` handles the bulk migration; running it twice should produce zero changes.

---

## Common patterns

### Empty state inside DataTable
```vue
<template #empty>
  <div class="empty-message">No items yet.</div>
</template>
```
And in scoped CSS:
```css
.empty-message {
  text-align: center;
  padding: 1.5rem;
  color: var(--p-text-muted-color);
}
```

### Error banner
```vue
<div v-if="error" class="error-banner">{{ error }}</div>
```
```css
.error-banner {
  background: var(--p-red-50);
  color: var(--p-red-700);
  border: 1px solid var(--p-red-200);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}
```

### Loading state
```vue
<div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>
```
```css
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem;
}
```

### Click-to-detail row
```vue
<DataTable
  :value="rows"
  rowClass="row-clickable"
  @row-click="(event) => openDetail(event.data)"
>
```
```css
.row-clickable { cursor: pointer; }
```

### Detail Dialog
```vue
<Dialog
  v-model:visible="detailVisible"
  modal
  header="Item detail"
  :style="{ width: '640px' }"
  @hide="closeDetail"
>
  <div v-if="detailLoading" class="spinner-wrap"><ProgressSpinner /></div>
  <div v-else-if="detail" class="detail-grid">
    <div class="detail-row"><span class="label">Field</span><span>{{ detail.field }}</span></div>
  </div>
</Dialog>
```

### Pagination row (when DataTable's built-in paginator isn't right)
```vue
<div v-if="total > perPage" class="pagination-row">
  <Button label="Prev" icon="pi pi-chevron-left" :disabled="page <= 1" severity="secondary" @click="page--" />
  <span class="text-muted">Page {{ page }} · {{ total }} total</span>
  <Button label="Next" icon="pi pi-chevron-right" iconPos="right" :disabled="page * perPage >= total" severity="secondary" @click="page++" />
</div>
```

---

## Imports — copy this block to start a new view

```js
import { ref, computed, onMounted, watch } from 'vue'
import AppLayout from '../components/AppLayout.vue'
import { useApi } from '../composables/useApi'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import ProgressSpinner from 'primevue/progressspinner'
// optional, when you need them:
// import Card from 'primevue/card'
// import Textarea from 'primevue/textarea'
// import Toast from 'primevue/toast'
// import { useToast } from 'primevue/usetoast'
```

---

## Don'ts

- ❌ Bare `<table>` / `<select>` / `<button>` — use PrimeVue.
- ❌ Custom modal with `position: fixed` overlay — use `Dialog`.
- ❌ Custom CSS variables — use `--p-*` tokens.
- ❌ Skipping `<AppLayout>` — view will render full-bleed without sidebar.
- ❌ `<span class="status-tag status-voicemail">…</span>` — use `<Tag :value="…" :severity="…" />`.
- ❌ Hand-rolled pagination components — use DataTable's built-in `:paginator="true"` when possible.
- ❌ Hex colors in `<style scoped>` — token or nothing.
- ❌ Native `confirm()` / `alert()` — use `useDestructiveConfirm` (Promise-based `confirmAsync`) and `useToast`. Lint gate (`npm run lint:ux`) catches new sites.
- ❌ Local `formatDate()` / `formatCurrency()` definitions — import from `composables/useFormatters` (`formatDate`, `formatDateTime`, `formatMoney`, `formatPercent`, `formatNumber`).
- ❌ Manual `toast.add({ severity: 'success', ... })` after a successful mutation — pass `{ successMessage: 'X saved' }` to `useApi.post/patch/put/del` instead. The composable fires the toast for you.
- ❌ `severity="warning"` (not in v4 vocab — use `warn`). `severity="error"` on Tag/Button (use `danger`). `severity="danger"` on Toast/Message (use `error`).

---

## When in doubt

Open `CustomersView.vue` and copy its shape. It's the most up-to-date canonical example. If your new view doesn't render visually consistent with `CustomersView`, the layout shell is wrong — fix the shell first, then the content.

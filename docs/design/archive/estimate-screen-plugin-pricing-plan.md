# Plan: price with a plugin directly from the estimate screen

**Status:** RELEASED v1.54.0 / v1.55.0 — all three phases on main (verified
2026-08-21). Phase 1: `EstimateView.vue:414` renders one button per source
with `activeSource` per invocation, plus the bundled error visibility at
`:742/1510/1572` (forbidden vs unavailable, no longer an empty state).
Phase 2: `:830` embeds `PluginScreen` in a dialog, write-gated via
`writableSources`. Phase 3: `PluginScreen.vue:222` re-emits the captured
payload and `EstimateView.vue:1595 insertDraft` inserts the line.
**Open:** the first real in-dialog capture against the live plugin.
**Ask (Doug):** "when doing an estimate we can use a plugin like the chiohd one right from the estimate screen."

## 0. What already exists (don't rebuild it)

ADR-013 already defines the `estimate_source` hook, and it is live:

- A plugin declares `ui.estimate_source = {label, list_endpoint, draft_endpoint}` in its manifest.
- `EstimateView.vue` discovers it (`_discoverEstimateSource()`, line ~1222), renders an
  "Add {label}" button (line ~394), opens a folder→multi-select picker over
  `list_endpoint`, and for each selection fetches `draft_endpoint` →
  `{label, description, cost, pricing_category, category, quantity, line_metadata, image}`.
- The draft's **cost** goes through `recomputeSell()` → core's margin engine sets the
  customer price; `line_metadata` persists write-once on the line POST; the door photo
  attaches as an estimate document.
- Both installed pricing plugins (chipricing, midland) implement this contract.

So "use the plugin from the estimate screen" is **already true for picking previously
captured/priced quotes**. Two real gaps remain:

**Gap A — single provider.** Discovery is `plugins.find(x => x?.ui?.estimate_source)`
(EstimateView.vue:1225). First match wins. With both chipricing and midland installed,
one of them is unreachable from the estimate screen, and there is only one button.

**Gap B — pricing happens elsewhere.** To price a *new* door you still navigate to
`/plugins/{key}`, run the capture/configure flow there, then come back to the estimate
and use the picker. The ask is to run that flow without leaving the estimate.

## 1. Scope

In scope:
1. Multiple `estimate_source` providers, one entry each, from the estimate screen.
2. Launching a plugin's own pricing/capture UI from the estimate screen, and getting the
   result into the open estimate with minimal clicks.
3. Error visibility when the plugin host is degraded (today's picker shows an empty list
   on 5xx — indistinguishable from "no captures yet").

Out of scope:
- Any change to the pricing contract (cost → margin engine stays authoritative).
- Any change to browser-stream authorization (owner + consent stays as is; see §5).
- Tier-linked lines (no tier↔line FK exists; unchanged).
- New-estimate flow: the hook stays gated on `isExisting` (the line POST and photo
  attach both need an estimate id). Pricing a door before first save is not supported.

## 2. Design

### Phase 1 — multi-provider discovery (small; ship first, alone)

Frontend only. No backend change, no migration.

- `_discoverEstimateSource()` → `_discoverEstimateSources()`: collect **all** plugins
  with `ui.estimate_source` into `estimateSources: [{pluginKey, label, list_endpoint,
  draft_endpoint}]` (carry the plugin `key` from the catalog row — needed for
  permission checks and for Phase 2).
- Filter client-side by `auth.hasPluginPermission(key, 'read')` so users don't see
  buttons that can only 403. (The proxy still enforces server-side; this is UX only.)
- Rendering: one source → exactly today's single button, same `data-testid`
  (`est-add-captured-btn`) so existing tests and muscle memory hold. Two or more →
  one button per source ("Add captured door", "Add Midland quote"), same style.
  No dropdown/menu until a third provider exists — two buttons is fewer clicks.
- Picker state (`capturedItems`, `capturedFolder`, `selectedDoors`) becomes
  per-invocation with an `activeSource` ref set on open; `openCapturedPicker(source)`
  already resets state on every open, so this is a parameter change, not a rework.
- `addSelectedDoors()` reads `activeSource` instead of the singleton.

### Phase 2 — in-context pricing ("Price with {plugin}…")

The plugin UI is host-rendered (declarative manifest → `PluginScreen.vue`; no plugin
JS ever ships to the browser — ADR-013). That makes embedding tractable: we are
embedding **our own component**, not third-party code.

- New button per source on the estimate line-item toolbar: "Price with {label}…",
  shown when the user holds `plugin.<key>.write` (capture/create endpoints are POSTs,
  which the proxy grades as `write`).
- Clicking opens a **maximized PrimeVue Dialog** hosting `PluginScreen` with
  `pluginKey` passed as a prop. **[audit]** No `embedded` mode flag: PluginScreen
  is bare Tabs with no page chrome of its own (verified — props-only, no route
  deps, the route wrapper supplies the header), and the Dialog brings its own
  header/close. Expect at most a CSS tweak, not a mode.
  - The estimate stays mounted underneath; its autosave watcher keeps running.
    Closing the dialog cannot lose estimate data — autosave already owns persistence.
  - **[audit]** Stream teardown is verified safe: `BrowserStream` connects
    `onMounted` and disconnects `onBeforeUnmount` (BrowserStream.vue:238-243),
    so dialog close closes the socket and the server-side browser.
  - **[audit] Gate before building:** each dialog open spawns a fresh
    server-side Chromium and each close kills it; the HubX login surviving that
    cycle depends on saved-session reload, which has never been live-verified
    against CHI's real login form. **Walk that on prod first.** If the session
    does not survive reconnects, Phase 2 ships for form-based plugins only
    (Midland-style configurators) and browser-screen plugins keep an
    "Open full page" link instead — otherwise the dialog turns pricing into a
    re-login loop and is worse than the status quo. This also keeps the
    plugin host (documented boot-hang history) out of the estimate hot path
    until it's proven.
- On dialog close: re-fetch the source's `list_endpoint` and open the existing
  picker, so the just-priced door is one click from becoming a line. This is
  deliberate: it reuses the proven picker path (photo attach, metadata, engine
  pricing) instead of inventing a second insertion path. **[audit]** Both
  plugins' list endpoints already return newest-first (`order_by(id.desc())` —
  midland router.py:115, chi router.py:163); no core sorting work exists.
- Route guard note: `/plugins/:key` permission checks live in the router guard, which
  an embedded dialog bypasses. That is acceptable because (a) the button itself is
  permission-gated and (b) every underlying request goes through the proxy, which
  enforces `plugin.<key>.read/write` server-side. The guard was always UX, not the
  security boundary.

### Phase 3 (optional polish, separate PR) — capture → auto-insert

**[audit]** The event already half-exists: `BrowserStream` emits `captured` WITH
the capture POST's JSON response (BrowserStream.vue:231), and `PluginScreen`
currently consumes it as `@captured="load"`, discarding the payload
(PluginScreen.vue:52). Phase 3 is therefore a re-emit upward, not new machinery:
`PluginScreen` forwards `captured` with the payload; `EstimateView` listens; if
the response carries an `id`, fetch `draft_endpoint` for it and insert the line
(refactor `addSelectedDoors`'s per-item body into `insertDraft(draft)` and
share it). Toast "Added to estimate — still in {plugin}; keep capturing or Done."

**[audit] Scoping rule:** do NOT generalize auto-insert to every create-form
POST. Midland's "Multipliers" tab is also a `create` form — auto-inserting a
multiplier edit as an estimate line would be nonsense. If create-based
auto-insert is wanted for configurator plugins, emit only when the screen's
`endpoint` equals the manifest's `estimate_source.list_endpoint` (and
`usePluginScreen.create()` must start returning the POST response — it
currently discards it). Otherwise keep auto-insert capture-only; the Phase 2
close-and-pick path is the universal fallback.

### Error visibility (bundled with Phase 1)

`openCapturedPicker`'s `catch { capturedItems = [] }` swallows plugin-host 503s
(stale-version fail-closed) and 403s (missing per-plugin grant) into "no captures."
Distinguish: on error, show an inline message in the picker ("Plugin unavailable
(503) — try Manage plugins → Restart" / "You don't have access to this plugin —
ask an owner to grant it in Roles & Permissions") instead of the empty state.

## 3. What we are NOT changing, and why it holds

- **Pricing authority.** Drafts return supplier **cost**; core's engine sets sell.
  A plugin returning retail would bypass margin snapshots — the contract stays as is
  and Phase 2/3 reuse the same insertion path, so no new pricing branch appears.
- **`line_metadata` is write-once at line POST.** All phases insert via the existing
  POST path, so provenance still lands. No PATCH support added.
- **Browser-stream authz.** The embedded workspace still requires owner role +
  recorded consent to mint a stream ticket. Embedding changes *where* the UI renders,
  not *who* may use it.

## 4. Traps this plan must respect (from code, this session)

1. **`_lineHasContent()` gate** (EstimateView ~2079): a line is not POSTed until it has
   a description AND `unit_price > 0`. A draft with `cost: null` (or a pricing config
   that yields 0) inserts a row that **silently never persists**. Phase 1 must add:
   after insert, if `unit_price <= 0`, toast a warning ("line added but needs a price
   before it saves"). This is a live latent issue in the current single-source flow too.
2. **Finalized estimates silently eat added lines — fix it, don't match it.**
   **[audit — verified, this is a live defect today]** The add-line toolbar is
   NOT gated on status (only Accept/Convert/Duplicate are, ~749-770), while
   `_scheduleFlush`/`_flushNow` silently early-return for FINALIZED statuses
   (EstimateView.vue:2153-2155, 2257-2258). Adding any line — free-form,
   catalog, or plugin — to an accepted/declined estimate renders, never
   persists, and vanishes on reload with no error. The server's
   `_ensure_editable()` 409 (estimates.py ~244) is never even reached.
   Phase 1 must hide/disable the whole line-item toolbar for finalized
   statuses. Add a vitest for it.
   **[audit round 2]** The flush skips the ENTIRE payload, so the same silent
   loss covered every autosaved header field (label, valid-until, jobsite,
   description, tax, discount, notes, hide-line-prices) — all locked in Phase 1
   alongside the lines. Pulling that thread also exposed a distinct sibling:
   `valid_until` was never in `EstimatePatchIn` at all, so editing Valid Until
   on ANY existing estimate (draft included) silently reverted on reload —
   fixed in Phase 1 (schema field + flush payload + regression test).
3. **Stale-plugin fail-closed**: a version-drifted plugin 503s on every route while
   still absent from the catalog — buttons disappear (catalog-driven) but a picker
   opened moments earlier can hit 503 mid-flight; the error state in §2 covers it.
4. **Mobile**: `browser`-type screens refuse to open below 768px by design
   (PluginScreen). The embedded dialog inherits that: on a phone, CHI's capture
   workspace shows the explanatory panel, not a broken stream. Midland's configurator
   screens are phone-usable. No special-casing; inherit the existing behavior.
5. **Permission drift between button and proxy**: `hasPluginPermission` mirrors the
   backend OR (blanket ∨ per-plugin). Do not gate on the per-plugin key alone —
   that hides buttons from admins holding only blanket keys (documented trap from
   PR #304).
6. **Public-repo hygiene**: all core changes stay generic — plugin behavior comes from
   manifests. No CHI/Midland-specific logic lands in core. (The names already appear
   in tracked docs/tests as manifest examples; that precedent stands.)

## 5. Explicit non-goal called out for Doug

Embedding does **not** make CHI capture available to non-owner staff. The HubX
workspace rides the browser stream, which is owner+consent gated (saved distributor
credentials live behind it). A salesperson with `plugin.chipricing.write` can open the
embedded dialog but not the stream. If staff should price CHI doors from estimates,
that's a separate authorization decision (e.g. relaxing `_gate_browser` to a
permission instead of role) — deliberately not bundled here.

## 6. Test plan

- **Vitest** (Phase 1): discovery collects N sources; 0 sources → no buttons; 1 source
  → legacy testid; 2 sources → 2 buttons; permission-filtered rendering (blanket key,
  per-plugin key, neither); picker uses the invoked source's endpoints; error states
  (403 vs 503 vs empty) render distinctly.
- **Vitest** (Phase 2): embedded PluginScreen receives pluginKey/embedded; close
  triggers list refetch + picker open; estimate form state untouched across
  open/close.
- **Backend**: no changes in Phases 1–2 ⇒ no new backend tests; Phase 3 has no
  backend change either (event is frontend-only).
- **Browser walk** (Doug's standing preference — headed, real account): on the demo
  or a throwaway container with the example plugin, since chipricing/midland aren't
  in CI. Add an `estimate_source` + a trivial create-form screen to
  `gdx-plugin-example` so the flow is walkable and testable without proprietary
  plugins — this is the only way CI ever exercises the picker end-to-end.
- Existing tests to keep green: `useTenantModules.plugins.spec.js`,
  `authPluginPermission.spec.js`, `test_plugins_proxy.py` (untouched surfaces).

## 7. Rollout

Phase 1 + error-visibility + the finalized-toolbar fix → one PR.
Phase 2 → second PR, **gated on the prod session-persistence walk** (§2 [audit]);
teardown-on-unmount is already verified, no open lifecycle question.
Phase 3 → third PR, optional, capture-only unless the scoping rule is built.
No migrations. Invisible in stock core (no plugins ⇒ no buttons), so safe for
the public repo and the demo.

# Making mobile work on all platforms

Status: **BUILT** — both adopted items are on main (verified 2026-08-21).
The adversarial review overturned two of the three proposals, so the shipped
scope is deliberately small: auth-form inputs raised to 1rem, and
`responsive.css:39` `.p-dialog { height: 100svh }` with a `vh` fallback — both
pinned by the guard test at
`assets/__tests__/primevue-cta-contrast.spec.js:185` ("mobile viewport units
and input zoom"). Item 1 (`viewport-fit=cover`) was **DROPPED** and item 4
(card-stacking the five wide tables) deferred as separate work — neither is a
gap. The "Found but NOT in scope" list below is reported-not-fixed by design.
**Never verified on iOS**, as this document says throughout.

The review overturned two of the three proposed changes. Both original readings
are kept below, struck through, because the *way* they were wrong is the useful
part: one would have shipped an iOS regression, the other was a no-op that would
have been marked done.

Prior work in this session fixed individual overflows at one viewport width in
one headless browser. That is not "mobile works". This plan is scoped by
research + measurement across device profiles.

## Method and its limits (state these first)

- Measured in Chromium device emulation at 360 / 375 / 390 / 412 / 430 / 844x390
  landscape / 768 tablet.
- **Playwright's WebKit is unsupported on this OS** (`ubuntu26.04-x64`), so
  iOS-ENGINE behaviour is NOT observed anywhere in this work. Every iOS claim
  below is documented-behaviour + static analysis, never observation.
- A real **Android** device path exists (Pixel 8 AVD, `androidTesting`) and is
  the one platform that can be empirically verified here.
- Demo data is thin and I ran as an owner, so every measured width is a **lower
  bound** and role-specific screens are unmeasured.

## Findings

### F1 — ~~add `viewport-fit=cover`~~ **WRONG — would break the bottom nav on every iPhone**

`index.html:5` is `width=device-width, initial-scale=1.0`. Per MDN, iOS reports
`env(safe-area-inset-*)` as **0 unless `viewport-fit=cover` is set**. The app
already has **15** `env(safe-area-inset-*)` declarations (`base.css:263`,
`MobileTimeclockView:454`, `MobileCustomersView:175`, …) written specifically to
clear the home indicator — every one of them is currently a no-op on iOS.

The app is `"display": "standalone"` (`manifest.webmanifest`) and is installed
on a phone. In standalone mode there is no browser chrome to absorb the gap, so
the bottom nav and the content padding above it sit under the home indicator.

**What the review found, verified:** the app has **17** `safe-area-inset-bottom`
declarations and **zero** for `-top`, `-left` or `-right`. `viewport-fit=cover`
does not "switch on padding we already wrote" — it *removes the browser's
automatic letterboxing* and hands you all four insets to handle yourself.

The thing this was supposed to fix is the thing it breaks:

- `AppBottomNav.vue` — `.bottom-nav { position: fixed; bottom: 0; height: … }`
  with **no `env()` at all**. Today `bottom: 0` sits at the top of the
  home-indicator strip *because* iOS insets the viewport, i.e. it is correct
  today. Add `cover` and the bar drops ~34px into the system gesture zone, which
  swallows the first tap on the lower half of every nav button.
- `.layout-header` is `position: sticky; top: 0` and no top inset exists
  anywhere → the topbar would go under the notch on every screen.
- the capture FAB and bug-report button both offset from the nav height → same
  drop.
- the toast fix shipped earlier pins `left/right: 0.75rem` → in landscape the
  44px notch inset would clip it.

The 15 existing declarations are also inconsistent (`calc(5rem + env(...))` in
some views, nothing on the shared `.layout-content`) precisely because they have
never once been active. Turning them all on at once is an **untested layout
change on a platform this environment cannot test at all**, not a fix.

**Correct scope, deferred:** `padding-bottom: env(...)` on `.bottom-nav` with
`height` → `min-height`, a top inset on the header, left/right on toast and nav,
and the `5rem` constants re-derived — then verified on a real iPhone. Severity
on Android: none (Chromium supplies non-zero insets regardless).

### F2 — ~~every input is 15px~~ **OVERSTATED — it is five hand-rolled auth forms**

Measured on all seven profiles, on `/login`: both fields compute to **15px**.
iOS Safari auto-zooms any focused input below 16px and does not zoom back out —
the user is left on a zoomed, horizontally-panning page from the first tap of
the first screen.

**Corrected:** that measurement was taken on `/login` only and generalised to
"every input", which was wrong. PrimeVue's `.p-inputtext` is already
`font-size: 1rem`, so the app's ordinary inputs were never at risk. The real
traps are the five hand-rolled auth forms, each with its own literal:
LoginView `0.9375rem`, SignupView `0.9rem`, Forgot/Reset `0.95rem`.

The originally proposed fix — a rule in `responsive.css` — **could not have
worked**: `input[type=…]` is specificity (0,1,1) and a scoped `.field input`
is (0,2,1), so it loses outright regardless of source order. It would have
shipped as a no-op and been marked done.

**Done:** each literal raised to `1rem`, with a test that parses the selectors
and fails on any rule targeting a real control below 1rem.

### F3 — fullscreen dialogs are taller than the visible viewport

`responsive.css:29` — `.p-dialog { height: 100vh }` with no `dvh` fallback.
`100vh` excludes mobile browser chrome, so the bottom of every fullscreen dialog
— where Save/Cancel live — is pushed below the fold. The codebase already uses
`dvh` correctly in 23 places with `100vh` as the fallback
(`AppLayout.vue:217-218`); this rule was missed.

Also `min-height: 100vh` on `LoginView`, `SignupView`, `ResetPasswordView`,
`ForgotPasswordView`, `OnboardingView`, `CustomerPortalView` — less severe
(min-height, so the page scrolls) but the same class.

### F4 — wide desktop tables (unchanged from the earlier audit)

`/jobs` 917px, `/billing` 885px, `/estimates` 735px, `/inventory` 595px,
`/reports` 554px at 390px wide. They scroll inside their own container rather
than breaking the page, and redirecting to mobile companions was already tried
and reverted — it removed capability and silently broke "Create Invoice".

### F5 — what is NOT broken

No horizontal document scroll on any route at any profile, including landscape.
All 13 purpose-built mobile screens are clean. The three "small tap targets"
found earlier are a text link, a hidden PrimeVue toggle input, and a styled file
input — not defects.

## Plan

Ordered by user impact per unit of risk.

1. ~~`viewport-fit=cover`~~ **DROPPED** — see F1. It is a multi-file safe-area
   implementation that must be verified on a real iPhone, not a one-line change.
2. **Auth inputs to 16px.** Done — the four hand-rolled forms, by literal.
3. **`svh`, not `dvh`, for the fullscreen dialog.** `dvh` re-evaluates as
   browser chrome hides/shows, resizing the dialog while the user types in it;
   `svh` is the smallest stable viewport. Two declarations, `vh` first, matching
   the house pattern. **Done.** Note no viewport unit responds to the software
   keyboard — a Save button under the keyboard needs `visualViewport` or
   `interactive-widget=resizes-content`, and remains unfixed.
4. **Leave F4 alone in this change.** Card-stacking five large views is a
   separate piece of work with its own verification; bundling it here would make
   this un-reviewable.

## Found but NOT in scope — reported, not fixed

These came out of the same review and are larger than a layout pass. Listed so
they are decisions rather than oversights:

- **The app has no offline capability.** `public/sw.js` registers **zero**
  `fetch` handlers and says caching is deliberately out of scope — yet
  `offlineDb.js`, `useOfflineSync.js` and `usePhotoQueue.js` all exist. A
  technician in a garage with no signal gets the browser's error page. The sync
  queue only helps if the tab is already open. Half-built.
- **The service worker is only registered from the push-subscription path**, so
  a user who declines notifications has no SW at all.
- **The dispatch board cannot be used on a touch device.** `DispatchView.vue`
  uses HTML5 `draggable`/`dragstart`/`drop` (6 occurrences, zero touch or
  pointer handlers), and those events do not fire on touch.
- **Software keyboard** covering focused inputs in dialogs — no `visualViewport`
  handling and no `interactive-widget` declaration anywhere.
- No `touch-action`, `-webkit-tap-highlight-color`, `overscroll-behavior`
  (pull-to-refresh can reload mid-form), or router `scrollBehavior`.

## Verification

- re-measure all seven profiles: no new overflow, zero sub-16px inputs, bottom
  nav clear of the safe area
- **real Android emulator** walk (the one platform that can be empirically
  verified here)
- explicitly NOT verified: **iOS Safari, at all.** Playwright's WebKit does not
  install on this OS. Every iOS statement in this document is documented
  behaviour plus static analysis. The iOS-specific fix (F2) should be treated as
  unproven until it is opened on a real iPhone.

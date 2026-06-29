# ADR-017 — Plugin install & dependency model (kill runtime pip on a no-egress host)

**Status:** Proposed (design only). Opened 2026-06-29 after the plugin-host prod outage. The
interim hardening (idempotent reconcile, degrade-don't-die, liveness/readiness split, fail-closed
stale plugins) ships separately; THIS ADR is the durable root-cause decision Doug deferred
("harden now, redesign later"). See **Options** — the tradeoffs are the load-bearing part.

## Context

`gdx-plugin-host-1` is a separate, **network-isolated** container (ADR-013 Model B) that pip-installs
plugins at runtime into a persistent `/plugins` volume from desired-state in Postgres
(`plugin_registry` packages + uploaded `plugin_artifact` wheels). `reconcile()` runs synchronously
at boot before uvicorn binds.

**The 2026-06-29 outage:** a deploy recreated the container; reconcile re-installed the chi-pricing
wheel, whose dependency `playwright` was not seen as already-vendored by `pip --target`, so pip
reached for PyPI — but the host has **no egress** — and hung on connection retries. Boot never
finished; the entire plugin surface went down.

The interim fix removed the hang and the silent failure. But it manages a **self-imposed
contradiction** the design can't escape: the host is asked to do a *network-dependent, non-
deterministic* operation (`pip install` + dependency resolution) on a host *deliberately given no
network*. Industry consensus is explicit that installing packages at container startup is an
anti-pattern (non-reproducible boots, startup latency, partial-state failures). Runtime pip into a
mutable volume also breaks immutable-infrastructure guarantees: two containers off the same image
can serve different code.

The design currently wants three things that can't all be true:
**(a) operators install plugins live with no image rebuild**, **(b) the host has no egress**, and
**(c) boots are deterministic**. Pick two.

## The decision in one line

Move plugin dependencies **off runtime pip on the serving container** — the network-needing install
step must happen somewhere that legitimately has network and produces an immutable, pre-resolved
artifact; the serving container only *loads* what's already present.

## Options

**Option A — Bake plugins into the image at build time.** Plugins become part of the plugin-host
image (or a thin overlay image per plugin set); `reconcile()` is deleted. *Pro:* fully deterministic,
immutable, no runtime pip, no egress needed ever. *Con:* installing/updating a plugin = build +
publish + redeploy the image — kills the "install live from the admin UI" UX that motivated ADR-013.
Best if live-install is not actually a hard requirement.

**Option B — Init container / build job does reconcile WITH network; serving container runs
read-only.** A short-lived sidecar (or the existing `app` container, which *does* have egress) runs
pip-install into the `/plugins` volume; the plugin-host then mounts it **read-only** and only
discovers/loads. *Pro:* keeps live-install, isolates the network step, serving boot can't hang on
pip. *Con:* orchestration complexity; the volume is still mutable shared state (just written by a
different, network-allowed actor); ordering/locking between writer and reader.

**Option C — Internal PyPI mirror / devpi on the isolated network.** Keep runtime pip, but point it
at a mirror reachable from the isolated network that holds every plugin + transitive dep (incl.
playwright). *Pro:* smallest change to today's flow; deterministic + offline-capable installs.
*Con:* you now run and curate a mirror; "no egress" becomes "egress to the mirror"; doesn't fix the
boot-blocking or mutable-volume concerns by itself (compose with the interim fail-fast/degrade work).

**Rejected — status quo + more try/except.** The interim hardening is the correct floor, but more
defensive code around runtime-pip-on-no-egress is lipstick: it cannot make a network-dependent step
succeed without network, it only makes the failure non-fatal.

## Recommendation (for Doug's decision)

Lean **B for the browser-heavy plugins (chi-pricing)** because their deps (playwright + Chromium) are
already baked into the plugin-host *image* — so in practice plugins should declare deps that are
**guaranteed present in the image** and reconcile should install with `--no-deps` (or not at all),
making the "live install" a code-only drop-in. If live-install of *arbitrary* third-party plugins
with *arbitrary* deps is a real requirement, **C** (internal mirror) is the only option that keeps it
honest. **A** if we decide live-install isn't worth the fragility.

## Known limitation the interim fix does NOT close (tracked here)

The interim "fail closed on stale" withholds a stale plugin's **live endpoints**
(`/api/plugins/<key>/*`). That fully covers a plugin whose money path is live —
chi-pricing captures door prices and builds estimate lines through those routes.
It does **not** cover an **ADR-015 pack** whose pricing *strategy* was copied into
a core `CustomCatalog` at create time: that catalog prices from the copied
`pricing_strategy`/`pricing_config` via `catalog._retail_for` entirely in core and
never calls the plugin-host, so withholding the route changes no computed price.

Closing this needs a core-side action when a pack is withheld/stale: flag or
invalidate catalogs sourced from it (e.g. stamp the source plugin+version on the
catalog at create time, then mark such catalogs degraded when the providing
plugin is withheld). Out of scope for the interim fix; folded into whichever
option below we adopt.

## What ships in the meantime (not this ADR)

Interim hardening already implemented (PR on `fix/plugin-reconcile-offline-idempotent`):
idempotent skip-if-installed (PEP 440), fail-fast pip (`--retries 0 --timeout 10` + wall-clock cap),
`build_app` degrade-don't-die, `/health` (liveness) vs `/ready` (readiness) split, and **fail-closed
withholding of stale plugins** so a wrong-version pricing plugin serves 503, never a stale quote.
That removes the outage class; this ADR removes the design that produced it.

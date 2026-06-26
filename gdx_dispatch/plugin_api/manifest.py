"""The plugin manifest — what a third-party module declares about itself.

Kept deliberately dependency-light (stdlib only): a plugin imports this to
describe its key, router, migrations and UI without dragging FastAPI/SQLAlchemy
into the declaration. That also lets discovery + compat be unit-tested on the
host with bare `python3`, no docker image. The router/UI it carries are typed
loosely (`Any`) for the same reason — the heavy types live in step 2.

See gdx_dispatch/docs/decisions/ADR-013-third-party-module-plugins.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Elevated capabilities a plugin may declare; each is consent-gated at install
# (ADR-014). Keep this the single source of truth — core + frontend read it.
KNOWN_PERMISSIONS = frozenset({"browser"})

# One-line human descriptions shown in the owner consent dialog. Every entry in
# KNOWN_PERMISSIONS must have a description here.
PERMISSION_RISKS = {
    "browser": (
        "Runs a real web browser on the server that this plugin controls, and "
        "lets you drive it from your screen. It can load external sites and use "
        "any login you complete in it. Only install plugins you trust."
    ),
}


@dataclass(frozen=True)
class PluginManifest:
    """One third-party module's self-description.

    A plugin package exposes an instance of this under the ``gdx.modules``
    entry-point group, e.g. in pyproject.toml::

        [project.entry-points."gdx.modules"]
        foo = "gdx_plugin_foo:manifest"

    Fields:
      key             stable module key (lowercase), e.g. "foo". Becomes the
                      company_module_grants key and the /api/plugins/<key> route.
      name            human label shown in the admin UI.
      tier            "starter" | "professional" | "business" (mirrors core MODULES).
      requires        host-version constraint, e.g. "gdx>=1.2". "" = any version.
      router          the plugin's FastAPI APIRouter (set in step 2; Any here so
                      this module stays import-light).
      migrations_path filesystem path to the plugin's Alembic version dir, or None.
      ui              declarative UI manifest (screens); schema lands in step 4.
      permissions     elevated capabilities the plugin needs, each gated by an
                      owner consent dialog at install (ADR-014). Currently only
                      "browser" — a streamed headless browser the operator drives
                      (e.g. to log into a no-API site). "" / () = no elevated
                      capability, installs silently like any core module.
      catalog_types   ADR-015 Catalog Pack — types this plugin contributes as
                      DATA, each {key, label, field_schema:[...],
                      pricing_strategy:{id,label,kind,params}}. The core catalog
                      surfaces them in the New Catalog dialog; creating one copies
                      its schema + pricing onto the catalog, so the type is
                      self-contained and needs no pack code at run time.
      pricing_strategies declarative pricing strategies ({id,label,kind,params})
                      a pack registers, e.g. duration-rounded labor. Code-free
                      (kind ∈ manual|multiplier|markup|margin) so they evaluate in
                      the core process without importing the pack (ADR-013).
      importers       reserved (ADR-015 Slice 3) — data-source importers a pack
                      offers, each {id, label}. Surfaced for discovery; wiring is
                      a later step.
    """

    key: str
    name: str
    tier: str = "professional"
    requires: str = ""
    router: Any = None
    migrations_path: str | None = None
    ui: Any = None
    permissions: tuple[str, ...] = ()
    catalog_types: tuple = ()
    pricing_strategies: tuple = ()
    importers: tuple = ()

    def __post_init__(self) -> None:
        # Fail loud at declaration time — a malformed key would otherwise surface
        # as a confusing 404 or a bad grant row much later.
        if not self.key or not self.key.strip():
            raise ValueError("PluginManifest.key must be non-empty")
        if self.key != self.key.strip().lower():
            raise ValueError(f"PluginManifest.key must be lowercase/trimmed: {self.key!r}")
        if not self.name or not self.name.strip():
            raise ValueError("PluginManifest.name must be non-empty")
        if self.tier not in ("starter", "professional", "business"):
            raise ValueError(f"PluginManifest.tier invalid: {self.tier!r}")
        unknown = set(self.permissions) - KNOWN_PERMISSIONS
        if unknown:
            raise ValueError(f"PluginManifest.permissions unknown: {sorted(unknown)}")
        # ADR-015 — minimal shape checks so a malformed pack fails at declaration,
        # not at New Catalog time. Kept stdlib-only (no schema lib).
        for ct in self.catalog_types:
            if not isinstance(ct, dict) or not ct.get("key") or not ct.get("label"):
                raise ValueError(f"catalog_type needs key+label: {ct!r}")
            if not isinstance(ct.get("field_schema", []), list):
                raise ValueError(f"catalog_type.field_schema must be a list: {ct!r}")
        for ps in self.pricing_strategies:
            if not isinstance(ps, dict) or not ps.get("id") or not ps.get("kind"):
                raise ValueError(f"pricing_strategy needs id+kind: {ps!r}")

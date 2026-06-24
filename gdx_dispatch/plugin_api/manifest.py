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
    """

    key: str
    name: str
    tier: str = "professional"
    requires: str = ""
    router: Any = None
    migrations_path: str | None = None
    ui: Any = None

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

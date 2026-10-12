"""gdx.plugin_api — the public surface third-party modules bind to.

v0 (step 1, ADR-013): the manifest + discovery. Stdlib-only on purpose, so this
package imports cleanly and unit-tests on the host without FastAPI/SQLAlchemy.

Later steps add the heavier surface (PluginBase, request/auth context,
require_module re-export) in their own submodules so importing those — and only
those — is what pulls in the web/ORM stack.

A plugin imports nothing from gdx_dispatch outside this package. Its DB session
comes from ``plugin_api.context``: ``get_plugin_db`` in a route,
``plugin_session()`` in an event handler or schedule callable.
"""
from gdx_dispatch.plugin_api.discovery import (
    ENTRY_POINT_GROUP,
    discover_plugins,
    is_compatible,
    load_manifests,
)
from gdx_dispatch.plugin_api.manifest import PluginManifest

__all__ = [
    "PluginManifest",
    "discover_plugins",
    "load_manifests",
    "is_compatible",
    "ENTRY_POINT_GROUP",
]

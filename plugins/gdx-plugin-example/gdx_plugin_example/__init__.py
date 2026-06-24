"""Reference GDX plugin. Exports `manifest`, which pyproject.toml registers under
the `gdx.modules` entry-point group for the plugin-host to discover.

Importing this registers the plugin's models on PluginBase (via the models import
below), so the plugin-host migration phase can create its tables.
"""
from gdx_dispatch.plugin_api import PluginManifest

from gdx_plugin_example import models  # noqa: F401 — registers tables on PluginBase
from gdx_plugin_example.router import router
from gdx_plugin_example.ui import UI

manifest = PluginManifest(
    key="example",
    name="Example",
    tier="professional",
    # Intentionally unpinned so the reference plugin loads on any host version;
    # a real plugin would pin e.g. requires="gdx>=1.0".
    requires="",
    router=router,
    ui=UI,
)

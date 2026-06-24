"""ASGI entry for the plugin-host container.

Run with:  uvicorn gdx_dispatch.plugin_host.main:app --host 0.0.0.0 --port 8000

Boot sequence (ADR-013 Model B migration phase):
  1. discover plugins (imports each package → registers its models on PluginBase)
  2. create plugin tables on the shared DB
  3. build the app, mounting each plugin's router

To pick up a newly-installed plugin the container is restarted; the core app
keeps serving.

ponytail: step 2 uses PluginBase.metadata.create_all — idempotent and enough
while plugins have no schema *changes* to version. Upgrade path is a per-plugin
Alembic branch (ADR-013) once a plugin needs migrations, not just creation.
"""
from gdx_dispatch.core.database import engine
from gdx_dispatch.plugin_api.base import PluginBase
from gdx_dispatch.plugin_api.discovery import discover_plugins
from gdx_dispatch.plugin_host.app import create_plugin_host

# 1. discover (imports plugin packages, registering models on PluginBase)
_plugins = discover_plugins()

# 2. create plugin tables (idempotent)
PluginBase.metadata.create_all(bind=engine)

# 3. build the app with the already-discovered plugins
app = create_plugin_host(plugins=_plugins)

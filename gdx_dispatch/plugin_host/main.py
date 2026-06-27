"""ASGI entry for the plugin-host container.

Run with:  uvicorn gdx_dispatch.plugin_host.main:app --host 0.0.0.0 --port 8000

Boot sequence (ADR-013 Model B migration phase):
  1. discover plugins (imports each package → registers its models on PluginBase)
  2. create plugin tables on the shared DB
  3. build the app, mounting each plugin's router

To pick up a newly-installed plugin the container is restarted; the core app
keeps serving.

ponytail: step 2 uses PluginBase.metadata.create_all — idempotent and enough to
*create* tables; step 2b additively reconciles missing *columns* (create_all
never ALTERs). Data backfills / type changes / drops remain a per-plugin Alembic
branch (ADR-013) once a plugin needs real migrations.
"""
from gdx_dispatch.core.database import engine
from gdx_dispatch.plugin_api.base import PluginBase
from gdx_dispatch.plugin_api.discovery import discover_plugins
from gdx_dispatch.plugin_host.app import create_plugin_host
from gdx_dispatch.plugin_host.reconcile import reconcile
from gdx_dispatch.plugin_host.schema_reconcile import reconcile_plugin_columns

# 0. reconcile: pip-install any registry packages into the /plugins volume and
#    put it on sys.path (no-op when the registry is empty).
reconcile()

# 1. discover (imports plugin packages, registering models on PluginBase)
_plugins = discover_plugins()

# 2. create plugin tables (idempotent)
PluginBase.metadata.create_all(bind=engine)

# 2b. additively add any model column missing from an existing plugin table —
#     create_all only creates tables, never ALTERs them (the CHI `folder`
#     outage, 2026-06-26). Plugin-layer twin of core issue #41.
reconcile_plugin_columns(engine, PluginBase.metadata)

# 3. build the app with the already-discovered plugins
app = create_plugin_host(plugins=_plugins)

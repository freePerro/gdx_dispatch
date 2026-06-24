"""ASGI entry for the plugin-host container.

Run with:  uvicorn gdx_dispatch.plugin_host.main:app --host 0.0.0.0 --port 8000

Discovery happens once at import (when create_plugin_host runs). To pick up a
newly-installed plugin, the plugin-host container is restarted — the core app
keeps serving (ADR-013, Model B).
"""
from gdx_dispatch.plugin_host.app import create_plugin_host

app = create_plugin_host()

"""Event-log reference plugin. Exports `manifest`, registered under the
gdx.modules entry-point group (pyproject.toml) for the plugin-host to discover.

The smallest complete demonstration of the event platform (Sprint 2): declare
`events`, provide an `event_handler`, persist to a namespaced table, and show the
results in a declarative list screen. Consent-gated on the `events` permission.
"""
from gdx_dispatch.plugin_api import PluginManifest

from gdx_plugin_eventlog import models  # noqa: F401 — registers the table on PluginBase
from gdx_plugin_eventlog.handler import handle_event
from gdx_plugin_eventlog.router import router
from gdx_plugin_eventlog.ui import UI

manifest = PluginManifest(
    key="eventlog",
    name="Event Log (example)",
    tier="starter",
    requires="",
    router=router,
    ui=UI,
    permissions=("events",),
    events=("*",),          # every business event
    event_handler=handle_event,
)

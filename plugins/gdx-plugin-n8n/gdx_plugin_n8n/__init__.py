"""n8n Automations plugin — the in-app console for the n8n flagship integration.

Exports `manifest`, registered under the gdx.modules entry-point group
(pyproject.toml) for the plugin-host to discover.

The WordPress-model integration surface for n8n: subscribe to every business
event (consent-gated on `events`), mirror them into a namespaced table so the
owner can watch the live stream their workflows trigger on, and render the setup
+ catalog + activity screens. Forwarding to n8n itself is the core webhook path's
job (plugin-host is network-isolated from n8n) — this console wires and monitors
it, the workflow canvas lives in the customer's own n8n editor.
"""
from gdx_dispatch.plugin_api import PluginManifest

from gdx_plugin_n8n import models  # noqa: F401 — registers the table on PluginBase
from gdx_plugin_n8n.handler import handle_event
from gdx_plugin_n8n.router import router
from gdx_plugin_n8n.ui import UI

manifest = PluginManifest(
    key="n8n",
    name="n8n Automations",
    tier="starter",
    requires="",
    router=router,
    ui=UI,
    permissions=("events",),
    events=("*",),          # every business event — the owner consents at install
    event_handler=handle_event,
)

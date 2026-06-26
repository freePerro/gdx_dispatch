"""Reference Catalog Pack (ADR-015 Slice 3).

Shows the smallest possible pack: pure DATA, no router/models/UI. It contributes
one catalog type ("HVAC Units") with its own field schema and a declarative
pricing strategy (markup 40%). When an operator picks this type in New Catalog,
the core copies the schema + pricing onto the catalog, so nothing from this
package runs in the core process at pricing time — only the plugin-host serves
the manifest data (ADR-013 process isolation).
"""
from gdx_dispatch.plugin_api import PluginManifest

HVAC_TYPE = {
    "key": "hvac_unit",
    "label": "HVAC Units",
    "field_schema": [
        {"name": "tonnage", "label": "Tonnage", "type": "number"},
        {"name": "seer", "label": "SEER Rating", "type": "number"},
        {"name": "refrigerant", "label": "Refrigerant", "type": "select",
         "options": ["R-410A", "R-32"]},
        {"name": "energy_star", "label": "Energy Star", "type": "checkbox"},
    ],
    # Declarative pricing — interpreted by core's strategy evaluator, no code.
    "pricing_strategy": {
        "id": "hvac_markup_40",
        "label": "HVAC Markup 40%",
        "kind": "markup",
        "params": {"pct": 0.4},
    },
}

manifest = PluginManifest(
    key="hvac",
    name="HVAC Catalog Pack",
    tier="professional",
    requires="",
    catalog_types=(HVAC_TYPE,),
    pricing_strategies=(HVAC_TYPE["pricing_strategy"],),
)

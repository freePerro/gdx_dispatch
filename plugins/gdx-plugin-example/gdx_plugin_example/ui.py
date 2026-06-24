"""The example plugin's UI manifest (ADR-013 step 4, schema v0).

No JavaScript — the host renders these declarative screens with its own PrimeVue
components. v0 vocabulary:

  screen.type "list"   — a table over `endpoint`, showing `columns`, with an
                         optional inline `create` form (POSTs to create.endpoint).
  column               — {field, label}
  create.field         — {name, label, type, required}

The host fetches this from GET /api/plugins/example/ui (via the proxy).
"""

UI = {
    "screens": [
        {
            "type": "list",
            "title": "Example Items",
            "endpoint": "/api/plugins/example/items",
            "columns": [
                {"field": "id", "label": "ID"},
                {"field": "name", "label": "Name"},
            ],
            "create": {
                "endpoint": "/api/plugins/example/items",
                "fields": [
                    {"name": "name", "label": "Name", "type": "text", "required": True},
                ],
            },
        }
    ]
}

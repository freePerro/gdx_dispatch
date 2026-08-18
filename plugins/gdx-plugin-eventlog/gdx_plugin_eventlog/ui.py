"""Declarative UI (host-rendered, no plugin JS — ADR-013). A list screen showing
recorded events, and a help screen explaining what the plugin proves."""

UI = {
    # Sidebar entry polish (both optional): `icon` is a single PrimeIcons pair
    # shown instead of the generic box; `category` (not set here) is a core nav
    # category key ("operations", "customers", "sales", "invoicing", ...) the
    # entry joins instead of the Plugins group — unknown keys fall back there,
    # and "admin"/"experimental" are reserved. Malformed values are ignored
    # with a warning, never fatal.
    "icon": "pi pi-history",
    "screens": [
        {
            "type": "list",
            "endpoint": "/api/plugins/eventlog/events",
            "columns": [
                {"field": "event", "label": "Event"},
                {"field": "received_at", "label": "Received"},
                {"field": "delivery_id", "label": "Delivery ID"},
            ],
        },
        {
            "type": "help",
            "sections": [
                {
                    "heading": "What this is",
                    "body": [
                        "A reference plugin that proves the GDX event platform.",
                        "It subscribes to every business event and records each one below.",
                        "- Consent to its 'events' permission at install to turn it on.",
                        "- Then accept an estimate, mark an invoice paid, or create a job —",
                        "  the event appears here within seconds.",
                    ],
                },
                {
                    "heading": "For plugin authors",
                    "body": [
                        "The whole integration is a manifest with events + an event_handler,",
                        "a table, and a list screen. Copy this package to build your own.",
                    ],
                },
            ],
        },
    ]
}

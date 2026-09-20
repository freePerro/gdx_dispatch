"""Declarative UI (host-rendered, no plugin JS — ADR-013).

Two list screens (search is server-side, wired to the endpoints' ?q=), an
upload screen for the SMS Backup & Restore backfill, and a help screen with
the phone-side setup. `category: "customers"` joins the same nav group the
Phone.com surfaces live in; an older host without that key falls back to the
Plugins group, and one without the `upload` screen type simply doesn't render
that screen (v-if chain) — nothing breaks."""

UI = {
    "icon": "pi pi-mobile",
    "category": "customers",
    "screens": [
        {
            "type": "list",
            "title": "Texts",
            "endpoint": "/api/plugins/cellcomms/messages",
            "search": {"param": "q", "placeholder": "Search text, number or customer"},
            "columns": [
                {"field": "when", "label": "When"},
                {"field": "direction", "label": ""},
                {"field": "number", "label": "Number"},
                {"field": "customer", "label": "Customer"},
                {"field": "body", "label": "Message"},
            ],
        },
        {
            "type": "list",
            "title": "Calls",
            "endpoint": "/api/plugins/cellcomms/calls",
            "search": {"param": "q", "placeholder": "Search number, contact or customer"},
            "columns": [
                {"field": "when", "label": "When"},
                {"field": "type", "label": "Type"},
                {"field": "number", "label": "Number"},
                {"field": "customer", "label": "Customer"},
                {"field": "duration", "label": "Duration"},
            ],
        },
        {
            "type": "upload",
            "title": "Import phone backup",
            "endpoint": "/api/plugins/cellcomms/backfill",
            "accept": ".xml",
            "button": "Import backup file",
            "help": [
                "Imports an SMS Backup & Restore XML file (sms-*.xml or calls-*.xml) —",
                "full history, both directions. Safe to re-upload: rows already",
                "imported (or already captured live) are skipped, not duplicated.",
            ],
            # Changed a customer's phone number? Re-run matching by hand —
            # only customer CREATION re-links automatically today.
            "secondary_action": {
                "label": "Re-match customers now",
                "endpoint": "/api/plugins/cellcomms/rematch",
            },
        },
        {
            "type": "help",
            "sections": [
                {
                    "heading": "What this is",
                    "body": [
                        "Texts and calls from the personal Android cell, alongside the",
                        "Phone.com business line. Two feeds fill these screens:",
                        "- LIVE: the phone forwards each incoming text and call the moment",
                        "  it happens (android-nomad-gateway, setup below).",
                        "- BACKFILL: upload an SMS Backup & Restore XML file for outgoing",
                        "  messages and history from before the live feed existed.",
                        "Numbers are matched to customers automatically; when a customer is",
                        "created, their earlier unmatched texts and calls re-link on the spot.",
                    ],
                },
                {
                    "heading": "Phone setup (one time)",
                    "body": [
                        "1. Install android-nomad-gateway (APK from its GitHub releases page).",
                        "2. Add an SMS rule and a Call rule, both POSTing to",
                        "   https://<your GDX domain>/api/cell-gateway/webhook",
                        "3. Set header X-GDX-Cell-Secret to the value of CELL_GATEWAY_WEBHOOK_SECRET.",
                        "4. Templates (exact field names matter):",
                        "   SMS:  {\"kind\":\"sms\",\"from\":\"%from%\",\"text\":\"%text%\",",
                        "          \"sentStamp\":\"%sentStamp%\",\"receivedStamp\":\"%receivedStamp%\",\"sim\":\"%sim%\"}",
                        "   Call: {\"kind\":\"call\",\"from\":\"%from%\",\"contact\":\"%contact%\",",
                        "          \"timestamp\":\"%timestamp%\",\"duration\":\"%duration%\"}",
                        "5. Allow the app to autostart and ignore battery optimization,",
                        "   or Android will kill the forwarder overnight.",
                        "The live feed is INCOMING only — outgoing texts and calls arrive",
                        "via the backup import on the Import screen.",
                    ],
                },
            ],
        },
    ],
}

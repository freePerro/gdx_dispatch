"""Declarative UI for the n8n Automations console (host-rendered, no plugin JS).

Four tabs, all on proven screen types:
  Activity        — live events this tenant's workflows can trigger on (list)
  Available Events— the catalog of GDX events (list)
  Connect         — dynamic setup facts a static help screen can't show (list)
  Setup           — the narrative guide (help)

There is deliberately no embedded n8n canvas: n8n's editor stays on the
customer's own instance (its Sustainable Use License covers self-hosting;
embedding the editor in our product would need n8n's commercial Embed license),
and the plugin-host is network-isolated from n8n. The workflow canvas lives in
n8n; wiring, discovery and monitoring live here.
"""

UI = {
    "screens": [
        {
            "type": "list",
            "title": "Activity",
            "endpoint": "/api/plugins/n8n/events",
            "columns": [
                {"field": "event", "label": "Event"},
                {"field": "received_at", "label": "Received (UTC)"},
                {"field": "detail", "label": "Detail"},
            ],
        },
        {
            "type": "list",
            "title": "Available Events",
            "endpoint": "/api/plugins/n8n/catalog",
            "columns": [
                {"field": "event", "label": "Event"},
                {"field": "fires_when", "label": "Fires when"},
            ],
        },
        {
            "type": "list",
            "title": "Connect",
            "endpoint": "/api/plugins/n8n/connect",
            "columns": [
                {"field": "setting", "label": "Setting"},
                {"field": "value", "label": "Value"},
            ],
        },
        {
            "type": "help",
            "title": "Setup",
            "sections": [
                {
                    "heading": "What this is",
                    "body": [
                        "n8n is an automation tool your workflows run in — 'when X "
                        "happens in GDX, do Y'. You build and edit workflows in n8n's "
                        "own editor, on your own n8n instance.",
                        "This console is where you wire GDX up to it and watch the "
                        "event stream. It does NOT itself deliver to n8n: the actual "
                        "delivery is a GDX webhook (below). This console just shows "
                        "you the events GDX is emitting so you know what to build on.",
                    ],
                },
                {
                    "heading": "1. Open your n8n",
                    "body": [
                        "n8n runs as its own service on your server (isolated from "
                        "GDX's database for safety). The Connect tab shows its "
                        "address; open it in a new tab and sign in.",
                    ],
                },
                {
                    "heading": "2. Add a Webhook trigger in n8n",
                    "body": [
                        "- In n8n, create a workflow starting with a Webhook node.",
                        "- Copy the Webhook node's Production URL.",
                    ],
                },
                {
                    "heading": "3. Point GDX at it",
                    "body": [
                        "- In GDX, open Settings → Webhooks and add a webhook.",
                        "- Paste your n8n Webhook URL, set a signing secret, and pick "
                        "the events to send (see the Available Events tab).",
                        "- GDX signs and POSTs each matching event to n8n. Verify the "
                        "X-GDX-Signature header in your workflow if you want to reject "
                        "anything not from GDX.",
                        "- Whether n8n actually received a delivery — and any "
                        "retries or failures — shows in Settings → Webhooks, not "
                        "here.",
                    ],
                },
                {
                    "heading": "About the Activity & Available Events tabs",
                    "body": [
                        "- Activity is the live stream of business events GDX emitted "
                        "on your system — exactly what your workflows can trigger on. "
                        "It is what GDX SENT, which is not the same as what n8n "
                        "received (that depends on your webhook above).",
                        "- Available Events is the catalog of events an automation "
                        "can subscribe to. Not every event emits in every release "
                        "yet — the Activity tab is the source of truth for what's "
                        "actually firing on your system today.",
                    ],
                },
                {
                    "heading": "4. Watch it work",
                    "body": [
                        "- Trigger an event (create a customer, mark an invoice paid).",
                        "- It appears on the Activity tab here within seconds — proof "
                        "GDX emitted it. If your webhook is configured, that same "
                        "event was also POSTed to n8n.",
                    ],
                },
                {
                    "heading": "For plugin authors",
                    "body": [
                        "This whole console is a manifest (events + a handler + list "
                        "and help screens) on the public plugin API — the WordPress "
                        "model. Copy this package to build your own integration.",
                    ],
                },
            ],
        },
    ]
}

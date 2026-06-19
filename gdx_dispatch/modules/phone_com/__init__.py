"""Phone.com per-tenant Voice + SMS integration.

Mirrors the QuickBooks module layout (client / oauth / router / sync /
tasks / webhook_router) and the LLM key_storage pattern (Fernet-
encrypted token in control-plane ``tenant_settings``).

Phase 1 (close gate): paste-permanent-token + webhooks + 5-min poll
backstop + core read/write (calls, messages, voicemails, dashboard).

Phase 2: OAuth convenience flow alongside paste-token.

Per-tenant constraint: Phone.com SMS is **P2P only**. A2P / marketing /
mass-text usage will get the sender number carrier-blocked. UI must
make this explicit; bulk-SMS is gated to a separate provider behind
its own module key.
"""

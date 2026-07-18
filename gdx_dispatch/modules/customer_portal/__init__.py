# Cookie-based router/auth removed per ADR-018 — the JWT flow in
# gdx_dispatch/routers/portal.py is the only portal implementation.
from gdx_dispatch.modules.customer_portal import models

__all__ = ["models"]

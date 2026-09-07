# router.py left 2026-09-07: its last route, GET /api/campaigns/{id}/stats, read columns
# the real marketing_campaigns table does not have and had no SPA caller.
from gdx_dispatch.modules.campaigns import models, service, tasks

__all__ = ["models", "service", "tasks"]

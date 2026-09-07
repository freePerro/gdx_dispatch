# router.py left 2026-09-07: all four of its routes were unauthenticated
# (`require_module` gates on the tenant module list, it does not authenticate)
# and none of them worked — the `parts` catalog has no writer anywhere.
# `models` and `service` stay exactly as they were; do NOT add `stock` here,
# it drags models.tenant_models -> models/__init__ -> celery into scope for
# anything that imports this package. Its three consumers already import the
# submodule directly.
from gdx_dispatch.modules.inventory import models, service

__all__ = ["models", "service"]

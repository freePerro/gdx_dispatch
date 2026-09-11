# router.py left 2026-09-07: all four of its routes were unauthenticated
# (`require_module` gates on the tenant module list, it does not authenticate)
# and none of them worked — the `parts` catalog has no writer anywhere.
# service.py followed it 2026-09-10 (#654): `deduct_stock` and
# `check_low_stock_alerts` had lost their only production caller, and both
# worked on that same writerless `parts` table. `models` stays; do NOT add
# `stock` here, it drags models.tenant_models -> models/__init__ -> celery into
# scope for anything that imports this package. Its three consumers already
# import the submodule directly.
from gdx_dispatch.modules.inventory import models

__all__ = ["models"]

from __future__ import annotations

from fastapi import APIRouter, Depends

from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

# This router now serves no routes. It stays mounted because `app.py` imports
# `modules.fleet.router` (and `modules/fleet/__init__.py` imports it eagerly);
# deleting the module would turn that into the swallowed-import path that
# substitutes an empty router anyway, only noisily.
#
# GET/POST /fleet/vehicles left this module 2026-09-06 (#569): routers/fleet.py registers
# them first, so FastAPI never dispatched here.
# Its maintenance copy left 2026-09-10 (#637): GET /fleet/vehicles/{id}/service-history,
# POST /fleet/vehicles/{id}/service and GET /fleet/due-maintenance read `vehicles`, a
# table nothing writes. The Fleet page's vehicles live in `fleet_vehicles_router`, so every
# id it holds 404'd here. The kept copy is routers/fleet.py (service-log, due-for-service).
# PUT /fleet/vehicles/{id} left 2026-09-12 (#558, owner-ruled): the last of the set, and
# the same shape — no caller anywhere in the repo (FleetView.vue:232 sends `api.patch`),
# reading the unwritten `vehicles` table rather than the Fleet page's
# `fleet_vehicles_router`, so every id the UI holds would 404. Its non-odometer branch was
# the unaudited half; the audited live equivalent is routers/fleet.py:197 (PATCH,
# action="fleet_vehicle_updated"). Deleted rather than audited — auditing a route no user
# can reach buys nothing.
router = APIRouter(prefix="/api", tags=["fleet"], dependencies=[Depends(require_module("fleet")), Depends(get_current_user)])

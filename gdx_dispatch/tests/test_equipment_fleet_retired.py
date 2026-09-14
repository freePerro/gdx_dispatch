"""Customer Equipment and Fleet, retired 2026-09-14 (#683), stay retired.

Owner ruling on #683, superseding the 2026-09-10 keep-rulings on #651 (the
equipment service routes) and #637 (the fleet service-log routes). Neither
surface was ever used on prod (read-only, 2026-09-14): `customer_equipments`,
`equipment_assets`, `fleet_vehicles_router` and `vehicles` all 0 rows, and no
audit row for any equipment, vehicle or fleet action. The forms that fed them
could not have worked: Equipment create sent `brand/install_date` to a model
expecting `manufacturer/installation_date` and offered door/motor/remote types
the Postgres enum rejects; Fleet sent `plate_number/mileage` for
`license_plate/odometer`.

Absence is asserted against the mounted route table, never source text. The
models are deliberately KEPT and still registered: the tables exist, nothing
creates them but the ORM, and `core/gdpr.py` still reads equipment service
history. Vehicle inspections are a separate surface and stay served.
"""

from __future__ import annotations

import importlib.util
import os
from collections import Counter
from pathlib import Path

import pytest

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long-for-hs256-sha256-safety")


@pytest.fixture(scope="module")
def registrations() -> Counter:
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    counts: Counter = Counter()
    for full_path, route in iter_app_routes(app):
        for method in getattr(route, "methods", None) or ():
            counts[(method, full_path)] += 1
    return counts


@pytest.mark.parametrize("dotted", [
    "gdx_dispatch.modules.equipment.router",
    "gdx_dispatch.routers.equipment_tracking",
    "gdx_dispatch.routers.fleet",
    "gdx_dispatch.modules.fleet.router",
    "gdx_dispatch.modules.fleet.service",
])
def test_module_no_longer_exists(dotted: str) -> None:
    assert importlib.util.find_spec(dotted) is None, f"{dotted} is back"


REMOVED_PATHS = [
    ("GET", "/api/equipment"),
    ("POST", "/api/equipment"),
    ("GET", "/api/equipment/{equipment_id}"),
    ("PUT", "/api/equipment/{equipment_id}"),
    ("DELETE", "/api/equipment/{equipment_id}"),
    ("GET", "/api/customers/{customer_id}/equipment"),
    ("POST", "/api/customers/{customer_id}/equipment"),
    ("POST", "/api/equipment/{equipment_id}/service"),
    ("POST", "/api/jobs/{job_id}/equipment/{equipment_id}/service"),
    ("GET", "/api/equipment/{equipment_id}/service-history"),
    ("GET", "/portal/equipment"),
    ("GET", "/api/fleet/vehicles"),
    ("POST", "/api/fleet/vehicles"),
    ("PATCH", "/api/fleet/vehicles/{vehicle_id}"),
    ("PUT", "/api/fleet/vehicles/{vehicle_id}"),  # deleted earlier (#558); pinned here since its test left with Fleet
    ("DELETE", "/api/fleet/vehicles/{vehicle_id}"),
    ("GET", "/api/fleet/vehicles/{vehicle_id}/service-log"),
    ("POST", "/api/fleet/vehicles/{vehicle_id}/service-log"),
    ("GET", "/api/fleet/vehicles/due-for-service"),
]


@pytest.mark.parametrize(("method", "path"), REMOVED_PATHS)
def test_retired_route_is_not_registered(registrations: Counter, method: str, path: str) -> None:
    assert registrations[(method, path)] == 0, f"{method} {path} is registered again"


def test_neighbours_are_still_served(registrations: Counter) -> None:
    """Vehicle inspections share the word 'vehicle' and nothing else; the portal
    router lost one route, not its mount. A retirement that took either with it
    would pass every absence check above."""
    for method, path in [
        ("GET", "/api/vehicle-inspections"),
        ("POST", "/api/vehicle-inspections"),
        ("GET", "/portal/dashboard"),
        ("GET", "/portal/jobs"),
    ]:
        assert registrations[(method, path)] >= 1, f"{method} {path} stopped being served"


def test_the_module_key_is_retired() -> None:
    from gdx_dispatch.core.modules import LEGACY_MODULE_ALIASES, MODULES

    assert "equipment_tracking" not in MODULES
    assert "fleet" not in LEGACY_MODULE_ALIASES


def test_the_kept_models_are_still_registered() -> None:
    """The ruling keeps these tables' models. Checked in a fresh interpreter
    through the app's own registration path (gdx_dispatch.models), because this
    suite's conftest imports the equipment and fleet models itself and would
    mask a registration that production no longer performs."""
    import subprocess
    import sys

    script = (
        "import gdx_dispatch.models\n"
        "from gdx_dispatch.core.audit import TenantBase\n"
        "print(' '.join(sorted(TenantBase.metadata.tables)))\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                         env={**os.environ, "JWT_SECRET": os.environ["JWT_SECRET"]})
    assert out.returncode == 0, out.stderr[-2000:]
    tables = set(out.stdout.split())
    for name in ("customer_equipments", "equipment_service_history", "vehicles", "vehicle_service_records",
                 "equipment_assets", "fleet_vehicles_router", "fleet_vehicle_service_logs_router"):
        assert name in tables, f"{name} is no longer registered on the ORM"


def test_the_spa_no_longer_routes_or_links_the_pages() -> None:
    src = Path(__file__).resolve().parents[1] / "frontend" / "src"
    assert not (src / "views" / "EquipmentView.vue").exists()
    assert not (src / "views" / "FleetView.vue").exists()
    router = (src / "router" / "index.js").read_text(encoding="utf-8")
    nav = (src / "constants" / "modules.js").read_text(encoding="utf-8")
    assert "views/EquipmentView.vue" not in router and "views/FleetView.vue" not in router
    assert "'/equipment'" not in nav
    assert "equipment_tracking" not in nav

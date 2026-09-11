"""The owner-ruled orphan deletions of 2026-09-10 stay deleted, and what the
rulings kept stays served.

Each removed route had no SPA caller. What prod could show (read-only,
2026-09-10), and what it could not:
- the tables the removed write routes land in (`vehicles`,
  `vehicle_service_records`, `dispatch_routes`, `customer_equipments`) were
  empty. `forecast_snapshots` held 74 rows, all from the nightly beat task,
  which stays.
- only two removed routes ever wrote an audit row (the customer-scoped
  equipment PUT, the fleet-module service POST); `audit_logs` since 2026-06-22
  holds none from either.
- the reads leave no trace in `audit_logs`, and the proxy log reached back only
  to 2026-09-10 00:32 UTC; it showed no hit on any removed path.
The rulings are recorded on the issues: #637 (the module-router adjudication),
#648 (forecasting), #650 (bank-feed statement lines), #651 (equipment), #654
(inventory service).

Absence is asserted against the mounted route table (conftest.iter_app_routes),
never against source text. The KEPT list is its complement: a ruling that said
"delete this, keep that" is only half-honoured if a later cleanup takes both.
"""

from __future__ import annotations

import importlib.util
import os
from collections import Counter

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
    "gdx_dispatch.modules.inventory.service",     # #654
    "gdx_dispatch.modules.change_orders.router",  # #637, never mounted
    "gdx_dispatch.modules.gps_dispatch.router",   # #637
])
def test_module_no_longer_exists(dotted: str) -> None:
    assert importlib.util.find_spec(dotted) is None, f"{dotted} is back"


REMOVED_PATHS = [
    # #648: the nightly beat task captures and reconciles; nothing read these
    ("POST", "/api/forecast/snapshots"),
    ("POST", "/api/forecast/snapshots/reconcile"),
    ("GET", "/api/forecast/accuracy"),
    ("GET", "/api/forecast/calibration"),
    ("GET", "/api/forecast/snapshots"),
    # #650 (b)
    ("GET", "/api/bank-feeds/statements/lines"),
    # #651: customer-scoped copies of the tenant-wide equipment routes
    ("PUT", "/api/customers/{customer_id}/equipment/{equipment_id}"),
    ("GET", "/api/customers/{customer_id}/equipment/{equipment_id}/history"),
    # #637
    ("POST", "/api/catalog-policy/suggest-description"),
    ("POST", "/api/dispatch/location"),
    ("POST", "/api/dispatch/routes"),
    # #637: modules/fleet's maintenance copy, which read the unwritten `vehicles` table
    ("GET", "/api/fleet/vehicles/{vehicle_id}/service-history"),
    ("POST", "/api/fleet/vehicles/{vehicle_id}/service"),
    ("GET", "/api/fleet/due-maintenance"),
]

KEPT_PATHS = [
    # #651: office staff log service outside a job; the job route and the
    # history reader wait for their UI
    ("POST", "/api/equipment/{equipment_id}/service"),
    ("POST", "/api/jobs/{job_id}/equipment/{equipment_id}/service"),
    ("GET", "/api/equipment/{equipment_id}/service-history"),
    # #650 (a): the rename gets its UI in its own PR; create-expense shares the
    # removed route's prefix and is called by BankFeedsView
    ("PATCH", "/api/bank-feeds/statements/accounts/{account_id}"),
    ("POST", "/api/bank-feeds/statements/lines/{line_id}/create-expense"),
    # #637: the fleet maintenance copy that shares the Fleet page's table
    ("GET", "/api/fleet/vehicles/{vehicle_id}/service-log"),
    ("POST", "/api/fleet/vehicles/{vehicle_id}/service-log"),
    ("GET", "/api/fleet/vehicles/due-for-service"),
    # #648: the forecast the calibrated rates feed
    ("GET", "/api/forecast/revenue"),
]


@pytest.mark.parametrize("method,path", REMOVED_PATHS)
def test_removed_path_is_not_registered(registrations: Counter, method: str, path: str) -> None:
    assert registrations[(method, path)] == 0, f"{method} {path} is registered again"


@pytest.mark.parametrize("method,path", KEPT_PATHS)
def test_kept_path_is_still_served(registrations: Counter, method: str, path: str) -> None:
    assert registrations[(method, path)] == 1, f"{method} {path} has {registrations[(method, path)]} handlers"


def test_the_nightly_measurement_job_is_still_scheduled() -> None:
    """#648 kept the job: its reconciled snapshots set the collection rates in
    the live forecast (calibration.calibrated_window_rates)."""
    from gdx_dispatch.core.scheduler import build_beat_schedule

    entry = build_beat_schedule().get("forecasting-measurement-tick-daily")
    assert entry is not None
    assert entry["task"] == "gdx_dispatch.modules.forecasting.tasks.advance_forecast_measurement_dispatcher"

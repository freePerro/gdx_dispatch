"""ASGI entry for the plugin-host container.

Run with:  uvicorn gdx_dispatch.plugin_host.main:app --host 0.0.0.0 --port 8000

Boot sequence (ADR-013 Model B migration phase):
  0. reconcile: materialize desired-state into the /plugins volume (best-effort)
  1. discover plugins (imports each package → registers its models on PluginBase)
  2. create plugin tables on the shared DB (best-effort)
  3. build the app, mounting each plugin's router

To pick up a newly-installed plugin the container is restarted; the core app
keeps serving.

DEGRADE, DON'T DIE (2026-06-29 outage). Every step that touches the network or
the DB is best-effort: a failure is recorded and surfaced via /health (→ 503
degraded), never allowed to abort boot. The already-installed plugins live in the
persistent /plugins volume and are serveable without reconcile or the DB, so a
reconcile hang / DB blip / bad DDL must NOT take the whole plugin surface down —
it must come up serving what it has and report the rest as missing.

ponytail: step 2 uses PluginBase.metadata.create_all — idempotent and enough to
*create* tables; step 2b additively reconciles missing *columns* (create_all
never ALTERs). Data backfills / type changes / drops remain a per-plugin Alembic
branch (ADR-013) once a plugin needs real migrations.
"""
import logging
import sys

from gdx_dispatch.core.database import SessionLocal, engine
from gdx_dispatch.plugin_api.base import PluginBase
from gdx_dispatch.plugin_api.discovery import discover_with_dists
from gdx_dispatch.plugin_host.app import create_plugin_host
from gdx_dispatch.plugin_host.reconcile import (
    INSTALL_DIR,
    detect_stale,
    desired_versions,
    reconcile,
)
from gdx_dispatch.plugin_host.schema_reconcile import reconcile_plugin_columns

log = logging.getLogger(__name__)


def build_app():
    """Build the plugin-host ASGI app, degrading instead of dying on any
    network/DB failure. Returns a FastAPI app whose /ready reports every step
    that didn't complete (so a partial boot is loud, not silent), and which
    withholds any plugin loaded at the wrong version (fail closed)."""
    degraded: list[str] = []

    # Already-installed plugins persist in the volume across restarts; put it on
    # the path FIRST so discovery can find them even if reconcile never runs.
    if INSTALL_DIR not in sys.path:
        sys.path.insert(0, INSTALL_DIR)

    # 0. reconcile (best-effort): pip-install desired-state. Idempotent +
    #    fail-fast (no network hang); wrapped so even an unexpected error (DB
    #    down, bad DDL) degrades rather than aborts boot.
    try:
        degraded.extend(reconcile().failed)
    except Exception as exc:  # noqa: BLE001 - boot must survive any reconcile error
        log.exception("plugin reconcile crashed — serving already-installed plugins only")
        degraded.append(f"reconcile-error: {exc!r}")

    # 0b. read the operator's desired version per plugin so we can detect a STALE
    #     loaded plugin below. Best-effort: if the DB read fails we CANNOT know
    #     what's stale, so detection is skipped (fail-OPEN on this edge) — chosen
    #     over withholding every plugin on a transient DB blip, which would defeat
    #     degrade-don't-die. The miss is recorded in `degraded` so /ready 503s and
    #     the gap is visible, not silent.
    desired: dict[str, str] = {}
    try:
        db = SessionLocal()
        try:
            desired = desired_versions(db)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        log.exception("could not read desired plugin versions — stale check skipped")
        degraded.append(f"desired-versions-error: {exc!r}")

    # 1. discover (imports plugin packages, registering models on PluginBase),
    #    paired with the installed distribution version. load_manifests already
    #    skips a bad plugin rather than raising.
    discovered = discover_with_dists()
    plugins = [m for m, _name, _ver in discovered]

    # 1b. fail closed: a plugin loaded at a version != the desired one is withheld
    #     (a stale pricing plugin must not quote money off outdated logic).
    stale = detect_stale(desired, discovered)

    # 2/2b. create plugin tables + additively reconcile columns (best-effort: a
    #    DB blip here must not strand otherwise-serveable plugins).
    try:
        PluginBase.metadata.create_all(bind=engine)
        reconcile_plugin_columns(engine, PluginBase.metadata)
    except Exception as exc:  # noqa: BLE001
        log.exception("plugin table/column reconcile failed — possible schema drift")
        degraded.append(f"schema-error: {exc!r}")

    # 3. build the app. Anything incomplete reads as degraded on /ready so the
    #    Docker healthcheck flips unhealthy instead of serving a partial catalog.
    return create_plugin_host(plugins=plugins, degraded=degraded, stale=stale)


app = build_app()

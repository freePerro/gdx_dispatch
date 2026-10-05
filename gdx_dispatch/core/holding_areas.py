"""Holding-area lookup shared by the job and estimate routers.

One copy, not two: ``routers/jobs.py`` and ``routers/estimates.py`` each had
their own ``_holding_area_id_by_name``, and once GDXA-157 and GDXA-158 wrapped
both in ``contained_read`` they were verbatim twins (three net-new
duplicate-block groups). Both routers re-export it under the old private name,
so their call sites and tests are unchanged.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text as _text

from gdx_dispatch.core.database import contained_read

log = logging.getLogger(__name__)


def holding_area_id_by_name(db: Any, name: str) -> str | None:
    """Resolve a holding-area row by name. Returns id (str) or None if missing.

    Per the 2026-05-13 directive two routes are automatic: ``create_job``
    sends service-call jobs to 'Ready to Schedule', and the estimate
    accept/convert flow sends the new job to 'Order Doors'. If the area row is
    missing (the migration script is the source of truth) this returns None and
    the job is created without a holding area rather than failing the request;
    the dispatcher can re-route it.

    ``contained_read`` because that promise is false on Postgres without it: a
    failed statement aborts the caller's whole transaction, and the
    ``db.add(job)``/``flush()``/``commit()`` that follow die with 25P02.

    - GDXA-157 (estimates): this runs as an argument to the ``Job(...)``
      constructor in ``_create_job_from_estimate``. Without the savepoint, a
      missing ``holding_areas`` row let the customer's accept commit, gave the
      customer a 500, destroyed the audit record of the failure, and, because a
      re-click returns early on ``status == "accepted"``, meant the job was
      never created at all.
    - GDXA-158 (jobs): the read runs at ``create_job``'s
      ``derived_holding_area``, before the ``Job`` is staged, so the request
      would 500 with no job: the opposite of what this function promises.

    Reads only, so the savepoint sits on the Connection (core.database rules
    2/3), and it goes INSIDE this ``try`` so the ``except`` still runs (rule 1).
    ``contained_read``'s docstring sets out why prod, as configured, rarely or
    never fails a read while the connection stays up: this is insurance on a
    real mechanism, not a fix for an observed outage.
    """
    try:
        with contained_read(db):
            row = db.execute(
                _text("SELECT id FROM holding_areas WHERE name = :n LIMIT 1"),
                {"n": name},
            ).first()
        return str(row[0]) if row else None
    except Exception:
        log.exception("holding_area_lookup_failed name=%s", name)
        return None

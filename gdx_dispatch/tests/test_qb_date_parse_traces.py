"""QB date-parse swallows leave a trace (2026-09-20, #751 class / ledger 2026-09-17).

A malformed QB ``TxnDate``/``DueDate`` used to be silently swallowed on the
invoice money path — reproducing the documented pre-D99 outage shape (invoice
invisible to every period-filtered metric) with zero log on a prod box that
has no Sentry. ``_parse_qb_date`` is now the single seam: empty input stays a
quiet None (routine); an UNPARSEABLE value logs a warning naming the field,
qb_id and raw value, and returns None so update paths keep the stored date.
"""
from __future__ import annotations

import logging

from gdx_dispatch.modules.quickbooks.sync import _parse_qb_date


def test_empty_value_is_a_quiet_none(caplog):
    with caplog.at_level(logging.WARNING, logger="gdx_dispatch.modules.quickbooks.sync"):
        assert _parse_qb_date(None) is None
        assert _parse_qb_date("") is None
    assert not caplog.records, "empty dates are routine and must not spam the log"


def test_unparseable_value_logs_and_returns_none(caplog):
    with caplog.at_level(logging.WARNING, logger="gdx_dispatch.modules.quickbooks.sync"):
        assert _parse_qb_date("13/31/2026", field="TxnDate", qb_id="42") is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "qb_sync_unparseable_date" in m and "TxnDate" in m and "42" in m and "13/31/2026" in m
        for m in msgs
    ), f"no trace of the unparseable date; got: {msgs!r}"


def test_valid_value_parses_with_time_suffix_tolerated():
    parsed = _parse_qb_date("2026-09-20T00:00:00-05:00")
    assert parsed is not None and parsed.isoformat() == "2026-09-20"

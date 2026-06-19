"""S1-A2 — drive-time provider abstraction.

Covers:
- Provider switch: off / mapbox / google / unknown — only google calls
  Distance Matrix; everything else returns aligned all-None lists.
- Empty / single-stop / blank-address inputs short-circuit safely.
- Diagonal extraction from a Distance Matrix response.
- Failure paths (client init failure, API error, malformed response)
  degrade to all-None rather than raising.

Tests run synchronously via asyncio.run; the cache layer is patched out
to a passthrough so we don't depend on Redis for unit tests.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from gdx_dispatch.core import drive_time as dt


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def passthrough_cache(monkeypatch):
    """Bypass Redis: ``cached`` always calls fetcher and returns its result."""

    async def _passthrough(tenant_id, key, ttl, fetcher):
        result = fetcher()
        if hasattr(result, "__await__"):
            result = await result
        return result

    monkeypatch.setattr(dt, "cached", _passthrough)


# ── Trivial inputs ────────────────────────────────────────────────────


class TestTrivialInputs:
    def test_empty_addresses(self):
        assert _run(dt.compute_drive_times("t", [], provider="google")) == []

    def test_single_address(self):
        result = _run(dt.compute_drive_times("t", ["addr"], provider="google"))
        assert result == [None]

    def test_blank_address_anywhere_short_circuits(self):
        # If even one address is empty, we don't call Google (their API
        # 400s on blank origins/destinations and the failure shouldn't
        # tank the whole route).
        with patch.object(dt, "_google_distance_matrix") as mock_dm:
            result = _run(
                dt.compute_drive_times(
                    "t", ["A", "", "C"], provider="google"
                )
            )
        assert result == [None, None, None]
        mock_dm.assert_not_called()


# ── Provider switch ───────────────────────────────────────────────────


class TestProvider:
    def test_off_skips_api_call(self):
        with patch.object(dt, "_google_distance_matrix") as mock_dm:
            result = _run(
                dt.compute_drive_times("t", ["A", "B", "C"], provider="off")
            )
        assert result == [None, None, None]
        mock_dm.assert_not_called()

    def test_mapbox_skips_api_call_for_now(self):
        with patch.object(dt, "_google_distance_matrix") as mock_dm:
            result = _run(
                dt.compute_drive_times("t", ["A", "B", "C"], provider="mapbox")
            )
        assert result == [None, None, None]
        mock_dm.assert_not_called()

    def test_unknown_provider_falls_through_to_all_none(self):
        with patch.object(dt, "_google_distance_matrix") as mock_dm:
            result = _run(
                dt.compute_drive_times("t", ["A", "B"], provider="waze")
            )
        assert result == [None, None]
        mock_dm.assert_not_called()

    def test_google_invokes_distance_matrix(self):
        with patch.object(
            dt, "_google_distance_matrix", return_value=[None, 600, 900]
        ) as mock_dm:
            result = _run(
                dt.compute_drive_times("t", ["A", "B", "C"], provider="google")
            )
        assert result == [None, 600, 900]
        mock_dm.assert_called_once()


# ── Google response handling ──────────────────────────────────────────


class TestGoogleParser:
    """Direct unit tests on _google_distance_matrix for diagonal extraction."""

    def _matrix(self, durations: list[list[int | None]]) -> dict:
        """Build a Distance Matrix response with rows[i].elements[j]."""
        rows = []
        for row in durations:
            elements = []
            for val in row:
                if val is None:
                    elements.append({"status": "ZERO_RESULTS"})
                else:
                    elements.append(
                        {"status": "OK", "duration": {"value": val, "text": "x"}}
                    )
            rows.append({"elements": elements})
        return {"rows": rows}

    def _patched_client(self, response):
        class _Client:
            def distance_matrix(self, origins, destinations):
                return response

        return _Client()

    def test_extracts_diagonal_for_sequential_legs(self, monkeypatch):
        # 4 stops → 3 legs. origins=[A,B,C], destinations=[B,C,D].
        # Diagonal: rows[0].elements[0]=300, rows[1].elements[1]=600,
        # rows[2].elements[2]=120.
        response = self._matrix(
            [
                [300, 999, 999],
                [999, 600, 999],
                [999, 999, 120],
            ]
        )

        from gdx_dispatch.routers import maps as maps_mod

        monkeypatch.setattr(
            maps_mod, "get_google_maps_client", lambda: self._patched_client(response)
        )
        result = dt._google_distance_matrix(["A", "B", "C", "D"])
        assert result == [None, 300, 600, 120]

    def test_zero_results_leg_returns_none(self, monkeypatch):
        response = self._matrix([[None, 999], [999, 600]])

        from gdx_dispatch.routers import maps as maps_mod

        monkeypatch.setattr(
            maps_mod, "get_google_maps_client", lambda: self._patched_client(response)
        )
        result = dt._google_distance_matrix(["A", "B", "C"])
        assert result == [None, None, 600]

    def test_client_init_failure_returns_all_none(self, monkeypatch):
        from gdx_dispatch.routers import maps as maps_mod

        def _raise():
            raise RuntimeError("no key")

        monkeypatch.setattr(maps_mod, "get_google_maps_client", _raise)
        result = dt._google_distance_matrix(["A", "B"])
        assert result == [None, None]

    def test_api_call_failure_returns_all_none(self, monkeypatch):
        class _BoomClient:
            def distance_matrix(self, *a, **kw):
                raise RuntimeError("upstream timeout")

        from gdx_dispatch.routers import maps as maps_mod

        monkeypatch.setattr(maps_mod, "get_google_maps_client", lambda: _BoomClient())
        result = dt._google_distance_matrix(["A", "B"])
        assert result == [None, None]

    def test_malformed_response_returns_all_none(self, monkeypatch):
        class _OddClient:
            def distance_matrix(self, *a, **kw):
                return {"rows": [{"elements": [{"status": "OK"}]}]}  # no duration

        from gdx_dispatch.routers import maps as maps_mod

        monkeypatch.setattr(maps_mod, "get_google_maps_client", lambda: _OddClient())
        result = dt._google_distance_matrix(["A", "B"])
        assert result == [None, None]


# ── Cache key stability ───────────────────────────────────────────────


class TestCacheKey:
    def test_same_addresses_same_key(self):
        k1 = dt._route_cache_key(["A", "B", "C"], "google")
        k2 = dt._route_cache_key(["A", "B", "C"], "google")
        assert k1 == k2

    def test_different_provider_different_key(self):
        k_g = dt._route_cache_key(["A", "B"], "google")
        k_m = dt._route_cache_key(["A", "B"], "mapbox")
        assert k_g != k_m

    def test_address_order_matters(self):
        k_ab = dt._route_cache_key(["A", "B"], "google")
        k_ba = dt._route_cache_key(["B", "A"], "google")
        assert k_ab != k_ba

    def test_caller_extra_address_changes_key(self):
        k1 = dt._route_cache_key(["A", "B"], "google")
        k2 = dt._route_cache_key(["A", "B", "C"], "google")
        assert k1 != k2

"""SS-25 Slice A — tests for ``gdx_dispatch.core.api_version``."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gdx_dispatch.core.api_version import (
    APIVersionError,
    SUPPORTED_VERSIONS,
    format_deprecation_header,
    format_sunset_header,
    latest_version,
    resolve_version,
)


class TestResolveVersion:
    def test_none_falls_back_to_latest(self):
        result = resolve_version(None)
        assert result.version == latest_version()
        assert result.explicit is False

    def test_empty_string_falls_back(self):
        result = resolve_version("")
        assert result.explicit is False

    def test_wildcard_falls_back(self):
        result = resolve_version("*/*")
        assert result.version == latest_version()
        assert result.explicit is False

    def test_plain_json_falls_back(self):
        result = resolve_version("application/json")
        assert result.explicit is False

    def test_explicit_v1(self):
        result = resolve_version("application/vnd.gdx.v1+json")
        assert result.version == 1
        assert result.explicit is True

    def test_case_insensitive(self):
        result = resolve_version("Application/VND.GDX.V1+JSON")
        assert result.version == 1
        assert result.explicit is True

    def test_with_quality_param(self):
        result = resolve_version("application/vnd.gdx.v1+json;q=0.9")
        assert result.version == 1

    def test_multi_entry_picks_vendor(self):
        result = resolve_version("application/json, application/vnd.gdx.v1+json")
        assert result.version == 1
        assert result.explicit is True

    def test_malformed_version_token_raises(self):
        with pytest.raises(APIVersionError):
            resolve_version("application/vnd.gdx.vfoo+json")

    def test_zero_version_raises(self):
        with pytest.raises(APIVersionError):
            resolve_version("application/vnd.gdx.v0+json")

    def test_unsupported_version_raises(self):
        with pytest.raises(APIVersionError):
            resolve_version("application/vnd.gdx.v9999+json")

    def test_wrong_suffix_raises(self):
        # Opted into vendor type but asked for XML — don't silently hand JSON.
        with pytest.raises(APIVersionError):
            resolve_version("application/vnd.gdx.v1+xml")

    def test_supported_versions_nonempty(self):
        assert SUPPORTED_VERSIONS
        assert latest_version() == SUPPORTED_VERSIONS[-1]


class TestSunsetHeader:
    def test_utc_renders_http_date(self):
        ts = datetime(2027, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        assert format_sunset_header(ts) == "Tue, 01 Jun 2027 12:30:00 GMT"

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            format_sunset_header(datetime(2027, 1, 1))  # noqa: DTZ001


class TestDeprecationHeader:
    def test_none_is_true_literal(self):
        assert format_deprecation_header(None) == "true"

    def test_dated_form(self):
        ts = datetime(2026, 4, 15, 0, 0, 0, tzinfo=timezone.utc)
        assert format_deprecation_header(ts) == "Wed, 15 Apr 2026 00:00:00 GMT"

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            format_deprecation_header(datetime(2026, 1, 1))  # noqa: DTZ001

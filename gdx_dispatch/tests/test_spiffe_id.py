"""SS-32 slice A tests — SPIFFE ID parser + validator."""
from __future__ import annotations

import pytest

from gdx_dispatch.core.spiffe.spiffe_id import (
    SpiffeID,
    SpiffeIdError,
    is_valid_spiffe_id,
    parse_spiffe_id,
    try_parse_spiffe_id,
)


class TestValid:
    def test_bare_trust_domain(self):
        sid = parse_spiffe_id("spiffe://example.com")
        assert sid.trust_domain == "example.com"
        assert sid.path == ""
        assert sid.segments == []
        assert sid.uri == "spiffe://example.com"

    def test_simple_path(self):
        sid = parse_spiffe_id("spiffe://example.com/agent/worker-1")
        assert sid.trust_domain == "example.com"
        assert sid.path == "/agent/worker-1"
        assert sid.segments == ["agent", "worker-1"]

    def test_full_charset_segment(self):
        sid = parse_spiffe_id(
            "spiffe://td.example/a.b_c~d!e$f&g'h(i)j*k+l,m;n=o:p@q-r"
        )
        assert sid.segments == ["a.b_c~d!e$f&g'h(i)j*k+l,m;n=o:p@q-r"]

    def test_percent_encoded(self):
        sid = parse_spiffe_id("spiffe://td.example/path%20space")
        assert sid.segments == ["path%20space"]


class TestInvalid:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "spiffe://",
            "spiffe:///path",            # empty trust domain
            "spiffe://TD.EXAMPLE/x",     # uppercase trust domain
            "spiffe://td.example/",      # trailing slash
            "spiffe://td.example//x",    # empty segment
            "spiffe://td.example/./x",   # dot segment
            "spiffe://td.example/../x",  # dotdot segment
            "spiffe://td.example/ok/..",  # dotdot at end
            "http://td.example/x",       # wrong scheme
            "SPIFFE://td.example/x",     # scheme case
            "spiffe://td.example/bad char",  # space in path
            "spiffe://td .example/x",    # space in td
            "spiffe://td.example/tab\tseg",  # tab
        ],
    )
    def test_rejects(self, bad):
        assert not is_valid_spiffe_id(bad)
        with pytest.raises(SpiffeIdError):
            parse_spiffe_id(bad)

    def test_rejects_non_string(self):
        assert not is_valid_spiffe_id(None)
        assert not is_valid_spiffe_id(42)
        assert not is_valid_spiffe_id(b"spiffe://td.example")


def test_try_parse_returns_none_on_invalid():
    assert try_parse_spiffe_id("nope") is None
    assert isinstance(try_parse_spiffe_id("spiffe://td.example"), SpiffeID)

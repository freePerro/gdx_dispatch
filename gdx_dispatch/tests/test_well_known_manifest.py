"""SS-26: tests for the .well-known manifest builder functions."""
from __future__ import annotations

import pytest

from gdx_dispatch.core import well_known_manifest as wkm


class TestTgdPlatformManifest:
    def test_has_required_top_level_fields(self):
        m = wkm.build_manifest(base_url="https://example.test")
        assert m["name"] == "GDX Platform"
        assert m["version"] == wkm.MANIFEST_VERSION
        assert m["issuer"] == "https://example.test"
        assert m["api_docs_url"].endswith("/docs")
        assert "@" in m["contact_email"]

    def test_directory_endpoints_point_to_well_known_paths(self):
        m = wkm.build_manifest(base_url="https://example.test")
        d = m["directory_endpoints"]
        assert d["oauth_authorization_server"] == "https://example.test/.well-known/oauth-authorization-server"
        assert d["openid_configuration"] == "https://example.test/.well-known/openid-configuration"
        assert d["gdx_platform"] == "https://example.test/.well-known/gdx-platform"
        assert d["security_txt"] == "https://example.test/.well-known/security.txt"
        assert d["mcp_tools"] == "https://example.test/.well-known/mcp-tools"

    def test_supported_features_includes_oauth_pkce_s256(self):
        m = wkm.build_manifest(base_url="https://example.test")
        assert "oauth2.pkce.s256" in m["supported_features"]
        assert "mcp.tools" in m["supported_features"]

    def test_contact_email_override(self):
        m = wkm.build_manifest(base_url="https://x.test", contact_email="ops@x.test")
        assert m["contact_email"] == "ops@x.test"

    def test_base_url_trailing_slash_normalised(self):
        m = wkm.build_manifest(base_url="https://example.test/")
        assert m["issuer"] == "https://example.test"
        assert m["directory_endpoints"]["gdx_platform"].count("//") == 1


class TestOAuthAuthorizationServerMetadata:
    def test_required_rfc8414_fields_present(self):
        m = wkm.build_oauth_authorization_server(base_url="https://a.test")
        for field in (
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
            "introspection_endpoint",
            "grant_types_supported",
            "code_challenge_methods_supported",
            "response_types_supported",
        ):
            assert field in m, f"missing {field}"

    def test_s256_pkce_supported(self):
        """SS-21 depends on PKCE S256 advertisement."""
        m = wkm.build_oauth_authorization_server(base_url="https://a.test")
        assert "S256" in m["code_challenge_methods_supported"]

    def test_issuer_matches_base(self):
        m = wkm.build_oauth_authorization_server(base_url="https://a.test")
        assert m["issuer"] == "https://a.test"


class TestOpenIDConfiguration:
    def test_required_fields_present(self):
        m = wkm.build_openid_configuration(base_url="https://a.test")
        for f in (
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "jwks_uri",
            "response_types_supported",
            "subject_types_supported",
            "id_token_signing_alg_values_supported",
        ):
            assert f in m

    def test_rs256_in_id_token_signing(self):
        m = wkm.build_openid_configuration(base_url="https://a.test")
        assert "RS256" in m["id_token_signing_alg_values_supported"]


class TestSecurityTxt:
    def test_required_rfc9116_fields(self):
        body = wkm.build_security_txt(
            contact_email="sec@x.test", expires_iso="2030-01-01T00:00:00Z", base_url="https://x.test"
        )
        assert "Contact: mailto:sec@x.test" in body
        assert "Expires: 2030-01-01T00:00:00Z" in body
        assert "Preferred-Languages: en" in body

    def test_canonical_points_to_self(self):
        body = wkm.build_security_txt(
            contact_email="sec@x.test", expires_iso="2030-01-01T00:00:00Z", base_url="https://x.test"
        )
        assert "Canonical: https://x.test/.well-known/security.txt" in body

    def test_default_expires_is_iso8601(self):
        body = wkm.build_security_txt(contact_email="sec@x.test", base_url="https://x.test")
        # Match "Expires: YYYY-MM-DDTHH:MM:SSZ" without pulling regex lib semantics.
        line = [ln for ln in body.splitlines() if ln.startswith("Expires:")][0]
        assert line.endswith("Z")
        assert "T" in line


class TestMcpToolsManifest:
    def test_default_tools_listed(self):
        m = wkm.build_mcp_tools_manifest(base_url="https://x.test")
        # S3: mcp_endpoint advertises the Streamable-HTTP transport.
        assert m["mcp_endpoint"] == "https://x.test/mcp"
        assert m["legacy_mcp_endpoint"] == "https://x.test/api/mcp"
        assert isinstance(m["tools"], list)
        assert len(m["tools"]) >= 1
        for t in m["tools"]:
            assert "name" in t and "uri" in t

    def test_injected_tools_passthrough(self):
        injected = [{"name": "foo", "uri": "https://x.test/api/mcp/tools/foo"}]
        m = wkm.build_mcp_tools_manifest(tools=injected, base_url="https://x.test")
        assert m["tools"] == injected

"""Tests for the .well-known manifest builders.

These documents are read by machines that will follow whatever they say. The
central rule they enforce is therefore not "the required fields are present"
but "every URL advertised resolves to a route this app registers" — see
``TestAdvertisedEndpointsExist``, which is the guard the previous version of
this suite lacked. Until 2026-09-04 the manifest advertised an OAuth
authorization server, a developer portal, a metering API and an event
catalog, none of which existed; the old tests asserted those fields were
present, so a fully green suite pinned the fiction in place.
"""
from __future__ import annotations

from gdx_dispatch.core import well_known_manifest as wkm


class TestGdxPlatformManifest:
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
        assert d["gdx_platform"] == "https://example.test/.well-known/gdx-platform"
        assert d["security_txt"] == "https://example.test/.well-known/security.txt"
        assert d["mcp_tools"] == "https://example.test/.well-known/mcp-tools"

    def test_does_not_advertise_an_authorization_server(self):
        """There is no OAuth server here, so nothing may point at one.

        `/oauth/authorize`, `/oauth/token` and `/oauth/register` left with the
        multi-tenant platform routers. On production they answer 200 with the
        SPA's HTML rather than 404, so a client that follows discovery gets a
        login page where it expects JSON and cannot tell it went wrong.
        """
        m = wkm.build_manifest(base_url="https://example.test")
        assert "oauth_endpoints" not in m
        d = m["directory_endpoints"]
        assert "oauth_authorization_server" not in d
        assert "openid_configuration" not in d
        blob = str(m)
        for dead in ("/oauth/authorize", "/oauth/token", "/oauth/register",
                     "/oauth/revoke", "/oauth/introspect", "jwks.json"):
            assert dead not in blob, f"manifest still advertises {dead}"

    def test_does_not_advertise_a_developer_portal_metering_or_event_catalog(self):
        m = wkm.build_manifest(base_url="https://example.test")
        blob = str(m)
        for dead in ("dev_portal", "metering", "deprecation-policy",
                     "/api/events/catalog", "events.outbox_replay"):
            assert dead not in blob, f"manifest still advertises {dead}"

    def test_supported_features_names_only_what_is_served(self):
        m = wkm.build_manifest(base_url="https://example.test")
        assert m["supported_features"] == ["mcp.tools"]

    def test_mcp_endpoint_is_advertised(self):
        m = wkm.build_manifest(base_url="https://example.test")
        assert m["mcp_endpoint"] == "https://example.test/mcp"

    def test_contact_email_override(self):
        m = wkm.build_manifest(base_url="https://x.test", contact_email="ops@x.test")
        assert m["contact_email"] == "ops@x.test"

    def test_base_url_trailing_slash_normalised(self):
        m = wkm.build_manifest(base_url="https://example.test/")
        assert m["issuer"] == "https://example.test"
        assert m["directory_endpoints"]["gdx_platform"].count("//") == 1


class TestNoProtectedResourceMetadata:
    """No PRM document, deliberately.

    MCP Authorization (revision 2025-06-18) makes authorization OPTIONAL, but
    for servers that support it: "The Protected Resource Metadata document
    returned by the MCP server MUST include the authorization_servers field
    containing at least one authorization server."

    This server has no authorization server. A PRM naming one would point at
    the RFC 8414 metadata that was deleted; a PRM omitting the field would
    violate that MUST. So it serves none, and a client learns in one request
    that OAuth is not on offer here.
    """

    def test_builder_is_gone(self):
        assert not hasattr(wkm, "build_oauth_protected_resource")

    def test_manifest_does_not_link_to_one(self):
        m = wkm.build_manifest(base_url="https://example.test")
        assert "oauth_protected_resource" not in m["directory_endpoints"]
        assert "oauth-protected-resource" not in str(m)


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

    def test_omits_policy_and_acknowledgments_pages_that_do_not_exist(self):
        """/security/policy and /security/hall-of-fame are not routes.

        Both fall through to the SPA catch-all and return the app's HTML, so
        a researcher following security.txt lands on the dashboard.
        """
        body = wkm.build_security_txt(
            contact_email="sec@x.test", expires_iso="2030-01-01T00:00:00Z", base_url="https://x.test"
        )
        assert "Policy:" not in body
        assert "Acknowledgments:" not in body

    def test_default_expires_is_iso8601(self):
        body = wkm.build_security_txt(contact_email="sec@x.test", base_url="https://x.test")
        line = [ln for ln in body.splitlines() if ln.startswith("Expires:")][0]
        assert line.endswith("Z")
        assert "T" in line


class TestMcpToolsManifest:
    def test_advertises_the_transport_not_per_tool_urls(self):
        m = wkm.build_mcp_tools_manifest(base_url="https://x.test")
        assert m["mcp_endpoint"] == "https://x.test/mcp"
        # /api/mcp and /api/mcp/tools/* have no routes; every such URI 404s.
        assert "legacy_mcp_endpoint" not in m
        assert "tools_index_url" not in m
        assert "/api/mcp" not in str(m)

    def test_does_not_enumerate_tools_to_anonymous_callers(self):
        """This document has no auth dependency — it is world-readable.

        Publishing the tool inventory would tell anyone what the server can be
        made to do (the registry contains invoices.void, email.read,
        documents.read). The transport gates tools/list behind a bearer token;
        that is where a capability list belongs.
        """
        import gdx_dispatch.core.mcp_tools  # noqa: F401  side-effect: registers tools
        from gdx_dispatch.core.mcp_registry import list_tools

        m = wkm.build_mcp_tools_manifest(base_url="https://x.test")
        assert "tools" not in m
        blob = str(m)
        for descriptor in list_tools():
            assert descriptor.name not in blob, f"leaked tool name {descriptor.name}"
            assert descriptor.name.replace(".", "_") not in blob

    def test_says_where_the_real_tool_list_is(self):
        m = wkm.build_mcp_tools_manifest(base_url="https://x.test")
        assert "tools/list" in m["tools_discovery"]


class TestAdvertisedEndpointsExist:
    """The guard the old suite did not have.

    Walks every absolute URL in every discovery document and asserts the app
    registers a route for its path. This is what turns "the docs are honest"
    from a claim into something that can go red.
    """

    @staticmethod
    def _urls(base: str) -> set[str]:
        import gdx_dispatch.core.mcp_tools  # noqa: F401

        found: set[str] = set()

        def walk(node):
            if isinstance(node, str):
                if node.startswith(base):
                    found.add(node)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(wkm.build_manifest(base_url=base))
        walk(wkm.build_mcp_tools_manifest(base_url=base))
        for line in wkm.build_security_txt(
            contact_email="s@x.test", expires_iso="2030-01-01T00:00:00Z", base_url=base
        ).splitlines():
            for token in line.split():
                if token.startswith(base):
                    found.add(token)
        return found

    def test_every_advertised_url_actually_answers(self):
        """Fetch every advertised URL and refuse HTML.

        Asserting a route is *registered* is not enough: the SPA catch-all
        answers any unmatched non-API path with index.html and a 200, which is
        exactly how /oauth/authorize looked "alive" on production while being
        entirely absent. So the check is: not a 404, and not HTML.
        """
        from fastapi.testclient import TestClient

        from gdx_dispatch.app import create_app

        base = "https://example.test"
        client = TestClient(create_app())

        bad = []
        for url in sorted(self._urls(base)):
            path = url[len(base):]
            if path in ("", "/"):
                continue  # the issuer identifier, not an endpoint
            resp = client.get(path, follow_redirects=False)
            if resp.status_code == 404:
                bad.append(f"{path} -> 404")
                continue
            if "text/html" not in resp.headers.get("content-type", ""):
                continue
            # HTML is only ever correct for the Swagger UI page. Anything else
            # returning HTML is the SPA catch-all pretending an endpoint
            # exists — the exact failure that made /oauth/authorize look alive.
            if path == "/docs" and "swagger" in resp.text.lower():
                continue
            bad.append(f"{path} -> HTML from the SPA catch-all ({resp.status_code})")
        assert not bad, f"discovery documents advertise dead URLs: {bad}"

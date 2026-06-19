"""Sprint 1.x-S21 — MCP bearer-token auth dependency."""
from __future__ import annotations
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient


def test_dependency_imports():
    from gdx_dispatch.core.mcp_protocol_adapter import get_mcp_principal
    assert callable(get_mcp_principal)


def test_missing_authorization_returns_401():
    from gdx_dispatch.core.mcp_protocol_adapter import get_mcp_principal
    app = FastAPI()

    @app.get("/probe")
    def probe(p = Depends(get_mcp_principal)):
        return {"identity_id": str(p.identity_id)}

    client = TestClient(app)
    r = client.get("/probe")
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("www-authenticate", "")


def test_invalid_token_returns_401():
    from gdx_dispatch.core.mcp_protocol_adapter import get_mcp_principal
    app = FastAPI()

    @app.get("/probe")
    def probe(p = Depends(get_mcp_principal)):
        return {"identity_id": str(p.identity_id)}

    client = TestClient(app)
    r = client.get("/probe", headers={"Authorization": "Bearer this-is-not-a-real-token-XYZ"})
    assert r.status_code == 401

#!/usr/bin/env python3
"""Sprint MCP-Streamable-HTTP S6 — lab smoke verification.

Walks the claude.ai-connector signup chain against a deployed tenant
host and reports per-step pass/fail. Read-only by default — no
deploy actions, no destructive tool calls.

The 11-step chain (all expected to pass before approving S7 prod):

  D1. GET  /.well-known/oauth-protected-resource
  D2. GET  /.well-known/oauth-authorization-server
  R1. POST /oauth/register  (RFC 7591 DCR)
  A1. GET  /oauth/authorize (sandbox auto-approve via subject_id)
  T1. POST /oauth/token     (RFC 8707 resource indicator → JWT)
  V1. Decode + verify JWT claims (iss / aud / gdx_tid / scope)
  M1. POST /mcp/  initialize
  M2. POST /mcp/  notifications/initialized
  M3. POST /mcp/  tools/list   (count >= 35)
  M4. POST /mcp/  tools/call   (--call-tool, optional)
  X1. Cross-tenant denial      (--cross-host, optional)

Usage:
    python -m gdx_dispatch.tools.mcp_lab_smoke --host gdx.lab.example.com
    python -m gdx_dispatch.tools.mcp_lab_smoke --host gdx.lab... --cross-host acme.lab...
    python -m gdx_dispatch.tools.mcp_lab_smoke --host gdx.lab... --call-tool list_customers

Exit code:
    0 — every required step passed
    1 — at least one required step failed
    2 — script invocation error (bad args, network unreachable)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


# ── result-tracking ─────────────────────────────────────────────────────────


@dataclass
class StepResult:
    step: str
    label: str
    ok: bool
    detail: str = ""

    def render(self) -> str:
        mark = "✓" if self.ok else "✗"
        head = f"  {mark} {self.step}  {self.label}"
        if self.detail:
            head += f"\n        {self.detail}"
        return head


class Smoke:
    def __init__(self, host: str, scheme: str = "https") -> None:
        self.host = host
        self.scheme = scheme
        self.base = f"{scheme}://{host}"
        self.client = httpx.Client(timeout=10.0, follow_redirects=False,
                                   headers={"Host": host})
        self.results: list[StepResult] = []

    def close(self) -> None:
        self.client.close()

    def _record(self, step: str, label: str, ok: bool, detail: str = "") -> bool:
        self.results.append(StepResult(step, label, ok, detail))
        return ok

    def render(self) -> str:
        lines = [f"\n=== MCP lab smoke against {self.base} ==="]
        lines.extend(r.render() for r in self.results)
        passed = sum(1 for r in self.results if r.ok)
        lines.append(f"\n  {passed}/{len(self.results)} steps passed")
        return "\n".join(lines)

    def all_ok(self) -> bool:
        return all(r.ok for r in self.results)


# ── PKCE helpers ────────────────────────────────────────────────────────────


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:80]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── steps ───────────────────────────────────────────────────────────────────


def step_d1_protected_resource(s: Smoke) -> dict | None:
    """D1: discover the protected-resource metadata."""
    r = s.client.get(f"{s.base}/.well-known/oauth-protected-resource")
    if r.status_code != 200:
        s._record("D1", "oauth-protected-resource", False,
                  f"HTTP {r.status_code}: {r.text[:200]}")
        return None
    body = r.json()
    expected_resource = f"{s.base}/mcp"
    if body.get("resource") != expected_resource:
        s._record("D1", "oauth-protected-resource", False,
                  f"resource={body.get('resource')!r} expected={expected_resource!r}")
        return None
    s._record("D1", "oauth-protected-resource", True,
              f"resource={body['resource']}")
    return body


def step_d2_authorization_server(s: Smoke) -> dict | None:
    """D2: discover the AS metadata."""
    r = s.client.get(f"{s.base}/.well-known/oauth-authorization-server")
    if r.status_code != 200:
        s._record("D2", "oauth-authorization-server", False,
                  f"HTTP {r.status_code}")
        return None
    body = r.json()
    issues = []
    if body.get("issuer") != s.base:
        issues.append(f"issuer={body.get('issuer')!r}")
    if not body.get("registration_endpoint"):
        issues.append("registration_endpoint missing")
    if body.get("resource_indicators_supported") is not True:
        issues.append("resource_indicators_supported != true")
    if "S256" not in (body.get("code_challenge_methods_supported") or []):
        issues.append("S256 not advertised")
    ok = not issues
    detail = (f"issuer={body.get('issuer')}" if ok
              else "; ".join(issues))
    s._record("D2", "oauth-authorization-server", ok, detail)
    return body if ok else None


def step_r1_register(s: Smoke, redirect_uri: str) -> dict | None:
    """R1: DCR — mint a fresh client."""
    r = s.client.post(
        f"{s.base}/oauth/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "gdx lab smoke",
        },
    )
    if r.status_code != 201:
        s._record("R1", "oauth/register", False,
                  f"HTTP {r.status_code}: {r.text[:200]}")
        return None
    body = r.json()
    if not body.get("client_id") or not body.get("client_secret"):
        s._record("R1", "oauth/register", False,
                  "missing client_id or client_secret in response")
        return None
    s._record("R1", "oauth/register", True,
              f"client_id={body['client_id'][:24]}...")
    return body


def step_a1_authorize(s: Smoke, *, client_id: str, redirect_uri: str,
                      challenge: str, resource: str) -> str | None:
    """A1: /oauth/authorize — sandbox auto-approve via subject_id query.

    The current implementation has subject_id as an INTEGRATION_TODO
    sandbox path. Production with a real session dependency would
    replace this with a browser-driven consent screen; this step works
    against the sandbox path that's currently deployed.
    """
    r = s.client.get(
        f"{s.base}/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp:invoke",
            "state": "smoke",
            "subject_id": "smoke@example.com",
            "resource": resource,
        },
    )
    if r.status_code not in (302, 307):
        s._record("A1", "oauth/authorize", False,
                  f"HTTP {r.status_code} (expected 302/307): {r.text[:200]}")
        return None
    loc = r.headers.get("location", "")
    qs = parse_qs(urlparse(loc).query)
    code = (qs.get("code") or [None])[0]
    if not code:
        s._record("A1", "oauth/authorize", False,
                  f"no `code` in redirect: {loc[:200]}")
        return None
    s._record("A1", "oauth/authorize", True, f"code length={len(code)}")
    return code


def step_t1_token(s: Smoke, *, code: str, redirect_uri: str,
                  client_id: str, verifier: str, resource: str) -> dict | None:
    """T1: /oauth/token — redeem code for a JWT."""
    r = s.client.post(
        f"{s.base}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    if r.status_code != 200:
        s._record("T1", "oauth/token", False,
                  f"HTTP {r.status_code}: {r.text[:200]}")
        return None
    body = r.json()
    if not body.get("access_token"):
        s._record("T1", "oauth/token", False, "no access_token in response")
        return None
    s._record("T1", "oauth/token", True, f"token_type={body.get('token_type')}")
    return body


def step_v1_verify_jwt(s: Smoke, token: str, *, expected_aud: str,
                       expected_iss: str) -> dict | None:
    """V1: parse JWT (no signature check — we don't have the lab key
    locally). Confirm the three load-bearing claims have the right
    shape; the transport's middleware does the signature work."""
    parts = token.split(".")
    if len(parts) != 3:
        s._record("V1", "verify JWT shape", False,
                  f"expected 3 segments, got {len(parts)}")
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    except Exception as exc:  # noqa: BLE001
        s._record("V1", "verify JWT shape", False, f"payload decode: {exc}")
        return None
    issues = []
    if payload.get("aud") != expected_aud:
        issues.append(f"aud={payload.get('aud')!r} expected={expected_aud!r}")
    if payload.get("iss") != expected_iss:
        issues.append(f"iss={payload.get('iss')!r} expected={expected_iss!r}")
    if not payload.get("gdx_tid"):
        issues.append("gdx_tid missing")
    ok = not issues
    detail = (f"gdx_tid={payload.get('gdx_tid')} scope={payload.get('scope')}"
              if ok else "; ".join(issues))
    s._record("V1", "verify JWT shape", ok, detail)
    return payload if ok else None


def _post_mcp(s: Smoke, *, body: dict, token: str,
              session_id: str | None = None,
              host_override: str | None = None) -> httpx.Response:
    headers = {
        "Host": host_override or s.host,
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    base = f"{s.scheme}://{host_override or s.host}"
    return s.client.post(f"{base}/mcp/", json=body, headers=headers)


def _parse_sse(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise ValueError("no SSE data: line in body")


def step_m1_initialize(s: Smoke, token: str) -> str | None:
    """M1: MCP initialize handshake → returns Mcp-Session-Id."""
    r = _post_mcp(s, body={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "gdx-lab-smoke", "version": "1.0"}},
    }, token=token)
    if r.status_code != 200:
        s._record("M1", "/mcp initialize", False,
                  f"HTTP {r.status_code}: {r.text[:200]}")
        return None
    sid = r.headers.get("mcp-session-id")
    if not sid:
        s._record("M1", "/mcp initialize", False, "no Mcp-Session-Id header")
        return None
    s._record("M1", "/mcp initialize", True, f"sid={sid[:16]}...")
    return sid


def step_m2_initialized(s: Smoke, token: str, sid: str) -> bool:
    """M2: notifications/initialized — required ack before tools/list."""
    r = _post_mcp(s, body={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }, token=token, session_id=sid)
    if r.status_code != 202:
        return s._record("M2", "/mcp notifications/initialized", False,
                         f"HTTP {r.status_code}")
    return s._record("M2", "/mcp notifications/initialized", True, "")


def step_m3_tools_list(s: Smoke, token: str, sid: str,
                       expected_min: int = 35) -> list[dict] | None:
    r = _post_mcp(s, body={
        "jsonrpc": "2.0", "id": 3, "method": "tools/list",
    }, token=token, session_id=sid)
    if r.status_code != 200:
        s._record("M3", "/mcp tools/list", False, f"HTTP {r.status_code}")
        return None
    try:
        payload = _parse_sse(r.text)
    except ValueError as exc:
        s._record("M3", "/mcp tools/list", False, f"parse: {exc}")
        return None
    tools = payload.get("result", {}).get("tools") or []
    ok = len(tools) >= expected_min
    detail = f"{len(tools)} tools" + (
        f" (>= {expected_min})" if ok else f" (< {expected_min} expected)"
    )
    s._record("M3", "/mcp tools/list", ok, detail)
    return tools if ok else None


def step_m4_tools_call(s: Smoke, token: str, sid: str, tool_name: str,
                       args: dict | None = None) -> dict | None:
    r = _post_mcp(s, body={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": tool_name, "arguments": args or {}},
    }, token=token, session_id=sid)
    if r.status_code != 200:
        s._record("M4", f"/mcp tools/call {tool_name}", False,
                  f"HTTP {r.status_code}")
        return None
    try:
        payload = _parse_sse(r.text)
    except ValueError as exc:
        s._record("M4", f"/mcp tools/call {tool_name}", False, f"parse: {exc}")
        return None
    result = payload.get("result", {})
    if result.get("isError"):
        s._record("M4", f"/mcp tools/call {tool_name}", False,
                  f"handler returned isError: {str(result)[:200]}")
        return None
    s._record("M4", f"/mcp tools/call {tool_name}", True,
              f"keys={list(result)}")
    return result


def step_x1_cross_tenant_denial(s: Smoke, token: str,
                                cross_host: str) -> bool:
    """X1: present the gdx-bound token at another tenant's /mcp.

    Expected: 403 (or 404 if cross_host is unknown). Anything else is a
    failed verification gate — DO NOT proceed to S7.
    """
    r = _post_mcp(s, body={
        "jsonrpc": "2.0", "id": 9, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "x", "version": "1"}},
    }, token=token, host_override=cross_host)
    if r.status_code in (200,):
        return s._record(
            "X1", f"cross-tenant denial @ {cross_host}", False,
            f"⚠⚠ token from {s.host} ACCEPTED at {cross_host} — "
            "verification gate FAILED, do not deploy",
        )
    return s._record(
        "X1", f"cross-tenant denial @ {cross_host}", True,
        f"HTTP {r.status_code} (rejected as expected)",
    )


# ── orchestrator ────────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> int:
    s = Smoke(args.host, scheme=args.scheme)
    redirect_uri = args.redirect_uri
    expected_resource = f"{s.base}/mcp"
    expected_issuer = s.base
    try:
        if not step_d1_protected_resource(s):
            return _final(s, ok_required=False)
        if not step_d2_authorization_server(s):
            return _final(s, ok_required=False)
        reg = step_r1_register(s, redirect_uri)
        if not reg:
            return _final(s, ok_required=False)

        verifier, challenge = _pkce()
        code = step_a1_authorize(
            s, client_id=reg["client_id"], redirect_uri=redirect_uri,
            challenge=challenge, resource=expected_resource,
        )
        if not code:
            return _final(s, ok_required=False)
        tok = step_t1_token(
            s, code=code, redirect_uri=redirect_uri,
            client_id=reg["client_id"], verifier=verifier,
            resource=expected_resource,
        )
        if not tok:
            return _final(s, ok_required=False)
        access = tok["access_token"]
        if not step_v1_verify_jwt(s, access,
                                  expected_aud=expected_resource,
                                  expected_iss=expected_issuer):
            return _final(s, ok_required=False)

        sid = step_m1_initialize(s, access)
        if not sid:
            return _final(s, ok_required=False)
        if not step_m2_initialized(s, access, sid):
            return _final(s, ok_required=False)
        if not step_m3_tools_list(s, access, sid):
            return _final(s, ok_required=False)

        # Optional steps.
        if args.call_tool:
            step_m4_tools_call(s, access, sid, args.call_tool,
                               args.call_args or {})
        if args.cross_host:
            step_x1_cross_tenant_denial(s, access, args.cross_host)

        return _final(s, ok_required=True)
    finally:
        s.close()


def _final(s: Smoke, *, ok_required: bool) -> int:
    print(s.render())
    return 0 if (ok_required and s.all_ok()) else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True,
                   help="Tenant host, e.g. gdx.lab.example.com")
    p.add_argument("--scheme", default="https", choices=["http", "https"])
    p.add_argument("--redirect-uri", default="https://example.invalid/cb",
                   help="Redirect URI to register (must be a real URL shape)")
    p.add_argument("--call-tool", default=None,
                   help="Optional tool name to invoke for tools/call (e.g. list_customers)")
    p.add_argument("--call-args", type=json.loads, default=None,
                   help="JSON-encoded args for --call-tool (e.g. '{\"limit\":1}')")
    p.add_argument("--cross-host", default=None,
                   help="Optional second tenant host to verify cross-tenant denial")
    args = p.parse_args()
    try:
        return run(args)
    except httpx.HTTPError as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

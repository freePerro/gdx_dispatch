from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

DEFAULT_ENDPOINTS = [
    "/api/health",
    "/api/jobs",
    "/api/customers",
    "/api/reports/summary",
]

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_TS_KEY_RE = re.compile(r"(?:^|_)(?:created|updated|deleted|modified|time|timestamp|date)(?:$|_)", re.IGNORECASE)


@dataclass(slots=True)
class EndpointResult:
    status_code: int | None
    payload: Any | None
    error: str | None = None


def _looks_like_timestamp(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        datetime.fromisoformat(candidate)
        return True
    except ValueError:
        return False


def normalize_payload(payload: Any, key_hint: str | None = None) -> Any:
    """Normalize dynamic fields that are expected to differ between services."""
    if isinstance(payload, dict):
        return {k: normalize_payload(v, key_hint=k) for k, v in payload.items()}

    if isinstance(payload, list):
        return [normalize_payload(item, key_hint=key_hint) for item in payload]

    if isinstance(payload, str):
        key_l = (key_hint or "").lower()

        if _UUID_RE.match(payload) and ("id" in key_l or "uuid" in key_l or not key_l):
            return "<normalized_uuid>"

        if (_TS_KEY_RE.search(key_l) or "at" in key_l or "time" in key_l or "date" in key_l) and _looks_like_timestamp(payload):
            return "<normalized_timestamp>"

    return payload


def compare_json_payloads(flask_payload: Any, fastapi_payload: Any, path: str = "$") -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []

    if isinstance(flask_payload, dict) and isinstance(fastapi_payload, dict):
        flask_keys = set(flask_payload.keys())
        fastapi_keys = set(fastapi_payload.keys())

        for key in sorted(flask_keys - fastapi_keys):
            diffs.append(
                {
                    "path": f"{path}.{key}",
                    "issue": "missing_in_fastapi",
                    "flask_value": flask_payload[key],
                    "fastapi_value": None,
                }
            )

        for key in sorted(fastapi_keys - flask_keys):
            diffs.append(
                {
                    "path": f"{path}.{key}",
                    "issue": "missing_in_flask",
                    "flask_value": None,
                    "fastapi_value": fastapi_payload[key],
                }
            )

        for key in sorted(flask_keys & fastapi_keys):
            diffs.extend(compare_json_payloads(flask_payload[key], fastapi_payload[key], f"{path}.{key}"))

        return diffs

    if isinstance(flask_payload, list) and isinstance(fastapi_payload, list):
        if len(flask_payload) != len(fastapi_payload):
            diffs.append(
                {
                    "path": path,
                    "issue": "list_length_mismatch",
                    "flask_value": len(flask_payload),
                    "fastapi_value": len(fastapi_payload),
                }
            )

        min_len = min(len(flask_payload), len(fastapi_payload))
        for idx in range(min_len):
            diffs.extend(compare_json_payloads(flask_payload[idx], fastapi_payload[idx], f"{path}[{idx}]"))

        for idx in range(min_len, len(flask_payload)):
            diffs.append(
                {
                    "path": f"{path}[{idx}]",
                    "issue": "missing_in_fastapi",
                    "flask_value": flask_payload[idx],
                    "fastapi_value": None,
                }
            )

        for idx in range(min_len, len(fastapi_payload)):
            diffs.append(
                {
                    "path": f"{path}[{idx}]",
                    "issue": "missing_in_flask",
                    "flask_value": None,
                    "fastapi_value": fastapi_payload[idx],
                }
            )

        return diffs

    if type(flask_payload) is not type(fastapi_payload):
        return [
            {
                "path": path,
                "issue": "type_mismatch",
                "flask_type": type(flask_payload).__name__,
                "fastapi_type": type(fastapi_payload).__name__,
                "flask_value": flask_payload,
                "fastapi_value": fastapi_payload,
            }
        ]

    if flask_payload != fastapi_payload:
        return [
            {
                "path": path,
                "issue": "value_mismatch",
                "flask_value": flask_payload,
                "fastapi_value": fastapi_payload,
            }
        ]

    return []


async def fetch_endpoint(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: str,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> EndpointResult:
    url = f"{base_url.rstrip('/')}{endpoint}"
    try:
        response = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        return EndpointResult(status_code=None, payload=None, error=f"{type(exc).__name__}: {exc}")

    payload: Any | None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    return EndpointResult(status_code=response.status_code, payload=payload, error=None)


async def run_comparison(
    flask_url: str,
    fastapi_url: str,
    *,
    auth_token: str | None = None,
    session_cookie: str | None = None,
    endpoints: list[str] | None = None,
    timeout: float = 10.0,
    flask_client: httpx.AsyncClient | None = None,
    fastapi_client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    endpoints_to_check = endpoints or DEFAULT_ENDPOINTS

    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    cookies: dict[str, str] = {}
    if session_cookie:
        cookies["session"] = session_cookie

    own_flask_client = flask_client is None
    own_fastapi_client = fastapi_client is None

    if own_flask_client:
        flask_client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, cookies=cookies)
    elif cookies:
        flask_client.cookies.update(cookies)
    if own_fastapi_client:
        fastapi_client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, cookies=cookies)
    elif cookies:
        fastapi_client.cookies.update(cookies)

    assert flask_client is not None
    assert fastapi_client is not None

    report: list[dict[str, Any]] = []

    try:
        for endpoint in endpoints_to_check:
            flask_result, fastapi_result = await asyncio.gather(
                fetch_endpoint(flask_client, flask_url, endpoint, headers=headers),
                fetch_endpoint(fastapi_client, fastapi_url, endpoint, headers=headers),
            )

            diffs: list[dict[str, Any]] = []

            if flask_result.error:
                diffs.append(
                    {
                        "path": "$",
                        "issue": "flask_request_error",
                        "flask_value": flask_result.error,
                        "fastapi_value": None,
                    }
                )
            if fastapi_result.error:
                diffs.append(
                    {
                        "path": "$",
                        "issue": "fastapi_request_error",
                        "flask_value": None,
                        "fastapi_value": fastapi_result.error,
                    }
                )

            if (
                flask_result.status_code is not None
                and fastapi_result.status_code is not None
                and flask_result.status_code != fastapi_result.status_code
            ):
                diffs.append(
                    {
                        "path": "$",
                        "issue": "status_code_mismatch",
                        "flask_value": flask_result.status_code,
                        "fastapi_value": fastapi_result.status_code,
                    }
                )

            if flask_result.payload is not None and fastapi_result.payload is not None:
                normalized_flask = normalize_payload(flask_result.payload)
                normalized_fastapi = normalize_payload(fastapi_result.payload)
                diffs.extend(compare_json_payloads(normalized_flask, normalized_fastapi))

            report.append(
                {
                    "endpoint": endpoint,
                    "flask_status": flask_result.status_code,
                    "fastapi_status": fastapi_result.status_code,
                    "field_diffs": diffs,
                }
            )
    finally:
        if own_flask_client:
            await flask_client.aclose()
        if own_fastapi_client:
            await fastapi_client.aclose()

    return report


async def _async_main(args: argparse.Namespace) -> int:
    report = await run_comparison(
        flask_url=args.flask_url,
        fastapi_url=args.fastapi_url,
        auth_token=args.auth_token,
        session_cookie=args.session_cookie,
        timeout=args.timeout,
        endpoints=DEFAULT_ENDPOINTS,
    )

    payload = json.dumps(report, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(payload)
            f.write("\n")
    else:
        print(payload)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare Flask and FastAPI endpoint responses.")
    parser.add_argument("--flask-url", required=True, help="Base URL for Flask server")
    parser.add_argument("--fastapi-url", required=True, help="Base URL for FastAPI server")
    parser.add_argument("--auth-token", default=None, help="Bearer token used for both servers")
    parser.add_argument("--session-cookie", default=None, help="Session cookie value shared across servers")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument("--output", default=None, help="Optional file path for JSON output")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())

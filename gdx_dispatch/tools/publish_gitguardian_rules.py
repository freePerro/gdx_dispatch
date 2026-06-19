"""Publish GDX PAT prefix regex rules to GitGuardian (SS-14 slice G).

# the PAT prefix taxonomy changes. It is NOT wired into gdx_dispatch/main.py —
# it's an out-of-band operator tool.

GitGuardian custom-detector API reference:
    https://api.gitguardian.com/docs#tag/Custom-detectors

The real endpoint is ``POST /v1/custom-detectors`` with an API key in
the ``Authorization: Token <key>`` header. Because this repo does not
ship with a GitGuardian API key by default, the HTTP call is performed
by a swappable transport callable so the tool can run in "dry-run" mode
(logging the payload) in environments that have no key, and exercise the
real call only when ``GITGUARDIAN_API_KEY`` is present.

Invoke:
    python -m gdx_dispatch.tools.publish_gitguardian_rules
    python -m gdx_dispatch.tools.publish_gitguardian_rules --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

GITGUARDIAN_ENDPOINT = "https://api.gitguardian.com/v1/custom-detectors"


@dataclass(frozen=True)
class DetectorRule:
    """One custom-detector rule submitted to GitGuardian."""

    name: str
    display_name: str
    regex: str
    description: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "regex": self.regex,
            "description": self.description,
        }


# GDX PAT prefix taxonomy (SS-14). The regex anchors on the fixed
# prefix plus 43 base64url chars (token_urlsafe(32) ≈ 43 chars).
PAT_DETECTORS: tuple[DetectorRule, ...] = (
    DetectorRule(
        name="gdx-pat-live",
        display_name="GDX live Personal Access Token",
        regex=r"gdx_pat_live_[A-Za-z0-9_\-]{30,}",
        description="GDX production PAT. Treat as a credential.",
    ),
    DetectorRule(
        name="gdx-pat-test",
        display_name="GDX test Personal Access Token",
        regex=r"gdx_pat_test_[A-Za-z0-9_\-]{30,}",
        description="GDX sandbox/test PAT. Treat as a credential.",
    ),
    DetectorRule(
        name="gdx-sk-live",
        display_name="GDX live Secret Key",
        regex=r"gdx_sk_live_[A-Za-z0-9_\-]{30,}",
        description="GDX live service key. Treat as a credential.",
    ),
    DetectorRule(
        name="gdx-sk-test",
        display_name="GDX test Secret Key",
        regex=r"gdx_sk_test_[A-Za-z0-9_\-]{30,}",
        description="GDX test service key. Treat as a credential.",
    ),
)


# A transport callable: (url, headers, json_body) -> (status_code, body_dict).
# The default transport uses ``urllib`` so there is no hard dep on ``httpx``
# or ``requests`` — keeping the tool importable in any environment.
Transport = Callable[[str, dict[str, str], dict[str, Any]], tuple[int, dict[str, Any]]]


def _urllib_transport(
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Default stdlib-only POST transport."""
    import urllib.error
    import urllib.request

    data = json.dumps(json_body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"raw": body}
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        # Surface the HTTP error with its response body so the caller
        # can log a meaningful failure.
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed


def publish_rules(
    rules: tuple[DetectorRule, ...] = PAT_DETECTORS,
    *,
    api_key: str | None = None,
    transport: Transport | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Publish every rule in ``rules`` to GitGuardian.

    Returns a list of result dicts — one per rule — with keys
    ``name``, ``status``, ``http_status`` (or ``dry_run``: True), and
    ``response``.

    ``dry_run=True`` short-circuits the HTTP call and returns the
    payload that would have been sent. If ``api_key`` is omitted the
    tool falls back to the ``GITGUARDIAN_API_KEY`` env var; if that is
    also absent, it forces ``dry_run=True`` and logs the decision.
    """
    if api_key is None:
        api_key = os.environ.get("GITGUARDIAN_API_KEY", "").strip() or None

    effective_dry_run = dry_run or api_key is None
    if effective_dry_run and not dry_run:
        log.info("no GITGUARDIAN_API_KEY set — forcing dry-run")

    results: list[dict[str, Any]] = []

    for rule in rules:
        payload = rule.to_payload()
        if effective_dry_run:
            log.info("gitguardian_publish_dry_run name=%s", rule.name)
            results.append(
                {
                    "name": rule.name,
                    "status": "dry_run",
                    "http_status": None,
                    "dry_run": True,
                    "payload": payload,
                }
            )
            continue

        xport = transport or _urllib_transport
        headers = {"Authorization": f"Token {api_key}"}
        try:
            status_code, body = xport(GITGUARDIAN_ENDPOINT, headers, payload)
        except Exception as exc:
            log.exception("gitguardian_publish_failed name=%s", rule.name)
            results.append(
                {
                    "name": rule.name,
                    "status": "error",
                    "http_status": None,
                    "error": str(exc),
                }
            )
            continue

        ok = 200 <= status_code < 300
        results.append(
            {
                "name": rule.name,
                "status": "ok" if ok else "error",
                "http_status": status_code,
                "response": body,
            }
        )
        if not ok:
            log.warning(
                "gitguardian_publish_non_2xx name=%s status=%s",
                rule.name,
                status_code,
            )

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish GDX PAT detector rules to GitGuardian.")
    parser.add_argument("--dry-run", action="store_true", help="Log payloads without calling GitGuardian.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = publish_rules(dry_run=args.dry_run)

    failures = [r for r in results if r["status"] == "error"]
    print(json.dumps(results, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

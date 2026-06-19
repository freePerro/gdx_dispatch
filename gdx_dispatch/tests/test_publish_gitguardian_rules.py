"""Tests for gdx_dispatch.tools.publish_gitguardian_rules (SS-14 slice G)."""
from __future__ import annotations

import re

import pytest

from gdx_dispatch.tools.publish_gitguardian_rules import (
    PAT_DETECTORS,
    DetectorRule,
    publish_rules,
)


def test_pat_detectors_cover_all_four_prefixes():
    names = {d.name for d in PAT_DETECTORS}
    assert names == {"gdx-pat-live", "gdx-pat-test", "gdx-sk-live", "gdx-sk-test"}


def test_pat_detectors_regexes_match_real_tokens():
    # Simulate a token_urlsafe(32) body — 43 chars of base64url.
    body = "A" * 40 + "_-x"
    for rule in PAT_DETECTORS:
        prefix = rule.name.replace("-", "_") + "_"
        token = f"{prefix}{body}"
        assert re.search(rule.regex, token), f"{rule.name} regex missed own prefix"


def test_pat_detectors_regexes_do_not_match_unrelated_strings():
    unrelated = ["github_pat_abc", "sk_live_abc123", "ghp_xxxxx", "random"]
    for s in unrelated:
        for rule in PAT_DETECTORS:
            assert not re.search(rule.regex, s), (
                f"{rule.name} regex false-positive on {s!r}"
            )


def test_publish_rules_dry_run_returns_payloads_without_calling_transport():
    calls = []

    def transport(url, headers, body):
        calls.append((url, headers, body))
        return 200, {}

    results = publish_rules(transport=transport, dry_run=True)
    assert len(results) == 4
    for r in results:
        assert r["status"] == "dry_run"
        assert r["dry_run"] is True
        assert r["http_status"] is None
        # Payload round-trips the rule fields.
        assert {"name", "regex", "display_name", "description"} <= set(r["payload"])
    assert calls == []


def test_publish_rules_no_api_key_env_forces_dry_run(monkeypatch):
    monkeypatch.delenv("GITGUARDIAN_API_KEY", raising=False)
    calls = []

    def transport(url, headers, body):
        calls.append((url, headers, body))
        return 200, {}

    results = publish_rules(transport=transport)
    assert calls == []
    for r in results:
        assert r["status"] == "dry_run"


def test_publish_rules_with_api_key_invokes_transport(monkeypatch):
    monkeypatch.setenv("GITGUARDIAN_API_KEY", "test-key")
    calls = []

    def transport(url, headers, body):
        calls.append((url, headers, body))
        return 201, {"id": body["name"]}

    results = publish_rules(transport=transport)
    assert len(calls) == 4
    for (url, headers, body), result in zip(calls, results, strict=True):
        assert url.startswith("https://api.gitguardian.com")
        assert headers["Authorization"] == "Token test-key"
        assert result["status"] == "ok"
        assert result["http_status"] == 201


def test_publish_rules_surfaces_non_2xx_failures(monkeypatch):
    monkeypatch.setenv("GITGUARDIAN_API_KEY", "test-key")

    def transport(url, headers, body):
        return 400, {"detail": "bad regex"}

    results = publish_rules(transport=transport)
    for r in results:
        assert r["status"] == "error"
        assert r["http_status"] == 400
        assert r["response"]["detail"] == "bad regex"


def test_publish_rules_surfaces_transport_exception(monkeypatch):
    monkeypatch.setenv("GITGUARDIAN_API_KEY", "test-key")

    def transport(url, headers, body):
        raise ConnectionError("dns failed")

    results = publish_rules(transport=transport)
    for r in results:
        assert r["status"] == "error"
        assert "dns failed" in r["error"]


def test_detector_rule_payload_shape():
    rule = DetectorRule(
        name="x",
        display_name="X",
        regex=r"x_[a-z]+",
        description="desc",
    )
    payload = rule.to_payload()
    assert payload == {
        "name": "x",
        "display_name": "X",
        "regex": r"x_[a-z]+",
        "description": "desc",
    }

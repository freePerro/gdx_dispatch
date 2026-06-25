"""Validation tests for uploaded plugin artifacts (ADR-013 file-install).

`safe_artifact_name` is the trust boundary: it decides what filename an uploaded
wheel/sdist gets written to on disk, so it must strip paths and reject anything
that isn't a wheel/sdist (no traversal, no executable scripts).
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")

from gdx_dispatch.plugin_host.reconcile import safe_artifact_name


def test_accepts_wheel():
    n = "gdx_plugin_chi_pricing-0.1.0-py3-none-any.whl"
    assert safe_artifact_name(n) == n


def test_accepts_sdist():
    assert safe_artifact_name("gdx-plugin-x-1.0.tar.gz") == "gdx-plugin-x-1.0.tar.gz"


def test_strips_directory_to_basename():
    # a path is reduced to its safe basename, never written where it points
    assert safe_artifact_name("/etc/cron.d/evil.whl") == "evil.whl"


def test_rejects_traversal():
    assert safe_artifact_name("../../etc/passwd") is None
    assert safe_artifact_name("../secrets.tar.gz") == "secrets.tar.gz"  # basename only


def test_rejects_wrong_extension():
    assert safe_artifact_name("evil.sh") is None
    assert safe_artifact_name("payload.py") is None
    assert safe_artifact_name("archive.zip") is None


def test_rejects_empty_or_none():
    assert safe_artifact_name("") is None
    assert safe_artifact_name(None) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("ALL PASS")

"""Sprint 3 — first-boot secret minting for the n8n runtime.

Verifies the redis password (→ REDIS_URL) and the n8n secret files land correctly
and stay stable across reruns. mint_runtime_env is stdlib-only, so this runs
without the app.
"""
from __future__ import annotations

import gdx_dispatch.tools.mint_runtime_env as m


def _run(secrets_dir, n8n_dir, monkeypatch):
    monkeypatch.setenv("GDX_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("GDX_N8N_SECRETS_DIR", str(n8n_dir))
    # Non-root in the test runner → the chown/chmod block is skipped (guarded by
    # geteuid == 0), which is fine; we assert file *contents*, not ownership.
    assert m.main() == 0


def test_mints_redis_password_into_url(tmp_path, monkeypatch):
    s, n = tmp_path / "s", tmp_path / "n8n"
    s.mkdir(); n.mkdir()
    _run(s, n, monkeypatch)

    env = (s / "runtime.env").read_text()
    lines = dict(l.split("=", 1) for l in env.splitlines() if "=" in l and not l.startswith("#"))
    assert lines["REDIS_URL"].startswith("redis://:")  # password embedded
    assert lines["REDIS_URL"].endswith("@redis:6379/0")
    assert (s / "redis_password").exists()
    # the bare file matches the password inside REDIS_URL
    bare = (s / "redis_password").read_text().strip()
    assert bare and bare in lines["REDIS_URL"]


def test_n8n_key_file_has_no_trailing_newline(tmp_path, monkeypatch):
    s, n = tmp_path / "s", tmp_path / "n8n"
    s.mkdir(); n.mkdir()
    _run(s, n, monkeypatch)

    key = (n / "encryption_key").read_bytes()
    assert len(key) == 64  # token_hex(32)
    assert not key.endswith(b"\n")  # n8n reads it untrimmed — a newline corrupts it
    dbp = (n / "db_password").read_bytes()
    assert dbp and not dbp.endswith(b"\n")  # postgres strips, n8n doesn't → must agree


def test_minting_is_idempotent(tmp_path, monkeypatch):
    s, n = tmp_path / "s", tmp_path / "n8n"
    s.mkdir(); n.mkdir()
    _run(s, n, monkeypatch)
    env1 = (s / "runtime.env").read_text()
    key1 = (n / "encryption_key").read_bytes()
    _run(s, n, monkeypatch)  # rerun — must reuse persisted values
    assert (s / "runtime.env").read_text() == env1
    assert (n / "encryption_key").read_bytes() == key1


def test_n8n_dir_unset_skips_n8n_files(tmp_path, monkeypatch):
    # dev/prod don't set GDX_N8N_SECRETS_DIR → no n8n files written, no crash.
    s = tmp_path / "s"; s.mkdir()
    monkeypatch.setenv("GDX_SECRETS_DIR", str(s))
    monkeypatch.delenv("GDX_N8N_SECRETS_DIR", raising=False)
    assert m.main() == 0
    assert (s / "runtime.env").exists()
    assert not (s / "encryption_key").exists()

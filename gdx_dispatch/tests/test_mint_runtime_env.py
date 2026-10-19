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
    s.mkdir()
    n.mkdir()
    _run(s, n, monkeypatch)

    env = (s / "runtime.env").read_text()
    lines = dict(ln.split("=", 1) for ln in env.splitlines() if "=" in ln and not ln.startswith("#"))
    assert lines["REDIS_URL"].startswith("redis://:")  # password embedded
    assert lines["REDIS_URL"].endswith("@redis:6379/0")
    assert (s / "redis_password").exists()
    # the bare file matches the password inside REDIS_URL
    bare = (s / "redis_password").read_text().strip()
    assert bare and bare in lines["REDIS_URL"]


def test_n8n_key_file_has_no_trailing_newline(tmp_path, monkeypatch):
    s, n = tmp_path / "s", tmp_path / "n8n"
    s.mkdir()
    n.mkdir()
    _run(s, n, monkeypatch)

    key = (n / "encryption_key").read_bytes()
    assert len(key) == 64  # token_hex(32)
    assert not key.endswith(b"\n")  # n8n reads it untrimmed — a newline corrupts it
    dbp = (n / "db_password").read_bytes()
    assert dbp and not dbp.endswith(b"\n")  # postgres strips, n8n doesn't → must agree


def test_minting_is_idempotent(tmp_path, monkeypatch):
    s, n = tmp_path / "s", tmp_path / "n8n"
    s.mkdir()
    n.mkdir()
    _run(s, n, monkeypatch)
    env1 = (s / "runtime.env").read_text()
    key1 = (n / "encryption_key").read_bytes()
    _run(s, n, monkeypatch)  # rerun — must reuse persisted values
    assert (s / "runtime.env").read_text() == env1
    assert (n / "encryption_key").read_bytes() == key1


def test_n8n_dir_unset_skips_n8n_files(tmp_path, monkeypatch):
    # dev/prod don't set GDX_N8N_SECRETS_DIR → no n8n files written, no crash.
    s = tmp_path / "s"
    s.mkdir()
    monkeypatch.setenv("GDX_SECRETS_DIR", str(s))
    monkeypatch.delenv("GDX_N8N_SECRETS_DIR", raising=False)
    assert m.main() == 0
    assert (s / "runtime.env").exists()
    assert not (s / "encryption_key").exists()


def _base_urls(secrets_dir):
    env = (secrets_dir / "runtime.env").read_text()
    lines = dict(ln.split("=", 1) for ln in env.splitlines() if "=" in ln and not ln.startswith("#"))
    return lines.get("GDX_PUBLIC_BASE_URL"), lines.get("GDX_BASE_URL")


def _fresh(tmp_path, monkeypatch):
    s = tmp_path / "s"
    s.mkdir()
    monkeypatch.setenv("GDX_SECRETS_DIR", str(s))
    monkeypatch.delenv("GDX_N8N_SECRETS_DIR", raising=False)
    for key in ("GDX_PUBLIC_BASE_URL", "GDX_BASE_URL", "GDX_DOMAIN"):
        monkeypatch.delenv(key, raising=False)
    return s


def test_public_ip_gives_an_https_base_url_and_a_domain_wins(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch)
    monkeypatch.setenv("GDX_PUBLIC_IP", "203.0.113.7")
    assert m.main() == 0
    assert _base_urls(s) == ("https://203.0.113.7",) * 2

    monkeypatch.setenv("GDX_DOMAIN", "dispatch.example.com")
    assert m.main() == 0
    assert _base_urls(s) == ("https://dispatch.example.com",) * 2


def test_clearing_the_public_ip_drops_the_derived_url_but_keeps_a_domain_one(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch)
    monkeypatch.setenv("GDX_PUBLIC_IP", "203.0.113.7")
    assert m.main() == 0
    # The IP certificate failed, so install.sh cleared the address.
    monkeypatch.setenv("GDX_PUBLIC_IP", "")
    assert m.main() == 0
    assert _base_urls(s) == (None, None)

    # A stored domain URL (the variable since removed) is still kept, as before.
    monkeypatch.setenv("GDX_DOMAIN", "dispatch.example.com")
    assert m.main() == 0
    monkeypatch.delenv("GDX_DOMAIN")
    assert m.main() == 0
    assert _base_urls(s) == ("https://dispatch.example.com",) * 2

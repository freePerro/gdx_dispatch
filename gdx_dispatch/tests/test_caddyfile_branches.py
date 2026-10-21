"""The Caddyfile the caddy service writes for each way an install is reached.

The base compose file's caddy command is a shell script that writes the
Caddyfile from the environment. Since 2026-10-19 an install with no domain gets
HTTPS on its public IP: install.sh sets GDX_PUBLIC_IP and Caddy asks Let's
Encrypt for an IP certificate, which LE issues only under the `shortlived`
profile. A browser sends no SNI for an IP, so without `default_sni` Caddy
would not pick that certificate. Over plain HTTP the browser drops the app's
Secure refresh cookie and every login ends at access-token expiry.

These run the compose file's own script under sh, with the Caddyfile path
pointed at a temp file and `exec caddy run` cut off. `caddy adapt` on each
output was checked by hand against caddy:2-alpine v2.11.7.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = REPO_ROOT / "gdx_dispatch" / "docker" / "docker-compose.yml"


def _caddyfile(tmp_path: Path, **env: str) -> str:
    command = yaml.safe_load(BASE.read_text())["services"]["caddy"]["command"]
    script = command[-1] if isinstance(command, list) else command
    # sh -c '<script>': compose turns $$ into $.
    script = script.split("'", 1)[1].rsplit("'", 1)[0].replace("$$", "$")
    head, sep, _ = script.partition("exec caddy run")
    assert sep, "the caddy command no longer ends in exec caddy run"
    out = tmp_path / "Caddyfile"
    head = head.replace("/etc/caddy/Caddyfile", str(out))
    full_env = {"PATH": "/usr/bin:/bin", "GDX_DOMAIN": "", "GDX_PUBLIC_IP": "", "GDX_N8N_ENABLED": "", **env}
    subprocess.run(["sh", "-c", head], env=full_env, check=True, capture_output=True, timeout=10)
    return out.read_text()


def _blocks(text: str) -> list[str]:
    """The first line of each top-level block."""
    return [line.rstrip(" {") for line in text.splitlines() if line.endswith("{") and not line.startswith(" ")]


def test_no_domain_and_no_ip_is_plain_http(tmp_path):
    assert _caddyfile(tmp_path) == ":80 {\n  reverse_proxy app:8000\n}\n"


def test_a_public_ip_gets_a_shortlived_certificate_picked_without_sni(tmp_path):
    text = _caddyfile(tmp_path, GDX_PUBLIC_IP="203.0.113.7")
    assert _blocks(text) == ["", "203.0.113.7", "http://"]
    assert "  default_sni 203.0.113.7\n" in text
    assert "    issuer acme {\n      profile shortlived\n    }\n" in text
    assert "  reverse_proxy app:8000\n" in text


def test_the_ip_branch_still_serves_other_host_names_over_plain_http(tmp_path):
    # Without the catch-all, Caddy's own redirect sends EVERY Host on :80 to
    # https, where only the IP has a certificate: a LAN, Tailscale or host name
    # that worked over HTTP then fails (measured on caddy:2-alpine 2026-10-19:
    # with it, the IP gets 308 to https and dispatch.lan, 192.168.1.50 and
    # 100.101.1.2 get 200 over HTTP).
    text = _caddyfile(tmp_path, GDX_PUBLIC_IP="203.0.113.7")
    assert text.endswith("http:// {\n  reverse_proxy app:8000\n}\n")


def test_a_domain_wins_over_the_ip_and_keeps_its_n8n_block(tmp_path):
    text = _caddyfile(tmp_path, GDX_DOMAIN="d.example.com", GDX_PUBLIC_IP="203.0.113.7")
    assert text == "d.example.com {\n  reverse_proxy app:8000\n}\n"
    text = _caddyfile(tmp_path, GDX_DOMAIN="d.example.com", GDX_PUBLIC_IP="203.0.113.7", GDX_N8N_ENABLED="1")
    assert _blocks(text) == ["d.example.com", "n8n.d.example.com"]


def test_the_ip_reaches_both_caddy_and_the_runtime_env_minting():
    spec = yaml.safe_load(BASE.read_text())
    assert spec["services"]["caddy"]["environment"]["GDX_PUBLIC_IP"] == "${GDX_PUBLIC_IP:-}"
    # secrets-init merges the shared app env, where mint_runtime_env reads it.
    assert spec["services"]["secrets-init"]["environment"]["GDX_PUBLIC_IP"] == "${GDX_PUBLIC_IP:-}"

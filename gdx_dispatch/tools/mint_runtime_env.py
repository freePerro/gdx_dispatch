"""First-boot runtime-env minting for one-click self-host deploys.

docker-compose.customer.yml runs this as the `secrets-init` service before
anything else starts. It makes a zero-env `docker compose up` come up with
real, stable secrets instead of dev defaults:

  1. Every managed secret (SECRET_KEY, JWT_SECRET, encryption keys, DB
     password) is taken from the container environment when the operator set
     one, otherwise from the previously persisted file, otherwise freshly
     generated.
  2. The result is persisted to <secrets dir>/runtime.env on a named volume.
     app/celery/plugin-host entrypoints load that file (GDX_RUNTIME_ENV_FILE),
     so every container agrees on every key across restarts and redeploys —
     an unpersisted key rotation would invalidate sessions and make
     EncryptedString columns unreadable.
  3. The DB password is additionally written bare to <secrets dir>/db_password
     for postgres' POSTGRES_PASSWORD_FILE.

Derived, non-secret convenience values: DATABASE_URL is built from the DB
password; GDX_PUBLIC_BASE_URL / GDX_BASE_URL are derived from GDX_DOMAIN when
the operator didn't set them explicitly, and re-derived when the domain
changes (redeploy with a new GDX_DOMAIN just works).

The DB password is the one key where a *stored* value beats the environment:
postgres fixes the superuser password into its data directory on first init,
so honoring a later env change would only break DATABASE_URL. A conflicting
env value logs a warning and is ignored.

Stdlib only — this runs before the app imports its settings (whose production
gates would refuse to boot on exactly the unset keys this script exists to fill).
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets as pysecrets
import sys
from urllib.parse import quote

log = logging.getLogger("gdx_dispatch.mint_runtime_env")

RUNTIME_ENV_NAME = "runtime.env"
DB_PASSWORD_NAME = "db_password"
REDIS_PASSWORD_NAME = "redis_password"
# n8n reads its encryption key + DB password from files via *_FILE env vars,
# written to a SEPARATE volume (gdx_n8n_secrets) WITHOUT a trailing newline (n8n
# uses file contents untrimmed). n8n mounts ONLY that volume, never gdx_secrets,
# so its Code node cannot read the GDX secret set — that is the isolation that
# matters. (The key values are ALSO in runtime.env on gdx_secrets, because
# they're MANAGED_SECRETS and persistence/idempotency reads back only from
# runtime.env; that copy lives in the TRUSTED GDX containers, which n8n can't
# reach, so it doesn't widen n8n's access.) Only materialized when the dir is set.
N8N_SECRETS_DIR_ENV = "GDX_N8N_SECRETS_DIR"
N8N_KEY_NAME = "encryption_key"

# The file is consumed by plain `set -a; . file` in entrypoint.sh, so every
# value must be shell-source-safe. Minted values are by construction (hex /
# urlsafe-b64); OPERATOR-supplied values are validated against this charset
# and rejected loudly otherwise — a value with spaces/quotes/$ would otherwise
# crash-loop every container's entrypoint, and a DB password with URL
# metacharacters would silently corrupt the derived DATABASE_URL.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/@%+=,-]*$")


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


# key -> generator. Distinct keys on purpose: they feed different encryption
# paths in the app and rotate independently (see .env.template).
MANAGED_SECRETS = {
    "SECRET_KEY": lambda: pysecrets.token_hex(32),
    "JWT_SECRET": lambda: pysecrets.token_hex(32),  # HS256 needs >= 32 bytes
    "MASTER_ENCRYPTION_KEY": _fernet_key,
    "GDX_FERNET_KEY": _fernet_key,
    "FERNET_KEY": _fernet_key,
    "DB_PASSWORD": lambda: pysecrets.token_urlsafe(24),  # URL-safe: goes in DATABASE_URL
    # Shared secret gating plugin-host's /internal/* routes. Injected into app +
    # plugin-host (both load runtime.env), never into n8n. Enforcement is
    # opt-in: plugin-host only checks it when this is set (staged rollout).
    "GDX_INTERNAL_TOKEN": lambda: pysecrets.token_hex(32),
    # Redis --requirepass. URL-safe so it drops cleanly into REDIS_URL. The redis
    # service reads the bare value from the redis_password file; app/celery get it
    # via the derived REDIS_URL below. Kept out of n8n's reach (isolated network +
    # no password file mounted there).
    "REDIS_PASSWORD": lambda: pysecrets.token_urlsafe(24),
    # n8n's data-at-rest encryption key. Persisted (a rotation orphans every
    # stored n8n credential). Written to its own file too (see below).
    "N8N_ENCRYPTION_KEY": lambda: pysecrets.token_hex(32),
    # Password for n8n's OWN isolated postgres (n8n-db). token_hex (no URL
    # metachars) since it's passed as a file to both n8n-db and n8n.
    "N8N_DB_PASSWORD": lambda: pysecrets.token_hex(24),
}


def _read_env_file(path: str) -> dict[str, str]:
    stored: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                stored[key] = value
    except FileNotFoundError:
        pass
    return stored


def _write_atomic(path: str, content: str) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception:
        os.unlink(tmp)
        raise
    os.replace(tmp, path)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

    secrets_dir = os.getenv("GDX_SECRETS_DIR", "/secrets")
    os.makedirs(secrets_dir, exist_ok=True)
    env_path = os.path.join(secrets_dir, RUNTIME_ENV_NAME)
    stored = _read_env_file(env_path)

    resolved: dict[str, str] = {}
    minted: list[str] = []
    # `var_name` is the environment-variable NAME (e.g. "JWT_SECRET"), never
    # its value — only names reach the log lines below.
    for var_name, generate in MANAGED_SECRETS.items():
        env_value = os.getenv(var_name, "").strip()
        if env_value and not _SAFE_VALUE.fullmatch(env_value):
            log.error(
                "%s contains characters that are not safe for the runtime env "
                "file (allowed: letters, digits, _.:/@%%+=,-). Set a simpler "
                "value or leave it blank to have one generated.",
                var_name,
            )
            return 1
        stored_value = stored.get(var_name, "")
        if var_name == "DB_PASSWORD" and stored_value:
            # Postgres baked the stored password into its data dir on first
            # init — a changed env value can't take effect and would only
            # desync DATABASE_URL.
            if env_value and env_value != stored_value:
                log.warning(
                    "DB_PASSWORD env differs from the persisted value; keeping the "
                    "persisted one (postgres fixed it at first init)."
                )
            resolved[var_name] = stored_value
        elif env_value:
            resolved[var_name] = env_value
        elif stored_value:
            resolved[var_name] = stored_value
        else:
            resolved[var_name] = generate()
            minted.append(var_name)

    # ── Derived, non-secret values ──
    # Percent-encode the password: SQLAlchemy URL-decodes it back to the raw
    # value postgres knows (the bare db_password file), so characters like @
    # or / in an operator-chosen password can't corrupt the URL.
    resolved["DATABASE_URL"] = (
        f"postgresql://gdx:{quote(resolved['DB_PASSWORD'], safe='')}@db:5432/gdx"
    )
    # REDIS_URL carries the minted password so app/celery/plugin-host authenticate
    # to the --requirepass'd redis. Overrides the passwordless compose default
    # (runtime.env is sourced after the container env). Percent-encode like the
    # DB password so URL metacharacters can't corrupt the URL.
    resolved["REDIS_URL"] = (
        f"redis://:{quote(resolved['REDIS_PASSWORD'], safe='')}@redis:6379/0"
    )

    domain = os.getenv("GDX_DOMAIN", "").strip()
    for url_key in ("GDX_PUBLIC_BASE_URL", "GDX_BASE_URL"):
        env_value = os.getenv(url_key, "").strip()
        if env_value:
            resolved[url_key] = env_value
        elif domain:
            resolved[url_key] = f"https://{domain}"
        elif stored.get(url_key):
            resolved[url_key] = stored[url_key]

    lines = [
        "# Generated by gdx_dispatch.tools.mint_runtime_env — do not edit by hand.",
        "# Loaded by entrypoint.sh via GDX_RUNTIME_ENV_FILE in every app container.",
    ]
    lines += [f"{key}={value}" for key, value in sorted(resolved.items())]
    _write_atomic(env_path, "\n".join(lines) + "\n")
    _write_atomic(os.path.join(secrets_dir, DB_PASSWORD_NAME), resolved["DB_PASSWORD"] + "\n")
    # Bare redis password for `redis-server --requirepass "$(cat …)"` (command
    # substitution strips the trailing newline, so it matches REDIS_URL's copy).
    _write_atomic(os.path.join(secrets_dir, REDIS_PASSWORD_NAME), resolved["REDIS_PASSWORD"] + "\n")

    # n8n encryption key → its OWN volume (never gdx_secrets), NO trailing newline
    # (n8n uses the file contents untrimmed). Only when the dir is mounted (the
    # customer compose sets GDX_N8N_SECRETS_DIR); dev/prod skip it.
    n8n_dir = os.getenv(N8N_SECRETS_DIR_ENV, "").strip()
    n8n_key_path = None
    n8n_files: list[str] = []
    if n8n_dir:
        os.makedirs(n8n_dir, exist_ok=True)
        n8n_key_path = os.path.join(n8n_dir, N8N_KEY_NAME)
        # Both files newline-free: n8n reads _FILE contents untrimmed, and
        # postgres' POSTGRES_PASSWORD_FILE strips a trailing newline — so a
        # newline would make n8n and n8n-db disagree on the DB password.
        _write_atomic(n8n_key_path, resolved["N8N_ENCRYPTION_KEY"])
        _write_atomic(os.path.join(n8n_dir, DB_PASSWORD_NAME), resolved["N8N_DB_PASSWORD"])
        n8n_files = [n8n_key_path, os.path.join(n8n_dir, DB_PASSWORD_NAME)]

    # A fresh named volume is root-owned, so the compose file runs this
    # service as root — hand the results over so the non-root consumers can
    # read them. runtime.env must be world-readable (0644): the app image
    # runs as uid 1000 (appuser) but plugin-host runs as the Playwright
    # image's pwuser with a DIFFERENT uid, and both source it. That is not
    # an exposure widening — the volume is only mounted into this stack's
    # containers, whose process env carries the same values anyway.
    # db_password stays 0600: its only readers (postgres init, backup
    # sidecar) run as root.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        uid = int(os.getenv("GDX_SECRETS_OWNER_UID", "1000"))
        gid = int(os.getenv("GDX_SECRETS_OWNER_GID", "1000"))
        os.chmod(secrets_dir, 0o755)
        os.chown(secrets_dir, uid, gid)
        for name in (RUNTIME_ENV_NAME, DB_PASSWORD_NAME, REDIS_PASSWORD_NAME):
            os.chown(os.path.join(secrets_dir, name), uid, gid)
        os.chmod(os.path.join(secrets_dir, RUNTIME_ENV_NAME), 0o644)
        # n8n runs as its image's `node` user (uid 1000, = default owner uid);
        # hand it its secret files (0600 — only n8n reads them) on its own volume.
        # n8n-db (postgres) also reads db_password as root, so 0600 is fine there.
        if n8n_files:
            n8n_uid = int(os.getenv("GDX_N8N_OWNER_UID", "1000"))
            os.chown(n8n_dir, n8n_uid, n8n_uid)
            for f in n8n_files:
                os.chown(f, n8n_uid, n8n_uid)
                os.chmod(f, 0o600)

    if minted:
        log.info("Minted new values for: %s", ", ".join(sorted(minted)))
    else:
        log.info("All managed values already present — nothing minted.")
    log.info("Wrote %s (%d entries).", RUNTIME_ENV_NAME, len(resolved))
    return 0


if __name__ == "__main__":
    sys.exit(main())

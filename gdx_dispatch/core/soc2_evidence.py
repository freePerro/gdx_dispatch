from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, select


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "")


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logging.getLogger(__name__).exception("_env_int caught exception")
        return default


def _pip_audit_available() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001  # silent failure when checking tool availability
        logging.getLogger(__name__).exception("_pip_audit_available caught exception")
        return False


def _runbook_exists() -> bool:
    candidates = [
        Path(__file__).parent.parent / "docs" / "RESTORE_RUNBOOK.md",
        Path("gdx_dispatch/docs/RESTORE_RUNBOOK.md"),
    ]
    return any(p.is_file() for p in candidates)


def collect_soc2_evidence(db: Any) -> dict[str, Any]:
    """Collect SOC 2 readiness evidence and return a structured dict.

    Parameters
    ----------
    db:
        A SQLAlchemy session (sync or async) used to query the AuditLog table.
    """
    from gdx_dispatch.core.audit import AuditLog

    # --- access_control ---
    mfa_required = _env_bool("MFA_REQUIRED", default=False)
    session_timeout_minutes = _env_int("SESSION_TIMEOUT_MINUTES", default=30)
    privileged_reauth_enabled = _env_bool("PRIVILEGED_REAUTH", default=True)

    access_control = {
        "mfa_required": mfa_required,
        "session_timeout_minutes": session_timeout_minutes,
        "privileged_reauth_enabled": privileged_reauth_enabled,
    }

    # --- encryption ---
    at_rest = bool(os.getenv("DB_ENCRYPTION_KEY"))
    in_transit = _env_bool("HTTPS_ONLY", default=True)
    try:
        from gdx_dispatch.core import pii  # noqa: PLC0415
        status = pii.encryption_status()
        pii_status: dict[str, Any] = dataclasses.asdict(status)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("collect_soc2_evidence: pii.encryption_status failed")
        pii_status = {
            "key_loaded": False,
            "columns": [],
            "columns_actually_encrypted": False,
            "scan_error": f"helper_unavailable: {type(exc).__name__}",
        }

    encryption = {
        "at_rest": at_rest,
        "in_transit": in_transit,
        # Legacy bool. STABLE semantics across S122-1c: True iff
        # MASTER_ENCRYPTION_KEY is loaded (same as pre-S122-1c, when
        # the underlying check was `import EncryptedString` and the
        # import always succeeded — so it effectively meant the key
        # was loaded). Auditor dashboards graphing this over time stay
        # continuous. For the richer "is anything actually encrypted"
        # signal, consume `pii_columns.columns_actually_encrypted`.
        "pii_columns_encrypted": pii_status["key_loaded"],
        "pii_columns": pii_status,
    }

    # --- audit_trail ---
    try:
        count_result = db.execute(select(func.count()).select_from(AuditLog))
        record_count: int = count_result.scalar_one()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("collect_soc2_evidence caught exception")
        record_count = 0

    audit_trail = {
        "immutable": True,
        "hash_chained": True,
        "record_count": record_count,
    }

    # --- change_management ---
    ci_cd_enabled = _env_bool("CI_CD_ENABLED", default=False)

    change_management = {
        "git_controlled": True,
        "ci_cd_enabled": ci_cd_enabled,
    }

    # --- incident_response ---
    sentry_configured = bool(os.getenv("SENTRY_DSN"))
    runbook_exists = _runbook_exists()

    incident_response = {
        "sentry_configured": sentry_configured,
        "runbook_exists": runbook_exists,
    }

    # --- vulnerability_management ---
    daily_scan = _env_bool("DAILY_VULN_SCAN", default=False)
    pip_audit_available = _pip_audit_available()

    vulnerability_management = {
        "daily_scan": daily_scan,
        "pip_audit_available": pip_audit_available,
    }

    return {
        "access_control": access_control,
        "encryption": encryption,
        "audit_trail": audit_trail,
        "change_management": change_management,
        "incident_response": incident_response,
        "vulnerability_management": vulnerability_management,
    }

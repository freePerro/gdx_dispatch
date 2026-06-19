from __future__ import annotations

import base64
import dataclasses
import hashlib
import logging
import os
from typing import Any, Iterable

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

_MASTER_KEY = os.getenv("MASTER_ENCRYPTION_KEY")
_TENANT_ID = os.getenv("TENANT_ID", "")
_SEARCH_HASH_SALT = os.getenv("SEARCH_HASH_SALT", "")
if _MASTER_KEY:
    _hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_TENANT_ID.encode("utf-8"),
        info=b"gdx-pii-v1",
    )
    _FERNET = Fernet(base64.urlsafe_b64encode(_hkdf.derive(_MASTER_KEY.encode("utf-8"))))
else:
    _FERNET = None
    # Fail closed in production: without the key, EncryptedString columns and
    # QuickBooks OAuth tokens would silently persist as plaintext. Mirror the
    # JWT_SECRET boot gate. Dev/test (GDX_ENV unset or "development") may run
    # keyless — encryption is then a no-op, as documented below.
    if os.getenv("GDX_ENV", "").strip().lower() in ("production", "prod", "staging"):
        raise RuntimeError(
            "MASTER_ENCRYPTION_KEY not configured. The app refuses to start in a "
            "production-like environment with at-rest encryption disabled "
            "(secrets such as QuickBooks OAuth tokens would be stored in plaintext). "
            "Generate one: python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )


class EncryptedString(TypeDecorator[str]):
    """Fernet-encrypted TEXT column, opt-in per model.

    Live model consumers: NONE as of 2026-05-12 / S122-1c.

    The three previous consumers (``Customer.{name,email,phone,address}``,
    ``WebhookEndpoint.secret``, ``IntegrationConfig.secret``) were all
    dropped to plain ``Text`` after S122-1b proved the encryption contract
    had never been kept on prod: ``_FERNET`` stayed None for months, so
    every ``process_bind_param`` call short-circuited to plaintext. The
    typing was theater. S122-1b activation broke 269 Customer pages
    because ~20 raw-SQL routers bypassed ``process_result_value`` on
    reads. The symmetric writer-side bypass exists at
    ``gdx_dispatch/api/public_router.py:493`` and ``gdx_dispatch/core/public_api.py:395``
    (raw INSERT into ``webhook_endpoints.secret``). Both bypass classes
    are inert today because none of the affected columns are typed
    ``EncryptedString`` anymore.

    NOT a consumer (despite the name pattern): ``QBTokenStore.*_enc``
    columns are plain ``Text`` in the ORM and use the manual
    ``_encrypt`` / ``_decrypt`` helpers in
    ``gdx_dispatch/modules/quickbooks/oauth.py``, which talk to ``_FERNET``
    directly. Manual helpers are required there because the token-refresh
    flow needs to retry on ``InvalidToken`` and a TypeDecorator can't
    distinguish "first read, plaintext-legacy" from "second read,
    decrypt-failed-real-corruption".

    The class is preserved (not deleted) for the day a future sprint
    decides to bring encryption-at-rest back to one of those tables —
    see ``ai-queue/plans/sprint_encryption_rollout_proper.md`` Option C
    for the activation playbook.

    Before adding ``EncryptedString`` to any new model, check the
    invariant pinned in ``gdx_dispatch/tests/test_pii_typedecorator_raw_sql.py``:
    every read AND every write must go through the ORM. A single raw
    ``text("INSERT INTO …")`` or ``text("SELECT … FROM …")`` against
    the column breaks the contract symmetrically.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect):
        if value is None or _FERNET is None:
            return value
        return _FERNET.encrypt(value.encode("utf-8")).decode("utf-8")

    def process_result_value(self, value: str | None, dialect):
        # Plaintext-passthrough on InvalidToken — mirrors the precedent
        # at ``gdx_dispatch.core.database._decrypt_db_url:102``. During the
        # plaintext→ciphertext re-activation transition (S122-9 follow-on),
        # rows are mixed-state: most plaintext, newly-written ones
        # ciphertext. The passthrough lets reads work for both states.
        # Once the re-encrypt tool reports 0 plaintext rows for 24h, the
        # strict TypeDecorator can ship (passthrough removed) so future
        # plaintext writes fail loudly. Pattern lifted from Rails
        # ``support_unencrypted_data`` and django-cryptography.
        if value is None or _FERNET is None:
            return value
        from cryptography.fernet import InvalidToken  # noqa: PLC0415
        try:
            return _FERNET.decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            _emit_passthrough_warning(
                "EncryptedString.process_result_value",
                value,
            )
            return value


class HashColumn:
    @staticmethod
    def hash_for_search(value: str) -> str:
        normalized = value.lower().strip()
        payload = f"{_SEARCH_HASH_SALT}{normalized}".encode()
        return hashlib.sha256(payload).hexdigest()


# Per-call-site dedupe for passthrough WARN logs — auditor round-2 finding
# on S122-9 slice 1. Without dedupe, a failed activation against an ORM
# column the app reads on every request would spam 10k+ identical lines
# per hour. We log once per ``(call_site, 6-char prefix)`` pair per
# process. Loud enough to notice on first deploy; quiet enough not to
# drown the rest of the log stream.
_PASSTHROUGH_SEEN: set[tuple[str, str]] = set()


def _emit_passthrough_warning(call_site: str, value: str) -> None:
    """WARN-log an InvalidToken → plaintext passthrough at most once per
    ``(call_site, 6-char prefix)`` per process. Token bytes are never
    transcribed; only length + 6-char prefix appear in the log.
    """
    prefix = value[:6] if value else ""
    key = (call_site, prefix)
    if key in _PASSTHROUGH_SEEN:
        return
    _PASSTHROUGH_SEEN.add(key)
    logging.getLogger("gdx_dispatch.core.pii.passthrough").warning(
        "%s: InvalidToken — passthrough returning raw value "
        "(len=%d, prefix=%r). Identical-prefix events from this call "
        "site will be suppressed for the lifetime of this process.",
        call_site, len(value), prefix,
    )


def _reset_passthrough_dedupe() -> None:
    """Test hook — drop the dedupe set so tests can assert WARNs fire."""
    _PASSTHROUGH_SEEN.clear()


# Historical: Customer used to declare EncryptedString columns. The
# S122-1b/1c arc proved that pattern is unsafe in this codebase as long
# as routers reach customers via raw SQL. Don't reintroduce on Customer
# without first executing Option C in
# ai-queue/plans/sprint_encryption_rollout_proper.md.


# ---------------------------------------------------------------------------
# Encryption-at-rest attestation — single source of truth
# ---------------------------------------------------------------------------
#
# Three independent surfaces used to answer "is PII encrypted at rest?":
#   * gdx_dispatch/app.py:_check_encryption_at_rest  (boot gate)
#   * gdx_dispatch/core/soc2_evidence.py             (audit packet)
#   * gdx_dispatch/tools/tenant_schema_drift_check.py (schema-comparison hint)
#
# Pre-S122-1c they drifted: the boot gate checked _FERNET, SOC2 returned
# True iff EncryptedString could be imported, the drift check hard-coded
# the column type. Round-3 audit flagged all three diverged. This helper
# is the single source of truth. Pattern lifted from the GitHub
# "compliance-as-code" + Hoop.dev field-level-encryption-for-SOC-2
# write-ups: per-column structured report + boolean roll-up.


@dataclasses.dataclass(frozen=True)
class EncryptedColumn:
    """One column that declares the EncryptedString TypeDecorator.

    ``module`` is the full Python module path of the model class — a
    reliable plane label even when bases are renamed or merged. ``plane``
    is a coarse string derived from the base's identity at scan time;
    treat it as a hint, not a security boundary.
    """

    plane: str        # "tenant" | "control" | "other"
    module: str       # full python module path of the mapped class
    table: str        # __tablename__
    column: str       # column name on that table
    type_name: str    # registered type class name (always "EncryptedString" today)


@dataclasses.dataclass(frozen=True)
class EncryptionStatus:
    """Frozen attestation snapshot, JSON-serializable via dataclasses.asdict.

      * ``key_loaded``: ``pii._FERNET is not None``. Mirrors the original
        S122-1 boot-gate signal — false means every EncryptedString
        round-trips plaintext today. Stable semantics across S122-1c.
      * ``columns``: every mapped column observed using the
        ``EncryptedString`` TypeDecorator at scan time. ``()`` today.
      * ``columns_actually_encrypted``: ``key_loaded AND len(columns) > 0
        AND scan_error is None``. The richer "data is encrypted at rest"
        attestation — distinct from ``key_loaded`` because a loaded key
        with zero declared columns means there's nothing to encrypt.
        Use this for new SOC2/audit fields; the legacy ``key_loaded``
        bool stays for backward-compat with dashboards graphing it
        over time.
      * ``scan_error``: any scan-time failure surfaces here so callers
        can fail loudly. ``None`` on a successful scan.
    """

    key_loaded: bool
    columns: tuple[EncryptedColumn, ...]
    columns_actually_encrypted: bool
    scan_error: str | None = None


def _default_bases() -> tuple[list[Any], str | None]:
    """Discover the declarative bases that hold every registered model.

    Returns ``(bases, error)``. ``error`` is non-None when any base
    failed to import — caller should treat that as a fatal attestation
    error rather than silently scan a partial inventory.

    Imported lazily because each base lives in a module that depends on
    bits of ``core``.

    **The list of bases is explicit, not reflective.** SQLAlchemy lets
    any module declare its own ``DeclarativeBase`` subclass or call
    ``declarative_base()``, and there's no global registry of bases.
    A new base that isn't added here is invisible to the scan. The
    known set today is enumerated below; a new base must be added here
    AND the audit packet reviewed.
    """
    bases: list[Any] = []
    errors: list[str] = []
    candidates = [
        ("TenantBase", "gdx_dispatch.core.audit", "TenantBase"),
        ("ControlBase", "gdx_dispatch.control.models", "Base"),
        ("TaskMonitorBase", "gdx_dispatch.core.task_monitor", "ControlBase"),
    ]
    for label, module, attr in candidates:
        try:
            mod = __import__(module, fromlist=[attr])
            bases.append(getattr(mod, attr))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            logging.getLogger(__name__).exception(
                "_default_bases: %s import failed", label
            )
    return bases, ("; ".join(errors) if errors else None)


def _plane_for(base: Any) -> str:
    """Plane label heuristic. Truth is the module path in
    ``EncryptedColumn.module``; this is a coarse summary for dashboards.
    """
    if base.__name__ == "TenantBase":
        return "tenant"
    if base.__name__ in ("Base", "ControlBase"):
        return "control"
    return "other"


def _scan_bases(bases: Iterable[Any]) -> list[EncryptedColumn]:
    found: list[EncryptedColumn] = []
    for base in bases:
        plane = _plane_for(base)
        for mapper in base.registry.mappers:
            table_name = getattr(mapper.persist_selectable, "name", "?")
            module = getattr(mapper.class_, "__module__", "?")
            for col in mapper.columns:
                if isinstance(col.type, EncryptedString):
                    found.append(
                        EncryptedColumn(
                            plane=plane,
                            module=module,
                            table=str(table_name),
                            column=col.name,
                            type_name=type(col.type).__name__,
                        )
                    )
    return found


def encryption_status(bases: Iterable[Any] | None = None) -> EncryptionStatus:
    """Single source of truth for encryption-at-rest attestation.

    Re-scans on every call — mapper inventory grows over time as router
    modules import lazy-loaded models, so caching at boot would freeze
    the answer to a partial truth (auditor round-3 finding on the
    earlier cached implementation). Scan is O(columns) with no I/O.

    Scan failures surface via ``scan_error``; the helper never raises.
    Boot gate / SOC2 evidence are expected to check ``scan_error`` and
    treat a non-None value as a fatal attestation failure.
    """
    key_loaded = _FERNET is not None
    columns: tuple[EncryptedColumn, ...] = ()
    if bases is not None:
        scan_error: str | None = None
        scanned = list(bases)
    else:
        scanned, scan_error = _default_bases()

    if scan_error is None:
        try:
            columns = tuple(_scan_bases(scanned))
        except Exception as exc:  # noqa: BLE001
            scan_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).exception("encryption_status: scan failed")

    columns_actually_encrypted = (
        key_loaded and len(columns) > 0 and scan_error is None
    )

    return EncryptionStatus(
        key_loaded=key_loaded,
        columns=columns,
        columns_actually_encrypted=columns_actually_encrypted,
        scan_error=scan_error,
    )

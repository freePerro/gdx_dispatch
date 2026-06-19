"""Sprint 1.0 Phase E5 — nightly tenant-plane schema drift detector.

Defends the an earlier session invariant: every tenant DB must equal what
`TenantBase.metadata.create_all()` would produce. Without this, the pave
is a one-time miracle, not an enforced state.

For each active tenant in `tenants` (control DB), decrypts `db_url_enc`
via `gdx_dispatch.core.database._decrypt_db_url` (handles the two-postgres-host
trap from an earlier session — never hardcodes a host), pulls column types from
information_schema.columns, compares to the ORM via the same import
(`import gdx_dispatch.models`) that `pave_tenant_db.py` uses.

Usage (in the app container, where CONTROL_DATABASE_URL + GDX_FERNET_KEY
+ JWT_SECRET are already set):

    docker exec docker-app-1 python -m gdx_dispatch.tools.tenant_schema_drift_check

Exit codes:
    0 → every tenant DB matches ORM (no drift)
    1 → drift detected; per-tenant findings printed to stdout
    2 → infra error (control DB unreachable, missing env, model imports failed)

Optional Uptime Kuma push:
    Set SCHEMA_DRIFT_KUMA_URL=https://kuma.domain/api/push/TOKEN

Optional skip-list (for known-deprecated tenants pending D95 pave-or-drop):
    Set SCHEMA_DRIFT_SKIP_TENANTS=slug1,slug2,slug3 (comma-separated slugs)
    Skipped tenants print one line each and do NOT contribute to the
    nonzero exit code. Use sparingly — every skip is a deferred decision.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request

from sqlalchemy import create_engine, text


# Type-class → canonical PG token. Mirrors the an earlier session scanner;
# extended with timestamp tz handling because an earlier session reported 378 tz
# mismatches and we want this gate to surface that class.
_ORM_TO_PG = {
    "String": "varchar",
    "Text": "text",
    "Integer": "integer",
    "BigInteger": "bigint",
    "SmallInteger": "smallint",
    "Boolean": "boolean",
    "DateTime": "timestamp without time zone",  # default (no tz=True)
    "Date": "date",
    "Time": "time without time zone",
    "Float": "double precision",
    "Numeric": "numeric",
    "LargeBinary": "bytea",
    "JSON": "json",
    "JSONB": "jsonb",
    "UUID": "uuid",
    "Uuid": "uuid",
    # Project-specific subclasses. `EncryptedString` is a TypeDecorator
    # whose `impl = Text`, so the PG column is `text` regardless of
    # whether the key is loaded. Pinned by
    # `gdx_dispatch/tests/test_pii_encryption_status.py::test_drift_map_matches_impl`.
    # The runtime attestation of "is this column actually encrypted at rest"
    # lives in `gdx_dispatch.core.pii.encryption_status()` — don't reproduce that
    # logic here; the drift check only cares about the SQL column type.
    "EncryptedString": "text",
    "HashColumn": "text",
}


def _canon_orm(type_repr: str, has_tz: bool = False) -> str:
    """Render an ORM column type as a canonical PG token for comparison.

    `__enum__` is a sentinel — SQLAlchemy auto-generates PG enum type names
    (e.g. `job_lifecycle_stage`) so we can't compare by exact name. The diff
    treats `__enum__` as matching any prod token that is NOT in the known
    generic-type set (varchar/text/integer/etc)."""
    if type_repr.startswith("String(") and type_repr.endswith(")"):
        return f"varchar({type_repr[len('String('):-1]})"
    base = type_repr.split("(")[0]
    if base == "Enum":
        return "__enum__"
    canon = _ORM_TO_PG.get(base, type_repr.lower())
    if base == "DateTime" and has_tz:
        return "timestamp with time zone"
    if base == "Time" and has_tz:
        return "time with time zone"
    return canon


# Generic types the diff knows how to compare directly. If the ORM says
# `__enum__` and prod's token is not in this set (and not a varchar/char
# with an explicit length), prod is a custom PG type — treat as matching.
_GENERIC_PG_TOKENS = frozenset(_ORM_TO_PG.values())


def _orm_metadata():
    """Load ORM metadata using the same import path as pave_tenant_db.py."""
    # This single import registers every tenant-plane model.
    import gdx_dispatch.models  # noqa: F401
    from gdx_dispatch.core.audit import TenantBase
    return TenantBase.metadata


def _orm_schema() -> dict[str, dict[str, str]]:
    """{table: {col: canonical_pg_token}} from ORM."""
    md = _orm_metadata()
    out: dict[str, dict[str, str]] = {}
    for table_name, table in md.tables.items():
        cols: dict[str, str] = {}
        for col in table.columns:
            t = col.type
            type_name = type(t).__name__
            length = getattr(t, "length", None)
            type_repr = f"{type_name}({length})" if length else type_name
            has_tz = bool(getattr(t, "timezone", False))
            cols[col.name] = _canon_orm(type_repr, has_tz=has_tz)
        out[table_name] = cols
    return out


_PROD_SCHEMA_SQL = text("""
    SELECT table_name, column_name, data_type,
           COALESCE(udt_name,'') AS udt_name,
           COALESCE(character_maximum_length::text,'') AS char_len
    FROM information_schema.columns
    WHERE table_schema='public'
    ORDER BY table_name, ordinal_position
""")


def _prod_schema(engine) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    with engine.connect() as c:
        for tbl, col, data_type, udt, char_len in c.execute(_PROD_SCHEMA_SQL):
            if data_type == "USER-DEFINED":
                token = udt
            elif data_type == "character varying":
                token = f"varchar({char_len})" if char_len else "varchar"
            elif data_type == "character":
                token = f"char({char_len})" if char_len else "char"
            else:
                token = data_type
            out.setdefault(tbl, {})[col] = token
    return out


def _diff(orm: dict[str, dict[str, str]],
          prod: dict[str, dict[str, str]]) -> list[dict]:
    """Return a list of drift findings. Skips prod-only tables (legacy
    residue acknowledged an earlier session — door_pricing_rules etc.)."""
    findings: list[dict] = []
    for tbl, cols in orm.items():
        prod_cols = prod.get(tbl)
        if prod_cols is None:
            findings.append({"kind": "missing_table", "table": tbl, "col": None,
                             "orm": None, "prod": None})
            continue
        for col, orm_token in cols.items():
            prod_token = prod_cols.get(col)
            if prod_token is None:
                findings.append({"kind": "missing_col", "table": tbl, "col": col,
                                 "orm": orm_token, "prod": None})
                continue
            if orm_token == prod_token:
                continue
            if orm_token == "__enum__":
                # Prod must be a custom type (not generic); auto-generated
                # PG enum names can't be compared exactly.
                is_generic = (prod_token in _GENERIC_PG_TOKENS
                              or prod_token.startswith("varchar(")
                              or prod_token.startswith("char("))
                if is_generic:
                    findings.append({"kind": "type_mismatch", "table": tbl, "col": col,
                                     "orm": "enum", "prod": prod_token})
                continue
            if orm_token.startswith("varchar") and prod_token.startswith("varchar"):
                # Length-only mismatch — softer class, still drift.
                findings.append({"kind": "varchar_len", "table": tbl, "col": col,
                                 "orm": orm_token, "prod": prod_token})
                continue
            findings.append({"kind": "type_mismatch", "table": tbl, "col": col,
                             "orm": orm_token, "prod": prod_token})
    return findings


def _iter_tenants(control_url: str):
    """Yield (slug, decrypted_url) for every active tenant. Decrypts via
    `_decrypt_db_url` so we never assume host from the column name."""
    from gdx_dispatch.core.database import _decrypt_db_url
    eng = create_engine(control_url)
    try:
        with eng.connect() as c:
            rows = c.execute(text(
                "SELECT COALESCE(slug, id::text) AS slug, db_url_enc "
                "FROM tenants "
                "WHERE deleted_at IS NULL "
                "  AND db_url_enc IS NOT NULL AND db_url_enc <> ''"
            )).fetchall()
    finally:
        eng.dispose()
    for slug, enc in rows:
        try:
            url = _decrypt_db_url(str(enc))
        except Exception as e:  # noqa: BLE001
            print(f"[decrypt-failed] {slug}: {e}", file=sys.stderr)
            continue
        yield str(slug), url


def _push_kuma(status: str, msg: str) -> None:
    url = os.environ.get("SCHEMA_DRIFT_KUMA_URL", "")
    if not url:
        return
    try:
        sep = "&" if "?" in url else "?"
        full = f"{url}{sep}status={urllib.parse.quote(status)}&msg={urllib.parse.quote(msg)}&ping="
        urllib.request.urlopen(full, timeout=5).close()
    except Exception as e:  # noqa: BLE001 — best-effort
        print(f"[kuma-push-failed] {e}", file=sys.stderr)


def main() -> int:
    control_url = os.environ.get("CONTROL_DATABASE_URL", "")
    if not control_url:
        print("CONTROL_DATABASE_URL not set", file=sys.stderr)
        _push_kuma("down", "CONTROL_DATABASE_URL missing")
        return 2

    try:
        orm = _orm_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[orm-import-failed] {e}", file=sys.stderr)
        _push_kuma("down", f"orm import failed: {e}")
        return 2

    print(f"schema_drift_check: ORM declares {len(orm)} tables", file=sys.stderr)

    tenant_results: list[tuple[str, list[dict], str | None]] = []
    try:
        tenants = list(_iter_tenants(control_url))
    except Exception as e:  # noqa: BLE001
        print(f"[control-db-error] {e}", file=sys.stderr)
        _push_kuma("down", f"control db error: {e}")
        return 2

    skip = {s.strip() for s in os.environ.get("SCHEMA_DRIFT_SKIP_TENANTS", "").split(",") if s.strip()}
    skipped: list[str] = []
    for slug, url in tenants:
        if slug in skip:
            skipped.append(slug)
            continue
        try:
            eng = create_engine(url)
            try:
                prod = _prod_schema(eng)
            finally:
                eng.dispose()
            findings = _diff(orm, prod)
            tenant_results.append((slug, findings, None))
        except Exception as e:  # noqa: BLE001
            tenant_results.append((slug, [], str(e)))

    total_drift = sum(len(f) for _, f, _ in tenant_results)
    error_count = sum(1 for _, _, e in tenant_results if e)

    # Per-tenant report
    for slug in skipped:
        print(f"  {slug}: skipped (SCHEMA_DRIFT_SKIP_TENANTS)")
    for slug, findings, err in tenant_results:
        if err:
            print(f"  {slug}: ERROR — {err}")
            continue
        if not findings:
            print(f"  {slug}: clean")
            continue
        by_kind: dict[str, int] = {}
        for f in findings:
            by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        kind_str = " ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
        print(f"  {slug}: DRIFT ({len(findings)}) — {kind_str}")
        # Show up to 5 type_mismatch lines (the C4 class — most actionable)
        type_mm = [f for f in findings if f["kind"] == "type_mismatch"][:5]
        for f in type_mm:
            print(f"    type_mismatch {f['table']}.{f['col']}: ORM={f['orm']} PROD={f['prod']}")

    if total_drift == 0 and error_count == 0:
        print("schema_drift_check: CLEAN — every tenant DB matches ORM")
        _push_kuma("up", f"clean tenants={len(tenant_results)}")
        return 0

    summary = (f"drift={total_drift} tenants={len(tenant_results)} "
               f"errors={error_count}")
    print(f"schema_drift_check: DRIFT — {summary}")
    _push_kuma("down" if total_drift > 0 else "up", summary)
    return 1 if total_drift > 0 else 2


if __name__ == "__main__":
    sys.exit(main())

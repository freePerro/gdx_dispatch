"""Migration 029 actually runs, and grants what BUILTIN_ROLES says — no more.

Two gaps this closes, both of which bit 029 before review:

1. **Nothing in the suite ran alembic.** test_migration_revision_ids regex-matches
   the source; it never executes a migration. So a `LIKE` guard that didn't
   guard, a `||` that concatenated a string onto text, and a missing is_system
   gate all passed CI and only surfaced when a human ran the SQL by hand. The
   Postgres tests below run the migration's real grant against a real Postgres.

2. **No lockstep with BUILTIN_ROLES.** If permissions.py changes which roles get
   customers.contact_write and the migration's _ROLES isn't updated (or vice
   versa), a role is silently over- or under-granted. The drift test catches it.

The Postgres tests skip on SQLite / when no Postgres URL is set, so a local
sqlite run stays green; CI sets DATABASE_URL to its postgres:16 service and runs
them for real. This is the leads.* sweep's discipline (test the grant against a
DB), generalized into grant_helpers and pinned here.
"""
from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from gdx_dispatch.core.permissions import BUILTIN_ROLES
from gdx_dispatch.migrations.grant_helpers import grant_permission_to_seeded_roles


# The migration module can't be imported by dotted name (leading digit), so read
# its two constants the way alembic does — by loading the file. Keeping the test
# bound to the real module means a rename of either constant fails here.
def _load_migration():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "029_customer_contact_write.py"
    )
    spec = importlib.util.spec_from_file_location("_mig_029", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MIG = _load_migration()
_KEY = _MIG._KEY
_ROLES = _MIG._ROLES


# ── Lockstep: the migration and BUILTIN_ROLES cannot drift ──────────────────


def test_migration_roles_match_builtin_roles_exactly() -> None:
    """_ROLES must equal the non-admin/owner builtin roles that hold the key.

    admin/owner resolve via the live BUILTIN union (resolver step 3), so they get
    the key on deploy without a snapshot edit and must NOT be in _ROLES. Every
    OTHER builtin role that has the key in permissions.py must be in _ROLES, or
    its snapshot never receives it and the feature 403s for that role.
    """
    expected = {
        role
        for role, perms in BUILTIN_ROLES.items()
        if _KEY in perms and role not in ("admin", "owner")
    }
    assert set(_ROLES) == expected, (
        f"migration grants {sorted(_ROLES)} but BUILTIN_ROLES (minus admin/owner) "
        f"has {sorted(expected)} — update _ROLES or permissions.py so they agree"
    )


def test_admin_and_owner_are_not_in_the_migration() -> None:
    """They resolve via live BUILTIN; a snapshot edit for them is dead code."""
    assert "admin" not in _ROLES
    assert "owner" not in _ROLES


# ── Behavior against a real Postgres ────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="grant helper is Postgres-only (jsonb / pg_input_is_valid); set "
    "DATABASE_URL or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    """A skipif that fails open is how "migrations are tested in CI" quietly
    becomes false. GitHub Actions sets CI=true and ci.yml wires a postgres:16
    service into DATABASE_URL — if a future edit drops either, the Postgres
    tests below would silently SKIP and CI would stay green while testing
    nothing. This turns that into a red build instead.
    """
    if os.environ.get("CI"):
        assert "postgresql" in _URL, (
            "CI is set but no postgres URL — the migration tests would skip. "
            "ci.yml must keep DATABASE_URL pointed at the postgres service."
        )


@pytest.fixture()
def pg() -> Generator[Engine, None, None]:
    try:
        eng = create_engine(_URL)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # unreachable / wrong creds → skip, don't fail
        pytest.skip(f"postgres not reachable: {exc}")
    yield eng
    eng.dispose()


def _seed(conn) -> None:
    # A TEMP table shadows any real tenant_roles and drops at commit, so the test
    # is fully isolated and can never touch live rows.
    conn.execute(
        text(
            """
            CREATE TEMP TABLE tenant_roles (
                id uuid PRIMARY KEY,
                company_id varchar,
                name varchar,
                permissions text,
                is_system boolean,
                created_at timestamptz,
                updated_at timestamptz
            ) ON COMMIT DROP
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO tenant_roles (id, company_id, name, permissions, is_system, created_at, updated_at)
            VALUES
              (gen_random_uuid(), 'co', 'technician', '["jobs.read_own"]', true,  now(), now()),
              (gen_random_uuid(), 'co', 'technician', '["jobs.read_own"]', false, now(), now()),
              (gen_random_uuid(), 'co', 'dispatcher', '["jobs.read_all"]', true,  now(), now()),
              (gen_random_uuid(), 'co', 'sales',      '["x"]',             true,  now(), now()),
              (gen_random_uuid(), 'co', 'viewer',     '["jobs.read_own"]', true,  now(), now()),
              (gen_random_uuid(), 'co', 'owner',      '["*"]',             true,  now(), now()),
              (gen_random_uuid(), 'co', 'broken',     '[oops]',            true,  now(), now())
            """
        )
    )


def _perms(conn, name: str, *, is_system: bool) -> str:
    return conn.execute(
        text(
            "SELECT permissions FROM tenant_roles WHERE name = :n AND is_system = :s"
        ),
        {"n": name, "s": is_system},
    ).scalar_one()


def _has(conn, name: str, *, is_system: bool) -> bool:
    # CAST(:k AS text), not :k::text: a bindparam immediately before a `::` cast
    # defeats SQLAlchemy text()'s colon parser (it requires the param not be
    # followed by ':'), so :k reaches Postgres literally. But the cast IS needed —
    # a bare :k arrives typed 'unknown' and to_jsonb is polymorphic, so it must be
    # told the type. CAST(... AS text) gives both.
    return conn.execute(
        text(
            "SELECT (permissions::jsonb @> to_jsonb(CAST(:k AS text))) "
            "FROM tenant_roles WHERE name = :n AND is_system = :s"
        ),
        {"k": _KEY, "n": name, "s": is_system},
    ).scalar_one()


@_requires_pg
def test_grants_only_the_seeded_builtin_rows(pg: Engine) -> None:
    with pg.begin() as conn:
        _seed(conn)
        changed = grant_permission_to_seeded_roles(conn, permission=_KEY, roles=_ROLES)

        # The three seeded builtins that needed it — exactly those, nothing else.
        assert changed == 3
        assert _has(conn, "technician", is_system=True)
        assert _has(conn, "dispatcher", is_system=True)
        assert _has(conn, "sales", is_system=True)


@_requires_pg
def test_a_custom_role_of_the_same_name_is_never_touched(pg: Engine) -> None:
    """The is_system gate. A tenant's custom 'technician' is theirs — a raw
    `WHERE name IN (...)` would have granted it a permission it never asked for."""
    with pg.begin() as conn:
        _seed(conn)
        grant_permission_to_seeded_roles(conn, permission=_KEY, roles=_ROLES)
        assert not _has(conn, "technician", is_system=False)


@_requires_pg
def test_a_malformed_row_does_not_abort_the_migration(pg: Engine) -> None:
    """'[oops]' passes a `LIKE '[%]'` guard and then raises on ::jsonb — which,
    in alembic's single transaction, kills the whole run. pg_input_is_valid skips
    it instead."""
    with pg.begin() as conn:
        _seed(conn)
        # The seeded builtins still get granted despite the junk row present.
        changed = grant_permission_to_seeded_roles(conn, permission=_KEY, roles=_ROLES)
        assert changed == 3
        assert _perms(conn, "broken", is_system=True) == "[oops]"  # untouched


@_requires_pg
def test_wildcard_owner_is_left_alone(pg: Engine) -> None:
    with pg.begin() as conn:
        _seed(conn)
        grant_permission_to_seeded_roles(conn, permission=_KEY, roles=_ROLES)
        assert _perms(conn, "owner", is_system=True) == '["*"]'


@_requires_pg
def test_a_role_not_in_the_grant_list_is_left_alone(pg: Engine) -> None:
    with pg.begin() as conn:
        _seed(conn)
        grant_permission_to_seeded_roles(conn, permission=_KEY, roles=_ROLES)
        assert not _has(conn, "viewer", is_system=True)


@_requires_pg
def test_appends_rather_than_replacing(pg: Engine) -> None:
    """sales started with ['x']; it must end with ['x', key], not just [key]."""
    with pg.begin() as conn:
        _seed(conn)
        grant_permission_to_seeded_roles(conn, permission=_KEY, roles=_ROLES)
        perms = _perms(conn, "sales", is_system=True)
        assert '"x"' in perms and _KEY in perms


@_requires_pg
def test_the_migrations_own_upgrade_grants_correctly(pg: Engine) -> None:
    """Run 029.upgrade() itself, not just the helper it calls.

    The other pg tests call grant_permission_to_seeded_roles directly, so they
    prove the SQL is correct but skip everything the migration body decides — the
    permission string, the role list, and that a bug like passing `roles=_KEY`
    (a str, which Sequence[str] accepts and would silently match nothing) is
    wrong. Bind alembic's op proxy to a seeded temp table and run the real
    entrypoint end to end.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with pg.begin() as conn:
        _seed(conn)
        ctx = MigrationContext.configure(conn)
        with Operations.context(Operations(ctx)):
            _MIG.upgrade()  # the actual migration, op.get_bind() -> this conn

        # The seeded builtins got it through the real code path...
        assert _has(conn, "technician", is_system=True)
        assert _has(conn, "dispatcher", is_system=True)
        assert _has(conn, "sales", is_system=True)
        # ...and the same guards still hold when driven by upgrade(), not the helper.
        assert not _has(conn, "technician", is_system=False)  # custom same-name
        assert _perms(conn, "broken", is_system=True) == "[oops]"  # malformed, survived
        assert _perms(conn, "owner", is_system=True) == '["*"]'  # wildcard untouched


@_requires_pg
def test_idempotent_no_duplicate_key_on_rerun(pg: Engine) -> None:
    with pg.begin() as conn:
        _seed(conn)
        grant_permission_to_seeded_roles(conn, permission=_KEY, roles=_ROLES)
        second = grant_permission_to_seeded_roles(conn, permission=_KEY, roles=_ROLES)
        assert second == 0  # nothing left to grant
        count = conn.execute(
            text(
                "SELECT count(*) FROM jsonb_array_elements_text("
                "  (SELECT permissions::jsonb FROM tenant_roles "
                "   WHERE name='technician' AND is_system=true)"
                ") e WHERE e = :k"
            ),
            {"k": _KEY},
        ).scalar_one()
        assert count == 1

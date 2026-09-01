"""DELETE /api/admin/plugins/{package} must not fake a success (issue #100).

The handler used to run an unconditional
`DELETE FROM plugin_registry WHERE package = :p`, then return
`{"status": "unregistered"}` and write a `plugin.unregistered` audit row —
whether or not any row matched.

Two defects in one, and the second is the serious one:

  1. A success response for work never done. The repo's rule: "an action that
     succeeds without a trace, or fakes a success response without doing the
     work, is a defect of the highest class."
  2. A **false audit row**. `audit_logs` is append-only, so an entry asserting
     an unregistration that never happened cannot be walked back. The question
     the audit trail exists to answer — "can we reconstruct who did what" —
     gets a confidently wrong answer.

These tests pin the fix for BOTH endpoints. `delete_artifact` carried the
identical shape 300 lines above `remove_plugin` and was missed on the first
pass — an adversarial audit caught it. The fix owns the class, not the instance.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.plugin_host.reconcile import ensure_registry_table


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": "tenant-test"}),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


def _audit_rows(db, package: str) -> int:
    return db.execute(
        text(
            "SELECT COUNT(*) FROM audit_logs "
            "WHERE action = 'plugin.unregistered' AND entity_id = :p"
        ),
        {"p": package},
    ).scalar() or 0


def test_removing_a_package_that_was_never_registered_404s(db_session):
    from gdx_dispatch.routers.admin_plugins import remove_plugin

    ensure_registry_table(db_session)
    ghost = f"never-registered-{uuid.uuid4().hex[:8]}"

    with pytest.raises(HTTPException) as exc:
        remove_plugin(
            package=ghost,
            request=_request(),
            user={"sub": "u1", "role": "owner"},
            db=db_session,
        )
    assert exc.value.status_code == 404


def test_a_failed_removal_writes_no_audit_row(db_session):
    """The append-only trail must not gain an entry for work that didn't happen."""
    from gdx_dispatch.routers.admin_plugins import remove_plugin

    ensure_registry_table(db_session)
    ghost = f"never-registered-{uuid.uuid4().hex[:8]}"

    with pytest.raises(HTTPException):
        remove_plugin(
            package=ghost,
            request=_request(),
            user={"sub": "u1", "role": "owner"},
            db=db_session,
        )
    assert _audit_rows(db_session, ghost) == 0, (
        "a 404'd removal still wrote a plugin.unregistered audit row"
    )


def test_removing_a_real_registry_row_still_works_and_still_audits(db_session):
    """Counterfactual half: the fix must not break the path that should succeed."""
    from gdx_dispatch.routers.admin_plugins import remove_plugin

    ensure_registry_table(db_session)
    pkg = f"real-pkg-{uuid.uuid4().hex[:8]}"
    db_session.execute(
        text("INSERT INTO plugin_registry (package, version) VALUES (:p, :v)"),
        {"p": pkg, "v": "1.0.0"},
    )
    db_session.commit()

    out = remove_plugin(
        package=pkg,
        request=_request(),
        user={"sub": "u1", "role": "owner"},
        db=db_session,
    )
    assert out["status"] == "unregistered"
    gone = db_session.execute(
        text("SELECT COUNT(*) FROM plugin_registry WHERE package = :p"), {"p": pkg}
    ).scalar()
    assert gone == 0
    assert _audit_rows(db_session, pkg) == 1


# --- the twin: DELETE /api/admin/plugins/artifacts/{filename}


def test_deleting_an_artifact_that_does_not_exist_404s(db_session):
    from gdx_dispatch.routers.admin_plugins import delete_artifact
    from gdx_dispatch.plugin_host.reconcile import ensure_artifact_table

    ensure_artifact_table(db_session)
    ghost = f"never-uploaded-{uuid.uuid4().hex[:8]}-0.1.0-py3-none-any.whl"

    with pytest.raises(HTTPException) as exc:
        delete_artifact(
            filename=ghost,
            request=_request(),
            user={"sub": "u1", "role": "owner"},
            db=db_session,
        )
    assert exc.value.status_code == 404
    rows = db_session.execute(
        text(
            "SELECT COUNT(*) FROM audit_logs "
            "WHERE action = 'plugin.artifact_deleted' AND entity_id = :f"
        ),
        {"f": ghost},
    ).scalar() or 0
    assert rows == 0, "a 404'd artifact delete still wrote an audit row"


def test_deleting_a_real_artifact_still_works_and_still_audits(db_session):
    from gdx_dispatch.routers.admin_plugins import delete_artifact
    from gdx_dispatch.plugin_host.reconcile import ensure_artifact_table

    ensure_artifact_table(db_session)
    name = f"real-{uuid.uuid4().hex[:8]}-0.1.0-py3-none-any.whl"
    db_session.execute(
        text(
            "INSERT INTO plugin_artifact (filename, sha256, content) "
            "VALUES (:f, :s, :c)"
        ),
        {"f": name, "s": "0" * 64, "c": b"wheel-bytes"},
    )
    db_session.commit()

    out = delete_artifact(
        filename=name,
        request=_request(),
        user={"sub": "u1", "role": "owner"},
        db=db_session,
    )
    assert out["status"] == "removed"
    left = db_session.execute(
        text("SELECT COUNT(*) FROM plugin_artifact WHERE filename = :f"), {"f": name}
    ).scalar()
    assert left == 0

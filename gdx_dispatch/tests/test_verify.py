"""Tests for verify_after_write — the structural shape-1 silent-failure guard."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.verify import VerifyError, verify_after_write, verify_all


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=eng)
    s = Session()
    s.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT, flag TEXT)"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def test_int_expected_passes_on_match(session):
    session.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'a'), (2, 'b')"))
    session.commit()
    result = verify_after_write(
        session, "SELECT COUNT(*) FROM widgets", expected=2,
        description="widgets has 2 rows",
    )
    assert result == 2


def test_int_expected_raises_on_mismatch(session):
    session.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'a')"))
    session.commit()
    with pytest.raises(VerifyError) as exc_info:
        verify_after_write(
            session, "SELECT COUNT(*) FROM widgets", expected=5,
            description="widgets has 5 rows",
        )
    assert exc_info.value.expected == 5
    assert exc_info.value.actual == 1
    assert "widgets has 5 rows" in str(exc_info.value)


def test_callable_expected_passes_on_truthy(session):
    session.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'a'), (2, 'b'), (3, 'c')"))
    session.commit()
    verify_after_write(
        session, "SELECT COUNT(*) FROM widgets",
        expected=lambda n: n > 0,
        description="widgets is non-empty",
    )


def test_callable_expected_raises_on_falsy(session):
    with pytest.raises(VerifyError, match="non-empty"):
        verify_after_write(
            session, "SELECT COUNT(*) FROM widgets",
            expected=lambda n: n > 0,
            description="widgets is non-empty",
        )


def test_session_13_phantom_backfill_repro(session):
    """THE motivating case: an earlier session claimed a backfill committed that didn't.
    With verify_after_write, the commit would have raised VerifyError before
    log-claiming success."""
    session.execute(text("INSERT INTO widgets (id, flag) VALUES (1, NULL), (2, NULL), (3, NULL)"))
    session.commit()

    def claimed_backfill(commit_it: bool):
        # Simulate a backfill that does NOT actually happen for some reason
        # (e.g. WHERE clause matches 0, connection retry swallow, etc)
        if commit_it:
            session.execute(text("UPDATE widgets SET flag = 'y' WHERE id = 1"))
            session.commit()
        # The "success" claim — without verify, this runs either way
        verify_after_write(
            session, "SELECT COUNT(flag) FROM widgets", expected=3,
            description="all 3 widgets have flag populated after backfill",
        )

    # If the backfill didn't happen, verify_after_write catches the phantom
    with pytest.raises(VerifyError):
        claimed_backfill(commit_it=False)
    # If it did happen but only for 1 row, still catches the shortfall
    with pytest.raises(VerifyError):
        claimed_backfill(commit_it=True)


def test_verify_all_raises_on_first_failure(session):
    session.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'a')"))
    session.commit()
    checks = [
        ("SELECT COUNT(*) FROM widgets", 1, "first check — should pass"),
        ("SELECT COUNT(*) FROM widgets", 999, "second check — should fail"),
        ("SELECT 1/0", 0, "third check — never runs"),  # would error if reached
    ]
    with pytest.raises(VerifyError, match="second check"):
        verify_all(session, checks)


def test_params_are_bound(session):
    session.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'alice'), (2, 'bob')"))
    session.commit()
    result = verify_after_write(
        session, "SELECT COUNT(*) FROM widgets WHERE name = :who",
        expected=1,
        description="only alice matches",
        params={"who": "alice"},
    )
    assert result == 1


def test_raises_inside_transaction_triggers_rollback(session):
    """verify_after_write is designed to be called inside db.begin() so
    a failure triggers rollback. Confirm the pattern works end-to-end."""
    # Seed a clean state
    session.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'pristine')"))
    session.commit()

    # Attempt an operation + verify + expect rollback on mismatch
    try:
        with session.begin():
            session.execute(text("UPDATE widgets SET name = 'dirty' WHERE id = 1"))
            # Verify fails — this should roll back the UPDATE
            verify_after_write(
                session, "SELECT COUNT(*) FROM widgets WHERE name = 'pristine'",
                expected=1,
                description="pristine row must survive",
            )
    except VerifyError:
        pass  # expected

    # Post-rollback: the pristine row is still pristine
    current_name = session.execute(
        text("SELECT name FROM widgets WHERE id = 1")
    ).scalar()
    assert current_name == "pristine", \
        f"expected rollback, but name is {current_name!r}"

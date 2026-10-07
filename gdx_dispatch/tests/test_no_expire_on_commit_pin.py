"""No test session factory pins `expire_on_commit` off (GDXA-368).

`core.database.SessionLocal` leaves `expire_on_commit` at its default True, so
a handler that reads an attribute off a just-committed instance issues a
SELECT — the shape behind the post-commit-read 500s. A test factory pinned to
`expire_on_commit` off answers that read from memory, so its "no 500" can never
fail. 81 such pins were found in 24 files; they now go through
`conftest.production_sessionmaker`, and these tests keep them from returning.

Scope is that one setting. Other test `sessionmaker(...)` calls still leave
autoflush on, which production does not; this file does not police that.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import Column, Integer, String, create_engine, inspect
from sqlalchemy.orm import declarative_base

from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.tests.conftest import production_sessionmaker

_REPO = Path(__file__).resolve().parents[2]
# Built from parts so this file does not match its own scan.
_PIN = re.compile(r"expire_on_commit" + r"\s*=\s*False")


def test_no_test_session_pins_expire_on_commit_off():
    roots = [_REPO / "gdx_dispatch" / "tests", _REPO / "conftest.py"]
    files = [p for r in roots if r.exists() for p in ([r] if r.is_file() else r.rglob("*.py"))]
    assert len(files) > 100, f"scan saw only {len(files)} files; the root is wrong"
    hits = [
        f"{p.relative_to(_REPO)}:{n}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _PIN.search(line)
    ]
    assert hits == [], (
        "a test session factory pins expire_on_commit off, unlike production's "
        "SessionLocal; use conftest.production_sessionmaker instead:\n" + "\n".join(hits)
    )


def test_production_sessionmaker_matches_sessionlocal():
    engine = create_engine("sqlite://")
    sm = production_sessionmaker(engine)
    prod = {k: v for k, v in SessionLocal.kw.items() if k != "bind"}
    assert {k: v for k, v in sm.kw.items() if k != "bind"} == prod
    # sessionmaker wraps class_ in a fresh subclass, so compare what it wraps.
    assert sm.class_.__mro__[1] is SessionLocal.class_.__mro__[1]
    assert sm.kw["bind"] is engine


def test_production_sessionmaker_expires_on_commit():
    """Behaviour, not configuration: a committed instance's columns are expired."""
    base = declarative_base()

    class Row(base):
        __tablename__ = "gdxa368_row"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    engine = create_engine("sqlite://")
    base.metadata.create_all(engine)
    db = production_sessionmaker(engine)()
    try:
        row = Row(name="x")
        db.add(row)
        db.commit()
        assert "name" in inspect(row).expired_attributes
    finally:
        db.close()
        engine.dispose()

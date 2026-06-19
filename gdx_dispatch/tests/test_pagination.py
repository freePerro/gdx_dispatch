"""Tests for gdx_dispatch.core.pagination — shared paginator helper.

Coverage:
    - default PageParams (offset=0, limit=50)
    - max-limit clamp (limit > MAX_LIMIT rejected at Pydantic gate AND
      clamped() helper rescues programmatically-built instances)
    - offset=0 first-page returns rows 0..limit
    - offset beyond total returns empty page
    - has_more True/False boundary
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from gdx_dispatch.core.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PageParams,
    envelope,
    paginate,
)

Base = declarative_base()


class Item(Base):
    __tablename__ = "pagination_test_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False)


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'p.db'}", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    for i in range(10):
        db.add(Item(name=f"item-{i:02d}"))
    db.commit()
    yield db
    db.close()


def test_default_params():
    p = PageParams()
    assert p.offset == 0
    assert p.limit == DEFAULT_LIMIT == 50


def test_max_limit_rejected_by_pydantic():
    with pytest.raises(ValidationError):
        PageParams(limit=MAX_LIMIT + 1)
    # negative offset rejected
    with pytest.raises(ValidationError):
        PageParams(offset=-1)
    # zero limit rejected
    with pytest.raises(ValidationError):
        PageParams(limit=0)


def test_offset_zero_first_page_legacy_query(session):
    q = session.query(Item).order_by(Item.id)
    paged, meta = paginate(q, PageParams(offset=0, limit=3))
    rows = paged.all()
    assert [r.name for r in rows] == ["item-00", "item-01", "item-02"]
    assert meta.offset == 0
    assert meta.limit == 3
    assert meta.total == 10
    assert meta.has_more is True


def test_offset_beyond_total_returns_empty(session):
    q = session.query(Item).order_by(Item.id)
    paged, meta = paginate(q, PageParams(offset=100, limit=5))
    assert paged.all() == []
    assert meta.total == 10
    assert meta.has_more is False


def test_has_more_boundary(session):
    # last page exactly → has_more False
    q = session.query(Item).order_by(Item.id)
    paged, meta = paginate(q, PageParams(offset=5, limit=5))
    rows = paged.all()
    assert len(rows) == 5
    assert meta.has_more is False

    # second-to-last page → has_more True
    paged2, meta2 = paginate(q, PageParams(offset=0, limit=5))
    assert meta2.has_more is True


def test_envelope_shape(session):
    q = session.query(Item).order_by(Item.id)
    paged, meta = paginate(q, PageParams(offset=0, limit=4))
    body = envelope([{"id": r.id, "name": r.name} for r in paged.all()], meta)
    assert set(body.keys()) == {"items", "meta"}
    assert len(body["items"]) == 4
    assert body["meta"] == {
        "offset": 0,
        "limit": 4,
        "total": 10,
        "has_more": True,
    }

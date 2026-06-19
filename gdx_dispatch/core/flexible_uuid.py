"""FlexibleUUID — SQLAlchemy TypeDecorator that handles both UUID objects and strings.

Solves the Flask→GDX migration problem:
- Flask stored UUIDs as VARCHAR(36) strings
- GDX ORM defined columns as Uuid(as_uuid=True)
- PostgreSQL can't compare VARCHAR = UUID

FlexibleUUID:
- Accepts both UUID objects and string inputs
- Stores as string (VARCHAR-compatible)
- Returns string from queries
- Works on both SQLite and PostgreSQL
- Compares correctly with both str and UUID values

Usage:
    from gdx_dispatch.core.flexible_uuid import FlexibleUUID

    class Job(Base):
        id: Mapped[str] = mapped_column(FlexibleUUID(), primary_key=True, default=lambda: str(uuid4()))
        customer_id: Mapped[str | None] = mapped_column(FlexibleUUID(), ForeignKey("customers.id"), nullable=True)
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator


class FlexibleUUID(TypeDecorator):
    """UUID type that works with both VARCHAR and UUID database columns.

    Stores as VARCHAR(36), accepts UUID objects or strings,
    always returns strings from queries.
    """

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Convert incoming value to string for storage."""
        if value is None:
            return None
        if isinstance(value, UUID):
            return str(value)
        return str(value)

    def process_result_value(self, value, dialect):
        """Return string from database."""
        if value is None:
            return None
        return str(value)

    def process_literal_param(self, value, dialect):
        """Handle literal parameters in SQL expressions."""
        if value is None:
            return None
        return str(value)

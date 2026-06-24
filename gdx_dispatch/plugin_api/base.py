"""PluginBase — the single declarative base plugin models inherit from.

The core codebase has multiple declarative bases and *no global registry*
(core/pii.py:231), so a plugin can't just "define models" and expect Alembic to
find them. Every plugin model inherits from this one base; the plugin-host's
migration phase targets `PluginBase.metadata`, so all plugin tables live on one
metadata the host can see.

Convention: name plugin tables `plug_<key>_*` so they never collide with core or
with each other. (Enforced by review, not code, for now.)

Importing this pulls in SQLAlchemy — that's why it's a submodule, not re-exported
from plugin_api/__init__ (which stays stdlib-only for host-side discovery tests).
"""
from __future__ import annotations

from sqlalchemy.orm import declarative_base

PluginBase = declarative_base()

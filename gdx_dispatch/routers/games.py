"""Game system router — catalog, state, and event endpoints.

This is the data-driven game system that powers Claude's co-op game today
and will power user games (Owner Garden, Tech Helper, etc.) tomorrow.

Three endpoints:
  - GET  /api/games/catalog       — list available game definitions
  - GET  /api/games/state         — read current state for one (actor, game)
  - POST /api/games/event         — append a score event, recompute state

The rule engine is intentionally tiny: it reads the game's rules_json and
applies one of a small set of operations (add, subtract, set, set_string)
to a state field. New games define new behavior by writing JSON, not Python.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import GameDefinition, GameEvent, GameState
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/games",
    tags=["games"],
    dependencies=[Depends(require_role("admin", "owner", "superadmin"))],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class GameDefinitionOut(BaseModel):
    slug: str
    name: str
    description: str | None
    icon: str | None
    actor_type: str
    publisher: str
    layout_json: dict[str, Any]
    rules_json: dict[str, Any]
    tenant_id: str | None
    is_published: bool


class GameStateOut(BaseModel):
    actor_id: str
    game_slug: str
    tenant_id: str | None
    lives: int
    max_lives: int
    hp: int
    max_hp: int
    xp: int
    current_phase: str | None
    state_json: dict[str, Any]
    updated_at: datetime
    recent_events: list[dict[str, Any]] = Field(default_factory=list)


class GameEventIn(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    game_slug: str = Field(min_length=1, max_length=100)
    event_type: str = Field(min_length=1, max_length=50)
    value: int | None = Field(default=None)
    value_string: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Rule engine — applies one event to a state row using the game's rules_json.
# ---------------------------------------------------------------------------


def _resolve_bound_value(state: GameState, key: str | int) -> int | None:
    """Resolve a rule value that might be a literal int or a state field name."""
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        return getattr(state, key, None)
    return None


def apply_event_to_state(state: GameState, rules: dict[str, Any], event: GameEventIn) -> None:
    """Mutate `state` in place by applying `event` according to `rules`.

    Rules format:
        {
          "events": {
            "<event_type>": {
              "field": "<state_field>",
              "operation": "add" | "subtract" | "set" | "set_string",
              "min": <int or field name>,           # optional clamp
              "max": <int or field name>,           # optional clamp
              "value": <int>                        # for "set" with fixed value
            },
            ...
          }
        }
    """
    rule = (rules.get("events") or {}).get(event.event_type)
    if not rule:
        raise HTTPException(
            status_code=400,
            detail=f"event_type '{event.event_type}' is not defined in this game's rules",
        )

    field = rule.get("field")
    operation = rule.get("operation")
    if not field or not operation:
        raise HTTPException(status_code=500, detail="game rule is missing field or operation")

    if operation == "set_string":
        if event.value_string is None:
            raise HTTPException(
                status_code=400,
                detail=f"event_type '{event.event_type}' requires value_string",
            )
        setattr(state, field, event.value_string)
        return

    # All other operations are integer-based
    current = getattr(state, field, 0) or 0

    if operation == "add":
        if event.value is None:
            raise HTTPException(status_code=400, detail=f"event_type '{event.event_type}' requires value")
        new_value = current + event.value
    elif operation == "subtract":
        if event.value is None:
            raise HTTPException(status_code=400, detail=f"event_type '{event.event_type}' requires value")
        new_value = current - event.value
    elif operation == "set":
        # Use rule's fixed value if present, otherwise the event's value
        fixed = rule.get("value")
        if fixed is not None:
            new_value = int(fixed)
        elif event.value is not None:
            new_value = event.value
        else:
            raise HTTPException(status_code=400, detail="set operation needs a value")
    else:
        raise HTTPException(status_code=500, detail=f"unknown operation: {operation}")

    # Clamp to min/max if specified
    rule_min = rule.get("min")
    rule_max = rule.get("max")
    if rule_min is not None:
        floor = _resolve_bound_value(state, rule_min)
        if floor is not None and new_value < floor:
            new_value = floor
    if rule_max is not None:
        ceiling = _resolve_bound_value(state, rule_max)
        if ceiling is not None and new_value > ceiling:
            new_value = ceiling

    setattr(state, field, new_value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_definition(d: GameDefinition) -> GameDefinitionOut:
    return GameDefinitionOut(
        slug=d.slug,
        name=d.name,
        description=d.description,
        icon=d.icon,
        actor_type=d.actor_type,
        publisher=d.publisher,
        layout_json=d.layout_json or {},
        rules_json=d.rules_json or {},
        tenant_id=d.tenant_id,
        is_published=d.is_published,
    )


def _serialize_event(e: GameEvent) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "actor_id": e.actor_id,
        "game_slug": e.game_slug,
        "event_type": e.event_type,
        "value": e.value,
        "value_string": e.value_string,
        "reason": e.reason,
        "created_by_user_id": e.created_by_user_id,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def _serialize_state(state: GameState, recent_events: list[GameEvent]) -> GameStateOut:
    return GameStateOut(
        actor_id=state.actor_id,
        game_slug=state.game_slug,
        tenant_id=state.tenant_id,
        lives=state.lives,
        max_lives=state.max_lives,
        hp=state.hp,
        max_hp=state.max_hp,
        xp=state.xp,
        current_phase=state.current_phase,
        state_json=state.state_json or {},
        updated_at=state.updated_at,
        recent_events=[_serialize_event(e) for e in recent_events],
    )


def _fetch_recent_events(db: Session, actor_id: str, game_slug: str, limit: int = 10) -> list[GameEvent]:
    stmt = (
        select(GameEvent)
        .where(GameEvent.actor_id == actor_id, GameEvent.game_slug == game_slug)
        .order_by(GameEvent.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/catalog", response_model=list[GameDefinitionOut])
def list_catalog(
    actor_type: str | None = Query(default=None, description="Filter by actor type (claude, owner, tech, etc.)"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return all published game definitions, optionally filtered by actor type."""
    stmt = select(GameDefinition).where(
        GameDefinition.is_published.is_(True),
        GameDefinition.deleted_at.is_(None),
    )
    if actor_type:
        stmt = stmt.where(GameDefinition.actor_type == actor_type)
    stmt = stmt.order_by(GameDefinition.slug)

    definitions = list(db.execute(stmt).scalars().all())
    return [_serialize_definition(d) for d in definitions]


@router.get("/state", response_model=GameStateOut)
def get_state(
    actor: str = Query(min_length=1, max_length=100),
    game: str = Query(min_length=1, max_length=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return the current state row for one (actor, game) pair, with recent events."""
    state = db.execute(
        select(GameState).where(
            GameState.actor_id == actor,
            GameState.game_slug == game,
        )
    ).scalar_one_or_none()

    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"no state row for actor='{actor}' game='{game}'",
        )

    recent = _fetch_recent_events(db, actor, game, limit=10)
    return _serialize_state(state, recent)


@router.post("/event", response_model=GameStateOut)
def post_event(
    event: GameEventIn,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Append a game event, apply its effect to the state row, return updated state."""
    # Find the game definition (for the rules)
    definition = db.execute(
        select(GameDefinition).where(GameDefinition.slug == event.game_slug)
    ).scalar_one_or_none()
    if not definition:
        raise HTTPException(status_code=404, detail=f"game '{event.game_slug}' not found")

    # Find the state row to mutate
    state = db.execute(
        select(GameState).where(
            GameState.actor_id == event.actor_id,
            GameState.game_slug == event.game_slug,
        )
    ).scalar_one_or_none()
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"no state row for actor='{event.actor_id}' game='{event.game_slug}'",
        )

    # Apply the event to the state via the rule engine
    apply_event_to_state(state, definition.rules_json or {}, event)
    state.updated_at = datetime.now(timezone.utc)

    # Append the event to the audit log
    user_id = (user.get("sub") or user.get("user_id") or "system") if isinstance(user, dict) else "system"
    db_event = GameEvent(
        id=uuid4(),
        actor_id=event.actor_id,
        game_slug=event.game_slug,
        event_type=event.event_type,
        value=event.value,
        value_string=event.value_string,
        reason=event.reason,
        created_by_user_id=str(user_id),
    )
    db.add(db_event)
    db.commit()
    db.refresh(state)

    log.info(
        "game_event actor=%s game=%s type=%s value=%s reason=%r",
        event.actor_id, event.game_slug, event.event_type, event.value, event.reason,
    )

    recent = _fetch_recent_events(db, event.actor_id, event.game_slug, limit=10)
    return _serialize_state(state, recent)

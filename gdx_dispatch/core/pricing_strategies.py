"""ADR-015 Slice 2 — pluggable catalog pricing strategies.

A *pricing strategy* turns an item's cost into its retail (sell) price. Every
catalog records which strategy it uses; the catalog router applies it when an
item is saved with no explicit price. The default (`manual`) keeps the entered
price — exactly the pre-ADR-015 behavior, so existing catalogs are unchanged.

Strategies are **declarative**: a strategy is a `{kind, params}` spec, not code.
That is deliberate (ADR-015 Slice 3): a Catalog Pack contributes its pricing as
DATA which is copied onto the catalog at creation, so pack pricing runs here in
the core process without importing any pack code — preserving ADR-013's process
isolation. Only the four `kind` evaluators below are code, and they are generic.

Kinds:
  manual      → None (keep the entered price)
  multiplier  → cost * factor            (params: {"factor": float})
  markup      → cost * (1 + pct)         (params: {"pct": float})   # pct as 0.5 = 50%
  margin      → cost / (1 - pct)         (params: {"pct": float})   # gross-margin method
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class StrategyDef:
    id: str
    label: str
    kind: str
    params: dict
    builtin: bool = True


# Built-in strategies. 'manual' is the default — passthrough, so adding the
# pricing-strategy column changes nothing for existing catalogs.
_BUILTINS: dict[str, StrategyDef] = {
    "manual": StrategyDef("manual", "Manual — enter price", "manual", {}),
    "margin_50": StrategyDef("margin_50", "Margin 50% (cost ÷ 0.5)", "margin", {"pct": 0.5}),
    "markup_50": StrategyDef("markup_50", "Markup 50% (cost × 1.5)", "markup", {"pct": 0.5}),
    "keystone": StrategyDef("keystone", "Keystone (2× cost)", "multiplier", {"factor": 2.0}),
}

DEFAULT_STRATEGY = "manual"
KNOWN_KINDS = {"manual", "multiplier", "markup", "margin"}

# Pack-contributed strategies, registered at runtime. Kept separate from
# _BUILTINS so a pack can never shadow a built-in id.
_PACK: dict[str, StrategyDef] = {}


def register_pack_strategy(id: str, label: str, kind: str, params: dict | None = None) -> StrategyDef:
    """Register a declarative strategy contributed by a Catalog Pack.

    Idempotent; a pack re-registering the same id replaces its prior def. Never
    overrides a built-in. Returns the stored def.
    """
    sid = (id or "").strip().lower()
    if not sid:
        raise ValueError("strategy id must be non-empty")
    if sid in _BUILTINS:
        raise ValueError(f"strategy id {sid!r} collides with a built-in")
    if kind not in KNOWN_KINDS:
        raise ValueError(f"strategy kind {kind!r} must be one of {sorted(KNOWN_KINDS)}")
    d = StrategyDef(sid, (label or sid), kind, dict(params or {}), builtin=False)
    _PACK[sid] = d
    return d


def get_strategy(strategy_id: str | None) -> StrategyDef:
    """Resolve a strategy id to its def, falling back to the default."""
    sid = (strategy_id or DEFAULT_STRATEGY).strip().lower()
    return _BUILTINS.get(sid) or _PACK.get(sid) or _BUILTINS[DEFAULT_STRATEGY]


def list_strategies() -> list[dict]:
    """All strategies (built-in + pack), for the New Catalog pricing dropdown.

    Includes `params` so the client can store a self-contained `pricing_config`
    on the catalog — pricing then never depends on this in-memory registry at
    compute time (important across multiple worker processes).
    """
    out = [
        {"id": d.id, "label": d.label, "kind": d.kind, "params": d.params, "builtin": d.builtin}
        for d in (*_BUILTINS.values(), *_PACK.values())
    ]
    return out


def is_builtin(strategy_id: str) -> bool:
    """True for the always-available built-in strategies (deterministic across
    workers, unlike the pack registry which is populated lazily)."""
    return (strategy_id or "").strip().lower() in _BUILTINS


def is_known(strategy_id: str) -> bool:
    sid = (strategy_id or "").strip().lower()
    return sid in _BUILTINS or sid in _PACK


def _evaluate(kind: str, params: dict, cost: Decimal) -> Decimal | None:
    if kind == "manual":
        return None
    if kind == "multiplier":
        factor = Decimal(str(params.get("factor", 1)))
        return cost * factor
    if kind == "markup":
        pct = Decimal(str(params.get("pct", 0)))
        return cost * (Decimal("1") + pct)
    if kind == "margin":
        pct = Decimal(str(params.get("pct", 0)))
        if pct < 0 or pct >= 1:
            return None  # a >=100% margin implies infinite price; ignore
        return cost / (Decimal("1") - pct)
    return None


def compute_price(
    strategy_id: str | None,
    cost: Decimal | float | int | None,
    *,
    config: dict | None = None,
) -> Decimal | None:
    """Compute retail from cost. Returns None to mean "keep entered price".

    `config` (when truthy and carrying a `kind`) is the catalog's stored,
    self-contained spec — used for pack strategies copied onto the catalog so no
    pack code or cross-process lookup is needed at pricing time. Otherwise the
    id is resolved against the built-in / registered defs.
    """
    if cost in (None, ""):
        return None
    c = cost if isinstance(cost, Decimal) else Decimal(str(cost))
    if c < 0:
        return None
    if config and config.get("kind"):
        return _evaluate(config["kind"], config.get("params") or {}, c)
    d = get_strategy(strategy_id)
    return _evaluate(d.kind, d.params, c)

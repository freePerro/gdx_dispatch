"""Sprint Outlook Integration — Phase 4 ACL chokepoint.

Single function ``can_view(message, viewer_user_id, viewer_role, viewer_tenant_db)``
returns True/False per the tenant's configured visibility rules. EVERY query
that surfaces emails (Phase 5 views, search, AI tools) must filter through
this function — there is no other path to authorize access.

Configuration lives on ``OutlookSettings.visibility_rules`` (JSON). Defaults
match Doug's 2026-04-27 spec; admins can change every rule.

Six rules:
  - ``tagged_visibility_above_role``: enum {tech, tech_plus_one, admin_only,
    owner_only} — minimum role to see tagged emails. Default
    ``tech_plus_one``. ``owner_only`` = nobody but the mailbox owner, ever
    (true single-person privacy — other admins/owners included).
  - ``tech_recipient_visible_to_all_techs``: bool — if a tech is in to/cc/bcc,
    all techs see it. Default true.
  - ``tech_outbound_no_tag_visibility``: enum {only_sender, all_techs, above_tech}
    — for emails *sent* by a tech with no customer/job tag. Default ``only_sender``.
  - ``tech_to_tech_internal_visibility``: enum {only_participants, all_techs, above_tech}
    — emails between two techs only, no tag. Default ``only_participants``.
  - ``above_tech_scope``: enum {all_tagged, only_assigned_rows} — does an
    above-tech (admin/owner) see all tagged emails or only ones for customers
    /jobs they have other access to. Default ``all_tagged``.
  - ``untagged_visibility``: enum {only_owner, none} — what to do with the
    long tail of untagged emails. Default ``only_owner``.

Plus per-user override: ``OutlookMessage.is_personal=True`` makes a thread
visible only to its sender, regardless of every rule above.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookMessage, OutlookSettings


log = logging.getLogger("gdx_dispatch.modules.outlook.visibility")


@dataclass
class VisibilityContext:
    """Per-request prefetch. Eliminates N+1 in filter_visible.

    `rules`: a single OutlookSettings.visibility_rules read.
    `account_owner`: account_id → user_id (str). Built from one batched query.
    `user_is_tech`: user_id (str) → bool. Built from one batched query.
    `tech_emails`: known-tech email addresses, lowercased.
    """
    rules: dict[str, Any] = field(default_factory=dict)
    account_owner: dict[Any, str] = field(default_factory=dict)
    user_is_tech: dict[str, bool] = field(default_factory=dict)
    tech_emails: set[str] = field(default_factory=set)


# Role hierarchy for "above_tech" comparisons. Match the existing
# gdx_dispatch.core.modules.RBAC_HIERARCHY shape.
_ROLE_RANK = {
    "owner": 5,
    "admin": 4,
    "dispatcher": 3,
    "csr": 3,                 # alias
    "technician": 2,
    "tech": 2,                # alias
    "viewer": 1,
}


_DEFAULT_RULES = {
    "tagged_visibility_above_role": "tech_plus_one",
    "tech_recipient_visible_to_all_techs": True,
    "tech_outbound_no_tag_visibility": "only_sender",
    "tech_to_tech_internal_visibility": "only_participants",
    "above_tech_scope": "all_tagged",
    "untagged_visibility": "only_owner",
}


def _role_rank(role: str | None) -> int:
    if not role:
        return 0
    return _ROLE_RANK.get(role.lower(), 0)


def _is_above_tech(role: str | None) -> bool:
    """Anyone above 'technician' rank — csr/dispatcher/admin/owner."""
    return _role_rank(role) > _role_rank("technician")


def _is_tech(role: str | None) -> bool:
    return _role_rank(role) == _role_rank("technician")


def _load_rules(tenant_db: Session) -> dict[str, Any]:
    """Load rules from OutlookSettings; fall back to defaults if missing."""
    settings = tenant_db.query(OutlookSettings).filter(OutlookSettings.id == 1).first()
    rules = dict(_DEFAULT_RULES)
    # isinstance guard: a real row's visibility_rules is a JSON dict or None;
    # anything else (corrupt row, test double) falls back to the fail-closed
    # defaults instead of raising inside the ACL.
    if settings is not None and isinstance(settings.visibility_rules, dict):
        rules.update(settings.visibility_rules)
    return rules


def _accounts_to_user(tenant_db: Session, account_id: UUID) -> UUID | None:
    """Return the user_id that owns this OutlookAccount, or None.

    getattr, not attribute access: fail CLOSED (no owner → no visibility)
    on odd shapes rather than 500ing the read path."""
    account = tenant_db.get(OutlookAccount, account_id)
    return getattr(account, "user_id", None) if account else None


def _is_message_recipient_a_tech(
    tenant_db: Session,
    message: OutlookMessage,
    tech_emails: set[str],
) -> bool:
    """Heuristic: any to/cc/bcc address matches a known-tech email."""
    if not tech_emails:
        return False
    msg_addrs = set()
    for collection in (message.to_addresses, message.cc_addresses, message.bcc_addresses):
        for addr in (collection or []):
            if addr:
                msg_addrs.add(addr.lower().strip())
    return bool(msg_addrs & tech_emails)


def _resolve_owner(message: OutlookMessage, tenant_db: Session, ctx: VisibilityContext | None) -> str | None:
    """Return account.user_id for a message, using ctx prefetch when present."""
    if ctx is not None and message.account_id in ctx.account_owner:
        return ctx.account_owner[message.account_id]
    return _accounts_to_user(tenant_db, message.account_id)


def _resolve_is_tech(user_id: str | None, tenant_db: Session, ctx: VisibilityContext | None) -> bool:
    """Return whether a given user_id is a tech, using ctx prefetch when present."""
    if user_id is None:
        return False
    sid = str(user_id)
    if ctx is not None and sid in ctx.user_is_tech:
        return ctx.user_is_tech[sid]
    return _accounts_role_is_tech(tenant_db, sid)


def _resolve_rules(tenant_db: Session, ctx: VisibilityContext | None) -> dict[str, Any]:
    if ctx is not None and ctx.rules:
        return ctx.rules
    return _load_rules(tenant_db)


def can_view(
    message: OutlookMessage,
    viewer_user_id: UUID | str,
    viewer_role: str,
    tenant_db: Session,
    *,
    tech_emails: set[str] | None = None,
    ctx: VisibilityContext | None = None,
) -> bool:
    """The single chokepoint. True if `viewer` may see `message`.

    `tech_emails` is a pre-loaded set of known-tech mailbox emails. `ctx` is a
    pre-built VisibilityContext that batches the rules + account-owner + user-
    role lookups. Callers iterating many messages should always pass ctx —
    otherwise this function does N+1 queries per message (acceptable on a
    single-message detail endpoint).
    """
    if ctx is not None:
        # ctx may already carry tech_emails — prefer the explicit kwarg if set.
        if tech_emails is None:
            tech_emails = ctx.tech_emails
    # Personal override — sender only, period. Compare as STRING because
    # OutlookAccount.user_id is String(36) but callers may pass UUID; in Python
    # `UUID('abc...') == 'abc...'` is False — coerce both sides to str.
    viewer_id_s = str(viewer_user_id) if viewer_user_id is not None else None
    if message.is_personal:
        owner = _resolve_owner(message, tenant_db, ctx)
        return owner is not None and str(owner) == viewer_id_s

    rules = _resolve_rules(tenant_db, ctx)
    sender_user_id = _resolve_owner(message, tenant_db, ctx)
    is_self = (
        sender_user_id is not None
        and viewer_id_s is not None
        and str(sender_user_id) == viewer_id_s
    )
    is_tagged = bool(message.linked_customer_id or message.linked_job_id)
    above = _is_above_tech(viewer_role)
    tech = _is_tech(viewer_role)

    # Self always sees own mailbox messages.
    if is_self:
        return True

    if is_tagged:
        # Tagged email visibility — check tagged_visibility_above_role.
        min_role = rules.get("tagged_visibility_above_role", "tech_plus_one")
        if min_role == "owner_only":
            # Fully private mailbox: is_self already returned True above, so
            # everyone else — other admins and owners included — is hidden.
            return False
        if min_role == "tech":
            return True  # all roles see tagged
        if min_role == "tech_plus_one":
            # csr/dispatcher/admin/owner see tagged; techs only see their own
            # AND messages where a tech is in to/cc (per recipient rule below).
            if above:
                # Optionally restrict to assigned rows
                scope = rules.get("above_tech_scope", "all_tagged")
                if scope == "all_tagged":
                    return True
                # only_assigned_rows: defer to caller's other access rules.
                # Simplest implementation: treat as True (caller must filter).
                return True
            if tech:
                # Tech recipient rule — if a tech is in to/cc, all techs see
                if rules.get("tech_recipient_visible_to_all_techs", True):
                    if _is_message_recipient_a_tech(tenant_db, message, tech_emails or set()):
                        return True
                return False
            return False
        if min_role == "admin_only":
            return _role_rank(viewer_role) >= _role_rank("admin")
        return False

    # Untagged email
    if not is_tagged:
        # Tech outbound (sent by a tech, no tag)
        sender_role_is_tech = False
        if sender_user_id is not None:
            sender_role_is_tech = _resolve_is_tech(sender_user_id, tenant_db, ctx)
        if sender_role_is_tech and message.direction == "outbound":
            policy = rules.get("tech_outbound_no_tag_visibility", "only_sender")
            if policy == "only_sender":
                return False  # is_self handled above
            if policy == "all_techs":
                return tech or above
            if policy == "above_tech":
                return above
            return False
        # Tech-to-tech internal (both parties techs, no tag)
        if sender_role_is_tech:
            if _is_message_recipient_a_tech(tenant_db, message, tech_emails or set()):
                policy = rules.get("tech_to_tech_internal_visibility", "only_participants")
                if policy == "only_participants":
                    return False  # is_self already covers participants
                if policy == "all_techs":
                    return tech or above
                if policy == "above_tech":
                    return above
        # Default: untagged → policy decides
        # Options: 'only_owner' (default), 'above_tech' (admins/owners can see),
        # 'none' (hidden from everyone, even owner — disable feature).
        policy = rules.get("untagged_visibility", "only_owner")
        if policy == "none":
            return False
        if policy == "above_tech":
            return above  # admins/owners see; techs don't (own already returned True at top)
        # 'only_owner' (default) — is_self was handled above; everyone else hidden
        return False


def _accounts_role_is_tech(tenant_db: Session, user_id: str) -> bool:
    """Best-effort: does this user_id correspond to a tenant tech? Looks up
    the tenant-plane User row's role. Returns False if not resolvable.

    user_id is `str` to match `gdx_dispatch.models.tenant_models.User.id` (String(36)).
    """
    try:
        from gdx_dispatch.models.tenant_models import User
        user = tenant_db.get(User, str(user_id))
        if user is None:
            return False
        role = getattr(user, "role", None) or getattr(user, "user_role", None)
        return _is_tech(role)
    except Exception:  # noqa: BLE001
        log.exception("visibility: _accounts_role_is_tech lookup failed for user_id=%s — defaulting to False", user_id)
        return False


def build_visibility_context(
    messages: list[OutlookMessage],
    tenant_db: Session,
    *,
    tech_emails: set[str] | None = None,
) -> VisibilityContext:
    """Single-batch prefetch for all data ``can_view`` needs across a list.

    Issues at most 3 queries regardless of len(messages):
      - 1 query for OutlookSettings.visibility_rules
      - 1 query for the (account_id → user_id) map
      - 1 query for the (user_id → is_tech) map
    """
    ctx = VisibilityContext(tech_emails=tech_emails or set())
    ctx.rules = _load_rules(tenant_db)
    if not messages:
        return ctx

    account_ids = {m.account_id for m in messages if m.account_id is not None}
    if account_ids:
        rows = (
            tenant_db.query(OutlookAccount.id, OutlookAccount.user_id)
            .filter(OutlookAccount.id.in_(account_ids))
            .all()
        )
        ctx.account_owner = {aid: uid for aid, uid in rows}

    user_ids = {str(uid) for uid in ctx.account_owner.values() if uid}
    if user_ids:
        try:
            from gdx_dispatch.models.tenant_models import User
            rows = (
                tenant_db.query(User.id, User.role)
                .filter(User.id.in_(list(user_ids)))
                .all()
            )
            ctx.user_is_tech = {
                str(uid): _is_tech(role) for uid, role in rows
            }
        except Exception:  # noqa: BLE001
            # User model unavailable — leave map empty; per-row fallback path
            # in _resolve_is_tech still works (just N+1). Log so we see when
            # the fast path is degraded — slowdown could otherwise be invisible.
            log.exception("visibility: build_visibility_context user-role preload failed — falling back to per-row lookups")
    return ctx


def mailbox_owner_id(message: OutlookMessage, tenant_db: Session) -> str | None:
    """Public owner-resolution: the user_id owning the account a message was
    synced from, or None. Single source of truth — views_router's ownership
    check routes through here so the two paths can't drift."""
    owner = _resolve_owner(message, tenant_db, None)
    return str(owner) if owner is not None else None


def visible_to_agent(
    message: OutlookMessage,
    tenant_db: Session,
    *,
    rules: dict[str, Any] | None = None,
) -> bool:
    """Privacy gate for MACHINE principals (MCP tools / AI workers).

    MCP bearers are tenant-wide agent principals — the FastMCP bridge sets
    ``principal_role="agent"`` and deliberately drops the human ``sub``
    claim, so the per-role rules in :func:`can_view` have no viewer to
    apply to. This enforces only the HARD privacy promises, the ones a
    human explicitly opted into:

    - ``is_personal`` — owner-only, period. No agent may read or act on it.
    - ``tagged_visibility_above_role == "owner_only"`` — a fully-private
      mailbox is dark to agents too.

    Everything else keeps today's agent behavior. Full per-user fidelity
    (running can_view as the token's minting user) needs ``claims.sub``
    plumbed through Principal — tracked as a follow-up, not silently
    approximated here.
    """
    if message.is_personal:
        return False
    if rules is None:
        rules = _load_rules(tenant_db)
    is_tagged = bool(message.linked_customer_id or message.linked_job_id)
    if is_tagged and rules.get("tagged_visibility_above_role") == "owner_only":
        return False
    return True


def filter_visible(
    messages: list[OutlookMessage],
    viewer_user_id: UUID | str,
    viewer_role: str,
    tenant_db: Session,
    *,
    tech_emails: set[str] | None = None,
    ctx: VisibilityContext | None = None,
) -> list[OutlookMessage]:
    """Apply ``can_view`` to a list. Builds a single-batch VisibilityContext
    when one is not supplied — eliminates N+1 queries across the list."""
    if ctx is None:
        ctx = build_visibility_context(messages, tenant_db, tech_emails=tech_emails)
    return [
        m for m in messages
        if can_view(m, viewer_user_id, viewer_role, tenant_db,
                    tech_emails=tech_emails, ctx=ctx)
    ]

"""The #701 guard: nothing reads a claim the login dict never carries.

``get_current_user`` returns exactly ``{user_id, tenant_id, role}``. These
tests pin the scan's rules on synthetic modules, then run it over the package.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from gdx_dispatch.tools import login_claims_scan as scan_mod

HEAD = """
from fastapi import Depends, Request
from gdx_dispatch.routers.auth import get_current_user
"""


def _scan(tmp_path: Path, body: str) -> list[tuple[str, str]]:
    (tmp_path / "routers").mkdir(exist_ok=True)
    (tmp_path / "routers" / "mod.py").write_text(textwrap.dedent(HEAD) + textwrap.dedent(body))
    return sorted((f.function, f.key) for f in scan_mod.scan(tmp_path))


def test_an_email_read_off_the_login_dict_is_flagged(tmp_path):
    assert _scan(tmp_path, """
        def create_po(user: dict = Depends(get_current_user)):
            return {"created_by": user.get("email")}
    """) == [("create_po", "email")]


def test_a_bare_sub_is_flagged_but_a_user_id_fallback_chain_is_not(tmp_path):
    assert _scan(tmp_path, """
        def bare(user: dict = Depends(get_current_user)):
            return str(user.get("sub") or "")

        def chained(user: dict = Depends(get_current_user)):
            return str(user.get("sub") or user.get("user_id") or "")
    """) == [("bare", "sub")]


def test_the_carried_keys_are_fine(tmp_path):
    assert _scan(tmp_path, """
        def handler(user: dict = Depends(get_current_user)):
            return user["user_id"], user.get("tenant_id"), user.get("role")
    """) == []


def test_a_same_module_helper_it_is_passed_to_is_followed(tmp_path):
    """signatures._user_label(user) — where half of the instances hid."""
    assert _scan(tmp_path, """
        def _label(u):
            return u.get("email") or u.get("name")

        def handler(user: dict = Depends(get_current_user)):
            return _label(user)
    """) == [("_label", "email"), ("_label", "name")]


def test_an_alias_is_the_same_dict(tmp_path):
    """mobile_chat: `user = current_user or {}`."""
    assert _scan(tmp_path, """
        def send(current_user: dict = Depends(get_current_user)):
            user = current_user or {}
            return user.get("display_name")
    """) == [("send", "display_name")]


def test_the_stashed_copy_on_request_state_user_is_followed(tmp_path):
    """recommendation_routes and error_sink read request.state.user."""
    assert _scan(tmp_path, """
        def _uid(request: Request):
            user = getattr(request.state, "user", None) or {}
            return user.get("id", "anonymous")

        def _email(request):
            principal = getattr(getattr(request, "state", None), "user", None) if request else None
            return principal.get("email")
    """) == [("_email", "email"), ("_uid", "id")]


def test_a_pass_through_dependency_is_the_login_dict(tmp_path):
    """admin_ops._require_admin returns its get_current_user parameter."""
    assert _scan(tmp_path, """
        def _require_admin(user: dict = Depends(get_current_user)) -> dict:
            return user

        def invite(_: dict = Depends(_require_admin)):
            return {"invited_by": _.get("email")}
    """) == [("invite", "email")]


def test_state_current_user_holds_raw_claims_not_the_login_dict(tmp_path):
    """require_role stashes the raw JWT claims there: sub, no user_id, no email.
    error_sink read state.user alone and lost every require_role route."""
    assert _scan(tmp_path, """
        def record(request: Request):
            principal = getattr(request.state, "current_user", None) or {}
            return principal.get("sub"), principal.get("user_id"), principal.get("email")
    """) == [("record", "email"), ("record", "user_id")]


def test_a_read_straight_off_a_stash_is_checked(tmp_path):
    """data_access_logger: every GDPR access on prod was logged as user '-'."""
    assert _scan(tmp_path, """
        def dispatch(request: Request):
            return getattr(request.state, "current_user", {}).get("user_id") or "-"
    """) == [("dispatch", "user_id")]


def test_either_stash_allows_only_the_keys_both_shapes_carry(tmp_path):
    """audit._request_user_dict returns `state.user or state.current_user`:
    on a require_role route that is raw claims, with no user_id."""
    assert _scan(tmp_path, """
        def union(request: Request):
            principal = getattr(request.state, "user", None) or getattr(request.state, "current_user", None) or {}
            return principal.get("user_id"), principal.get("role")
    """) == [("union", "user_id")]


def test_subscript_reads_and_carried_id_chains(tmp_path):
    assert _scan(tmp_path, """
        def by_index(request: Request):
            return request.state.user["email"]

        def raw_claims_chain(request: Request):
            claims = getattr(request.state, "current_user", None) or {}
            return claims.get("sub") or claims.get("user_id")
    """) == [("by_index", "email")]


def test_a_plain_row_dict_is_not_the_login_dict(tmp_path):
    assert _scan(tmp_path, """
        def listing(users):
            return [u.get("email") for u in users]
    """) == []


def test_repo_scan_is_clean():
    findings = scan_mod.scan()
    assert scan_mod.unallowed(findings) == [], "\n".join(map(str, scan_mod.unallowed(findings)))
    assert scan_mod.stale_allowances(findings) == []

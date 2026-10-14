"""Canonical role normalization — core/roles.py (role-name canonicalization #2)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gdx_dispatch.core import roles as R
from gdx_dispatch.core.permissions import BUILTIN_ROLES


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("tech", "technician"), ("technician", "technician"), ("TECH", "technician"),
        ("dispatch", "dispatcher"), ("dispatcher", "dispatcher"),
        ("  Owner ", "owner"), ("admin", "admin"),
        (None, ""), ("", ""), ("unknown_role", "unknown_role"),
    ],
)
def test_normalize_role(raw, expected):
    assert R.normalize_role(raw) == expected


@pytest.mark.parametrize(
    "role,expected",
    [
        ("owner", True), ("admin", True), ("dispatch", True), ("dispatcher", True),
        ("manager", True), ("superadmin", False), ("super_admin", False), ("super-admin", False),
        ("tech", False), ("technician", False), ("viewer", False), ("sales", False),
        ("user", False), ("", False),
    ],
)
def test_is_dispatch_manager(role, expected):
    assert R.is_dispatch_manager(role) is expected


@pytest.mark.parametrize(
    "role,expected",
    [
        ("owner", True), ("superadmin", False), ("super-admin", False),
        ("admin", False), ("dispatcher", False), ("tech", False),
    ],
)
def test_is_role_admin_actor(role, expected):
    assert R.is_role_admin_actor(role) is expected


def test_is_technician_both_spellings():
    assert R.is_technician("tech") and R.is_technician("technician")
    assert not R.is_technician("dispatcher")


def test_is_admin_tier():
    for r in ("owner", "admin", "OWNER", " Admin "):
        assert R.is_admin_tier(r)
    for r in ("dispatcher", "tech", "sales", "viewer"):
        assert not R.is_admin_tier(r)


def test_canonical_constants_match_builtin_role_keys():
    # Canonical (long) constants must be exactly the BUILTIN_ROLES keys so the
    # RBAC catalog and the normalizer never drift apart.
    canonical = {R.OWNER, R.ADMIN, R.DISPATCHER, R.TECHNICIAN, R.SALES, R.ACCOUNTING, R.VIEWER}
    assert canonical == set(BUILTIN_ROLES.keys())


def test_aliases_resolve_to_canonical_constants():
    # Every alias target must be a defined canonical constant (no typos).
    valid = {
        R.OWNER, R.ADMIN, R.DISPATCHER, R.TECHNICIAN, R.SALES,
        R.ACCOUNTING, R.VIEWER, R.MANAGER,
    }
    for target in R.ROLE_ALIASES.values():
        assert target in valid, f"alias target {target!r} is not a canonical constant"


# ── GDXA-1010: frontend/src/constants/roles.js mirrors core/roles.py ─────────
# Pins the role constants and the alias table only; normalizeRole's behaviour
# is pinned by frontend/src/constants/__tests__/roles.spec.js.

_ROLES_JS = Path(R.__file__).resolve().parents[1] / "frontend" / "src" / "constants" / "roles.js"
_JS_CONST = re.compile(r"export const ([A-Z_]+) = '([^']*)';")
_JS_ALIAS = re.compile(r"(\w+): ([A-Z_]+),")


def _js_role_constants_and_aliases() -> tuple[dict[str, str], dict[str, str]]:
    # Strict: a line the parser cannot read fails here, never slips past.
    src = _ROLES_JS.read_text(encoding="utf-8")
    consts: dict[str, str] = {}
    for line in src.splitlines():
        if re.match(r"export const [A-Z_]+\b", line):
            m = _JS_CONST.fullmatch(line.strip())
            assert m, f"unparseable role constant in roles.js: {line!r}"
            consts[m.group(1)] = m.group(2)
    block = re.search(r"^const ALIASES = \{\n(.*?)^\};", src, re.M | re.S)
    assert block, "ALIASES block not found in roles.js"
    aliases: dict[str, str] = {}
    for line in block.group(1).splitlines():
        m = _JS_ALIAS.fullmatch(line.strip())
        assert m and m.group(2) in consts, f"unparseable alias in roles.js: {line!r}"
        aliases[m.group(1)] = consts[m.group(2)]
    return consts, aliases


def test_roles_js_constants_match_roles_py():
    py_consts = {
        name: value for name, value in vars(R).items()
        if name.isupper() and isinstance(value, str)
    }
    js_consts, _ = _js_role_constants_and_aliases()
    assert js_consts == py_consts


def test_roles_js_aliases_match_roles_py():
    _, js_aliases = _js_role_constants_and_aliases()
    assert js_aliases == dict(R.ROLE_ALIASES)


# ── #45: write paths normalize users.role to ONE canonical (long) vocabulary ──

def test_user_write_models_canonicalize_role():
    from gdx_dispatch.routers.users import InviteIn, RoleChangeIn, UserCreateIn
    pw = "CorrectHorse1!"
    # Short input → long canonical on every user-write model.
    assert UserCreateIn(email="a@b.co", name="X", password=pw, role="tech").role == "technician"
    assert UserCreateIn(email="a@b.co", name="X", password=pw, role="dispatch").role == "dispatcher"
    assert RoleChangeIn(role="tech").role == "technician"
    assert InviteIn(email="a@b.co", name="X", role="dispatch").role == "dispatcher"
    # Long input passes through unchanged (idempotent).
    assert UserCreateIn(email="a@b.co", name="X", password=pw, role="technician").role == "technician"
    assert RoleChangeIn(role="dispatcher").role == "dispatcher"
    # admin/owner/sales identical in both vocabularies.
    for r in ("admin", "owner", "sales"):
        assert UserCreateIn(email="a@b.co", name="X", password=pw, role=r).role == r

"""Auth router package.

The original ``gdx_dispatch/routers/auth.py`` was promoted to a package; its body
lives in ``gdx_dispatch.routers.auth.core``. The names re-exported below are the
ones external callers reach via ``from gdx_dispatch.routers.auth import X`` or
``from gdx_dispatch.routers import auth``. Each line was justified by a
``grep -rn`` over the entire gdx_dispatch/ tree before commit; any addition here
should be paired with a comment naming the importer.

The auth-cluster siblings (gateway, login_picker, oauth2, sso, scim,
signup, pats, admin_pats, pats_support) live as sub-modules of this
package but are NOT re-exported at the package level — they are reached
via ``gdx_dispatch.routers.auth.<name>`` directly. Re-exporting them here would
force them into the eager-import path of every caller that does
``from gdx_dispatch.routers.auth import get_current_user`` (~50 routers), which
widens the failure surface from one sub-module to the whole package
without giving any caller a shorter import path.

Test code that needs to monkeypatch a function inside this module must
import the implementation directly (``from gdx_dispatch.routers.auth import core``)
and patch on the ``core`` reference — patching this package shim does
not reach the call site, since functions in ``core.py`` resolve names
via that module's own globals. See ``gdx_dispatch/tests/test_auth_router.py``
for the canonical test pattern.
"""

# Names external callers reach. Each line was justified by a grep -rn
# over the entire gdx_dispatch/ tree before this commit; any addition here
# should be paired with a comment naming the importer.
from gdx_dispatch.routers.auth.core import (
    ALG,                       # tests/test_auth_router.py + app.py
    SIGN_KEY,                  # tests/test_auth_router.py
    VERIFY_KEY,                # app.py (`VERIFY_KEY as _VERIFY`)
    RevokeTokenBody,           # tests/test_auth_router.py
    _db_verify_user,           # tests/test_auth_identity_invariants.py
    _denylist_redis_client,    # app.py (idempotency factory + health probe)
    _enforce_tenant_match,     # app.py (tenant-cross-check middleware seam)
    _get_app_denylist,         # tests/test_auth_router.py
    _issue,                    # app.py (tests assert via auth_router)
    admin_revoke_token,        # tests/test_auth_router.py
    get_current_user,          # 50+ routers (the primary auth dep)
    router,                    # app.py (`include_router(auth.router)`)
)

__all__ = [
    "ALG",
    "SIGN_KEY",
    "VERIFY_KEY",
    "RevokeTokenBody",
    "_db_verify_user",
    "_denylist_redis_client",
    "_enforce_tenant_match",
    "_get_app_denylist",
    "_issue",
    "admin_revoke_token",
    "get_current_user",
    "router",
]

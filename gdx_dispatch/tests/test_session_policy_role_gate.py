"""Role gate for PATCH /api/session-policy — only admin-tier may set the
tenant-wide idle timeout. (Read is open to any signed-in user.)"""
from gdx_dispatch.routers.session_policy import _is_admin


def test_is_admin():
    assert _is_admin("owner") is True
    assert _is_admin("admin") is True
    assert _is_admin("superadmin") is True
    assert _is_admin("OWNER") is True  # case-insensitive
    # Non-admin roles must be denied.
    assert _is_admin("technician") is False
    assert _is_admin("dispatcher") is False
    assert _is_admin("user") is False
    assert _is_admin("") is False
    assert _is_admin(None) is False


if __name__ == "__main__":
    test_is_admin()
    print("ok")

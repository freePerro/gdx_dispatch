"""Bootstrap credential-handoff pins (CodeQL alert 104 companion).

Two properties, tested at the unit level (`main()` needs a DB; the
log.warning call itself is a one-liner over these):

- `_admin_banner` includes the password ONLY when generated=True — an
  operator-supplied GDX_ADMIN_PASSWORD is never echoed into the banner.
- `_resolve_admin_password` derives (password, generated) from ONE
  expression, so the banner's claim can't diverge from where the password
  actually came from. The set-but-EMPTY case is the shipped `.env.template`
  default and MUST generate — an `in os.environ` test here once made the
  banner mislabel a generated password as operator-supplied.
"""
from gdx_dispatch.tools.bootstrap_app import _admin_banner, _resolve_admin_password

SECRET = "hunter2-operator-supplied"


def test_generated_password_is_shown():
    banner = _admin_banner(
        "initial admin account created",
        "admin@example.com",
        SECRET,
        generated=True,
    )
    assert SECRET in banner
    assert "admin@example.com" in banner


def test_env_supplied_password_is_not_in_banner():
    banner = _admin_banner(
        "initial admin account created",
        "admin@example.com",
        SECRET,
        generated=False,
    )
    assert SECRET not in banner
    assert "GDX_ADMIN_PASSWORD" in banner  # points the operator at the source


def test_resolve_unset_env_generates(monkeypatch):
    monkeypatch.delenv("GDX_ADMIN_PASSWORD", raising=False)
    password, generated = _resolve_admin_password()
    assert generated is True
    assert password  # non-empty random value


def test_resolve_empty_env_generates(monkeypatch):
    # .env.template ships `GDX_ADMIN_PASSWORD=`; compose env_file injects it
    # as an empty string. This MUST behave exactly like unset, or the banner
    # hides a password nobody holds (silent owner lockout on first boot).
    monkeypatch.setenv("GDX_ADMIN_PASSWORD", "")
    password, generated = _resolve_admin_password()
    assert generated is True
    assert password


def test_resolve_supplied_env_is_used_and_marked_not_generated(monkeypatch):
    monkeypatch.setenv("GDX_ADMIN_PASSWORD", SECRET)
    password, generated = _resolve_admin_password()
    assert generated is False
    assert password == SECRET

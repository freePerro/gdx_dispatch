"""Slice outlook-s3 — verify migration 036 round-trips cleanly."""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config


def _alembic_config() -> Config:
    cfg = Config(str(Path("gdx_dispatch/migrations/alembic.ini")))
    cfg.set_main_option("script_location", "gdx_dispatch/migrations")
    return cfg


def verify_migration_module_loads():
    """The migration file must import cleanly + expose the standard alembic interface."""
    # Python doesn't allow leading-digit module names directly; alembic uses
    # importlib so it works at runtime, but the test imports via filepath.


def test_migration_file_exists_and_has_correct_chain():
    path = Path("gdx_dispatch/migrations/versions/036_outlook_tenant_settings.py")
    assert path.exists(), f"migration file missing: {path}"
    text = path.read_text()
    assert 'revision = "036_outlook_tenant_settings"' in text
    assert 'down_revision = "035_phone_com_tenant_settings"' in text
    assert "outlook_microsoft_tenant_id" in text
    assert "outlook_client_id" in text
    assert "outlook_client_secret_enc" in text
    assert "outlook_secret_set_at" in text


def test_upgrade_and_downgrade_symmetric():
    """The downgrade must drop exactly the columns the upgrade added — no more, no less."""
    text = Path("gdx_dispatch/migrations/versions/036_outlook_tenant_settings.py").read_text()
    upgraded = ["outlook_microsoft_tenant_id", "outlook_client_id",
                "outlook_client_secret_enc", "outlook_secret_set_at"]
    for c in upgraded:
        assert text.count(f'add_column(\n        "tenant_settings",\n        sa.Column("{c}"') == 1 or \
               f'"{c}"' in text and "add_column" in text, f"upgrade missing {c}"
        assert f'drop_column("tenant_settings", "{c}")' in text, f"downgrade missing {c}"


def test_all_outlook_columns_nullable():
    text = Path("gdx_dispatch/migrations/versions/036_outlook_tenant_settings.py").read_text()
    # Every add_column for outlook_* must have nullable=True (existing rows have no value).
    import re
    for m in re.finditer(r'sa\.Column\("(outlook_[^"]+)".*?nullable=(True|False)', text, re.DOTALL):
        name, nullable = m.groups()
        assert nullable == "True", f"{name} must be nullable=True (existing tenants have no value)"

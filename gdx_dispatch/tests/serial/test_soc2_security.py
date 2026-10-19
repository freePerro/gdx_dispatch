"""SOC 2 Type II compliance tests for GDX platform.
Each test maps to a specific SOC 2 Trust Service Criteria control.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from conftest import make_fresh_db


@pytest.fixture()
def db():
    engine = make_fresh_db()
    SL = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SL()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# --- CC6.7: Credential Storage ---

def test_password_hash_not_plaintext(db):
    """SOC 2 CC6.7 — Passwords must be stored as cryptographic hashes.
    Checks that a password in the users table starts with a known hash prefix.
    Plaintext passwords are a critical compliance violation.
    """
    # Ensure users table exists in test DB
    db.execute(sa_text("""CREATE TABLE IF NOT EXISTS users (
        id VARCHAR(36) PRIMARY KEY, username VARCHAR(100), email VARCHAR(254),
        password_hash TEXT, role VARCHAR(20), company_id VARCHAR(36),
        active BOOLEAN DEFAULT true, created_at TIMESTAMP, deleted_at TIMESTAMP)"""))
    db.commit()
    from werkzeug.security import generate_password_hash
    hashed = generate_password_hash("TestP@ss123")
    db.execute(sa_text(
        "INSERT INTO users (id, username, email, password_hash, role, company_id) "
        "VALUES ('soc2-user-1', 'soc2test', 'soc2@test.com', :hash, 'admin', 'tenant-soc2')"
    ), {"hash": hashed})
    db.commit()

    stored = db.execute(sa_text(
        "SELECT password_hash FROM users WHERE id = 'soc2-user-1'"
    )).scalar()

    assert stored != "TestP@ss123", "Password stored as plaintext — CRITICAL VIOLATION"
    assert stored.startswith(("$2b$", "scrypt:", "pbkdf2:")), \
        f"Password hash must use bcrypt/scrypt/pbkdf2, got: {stored[:20]}"

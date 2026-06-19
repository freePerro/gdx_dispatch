# Secrets Rotation Procedures

## DB Credential Rotation

```bash
# 1. Generate new password
NEW_PASS=$(openssl rand -base64 32)

# 2. Create new PG role with new password (dual-credential window)
psql -U postgres -c "ALTER USER gdx WITH PASSWORD '$NEW_PASS';"

# 3. Update control plane encrypted credential
curl -X POST http://localhost:8001/api/admin/rotate-db-credential \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"tenant_slug": "TENANT", "new_password": "'"$NEW_PASS"'"}'

# 4. Health check (engine registry will reconnect on next request)
curl http://localhost:8001/health

# 5. Verify old password is gone (it was already replaced in step 2)
```

## JWT Signing Key Rotation (zero-downtime)

Uses JWKSKeyStore in gdx_dispatch/core/jwks.py which supports multiple active public keys.

```python
# 1. Generate new key pair
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private_pem = private_key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
)
public_pem = private_key.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)

# 2. Add new key to JWKS (keep old key — existing tokens still valid)
from gdx_dispatch.core.jwks import key_store
key_store.add_key("kid-2026-Q2", private_pem, public_pem)

# 3. Set new key as default for signing
# (key_store.sign_token will use latest-added key by default)

# 4. Wait 15 minutes (all outstanding 15-min access tokens expire)
# Refresh tokens are longer-lived but verify_token tries all public keys

# 5. Remove old public key from JWKS after confirming no errors
```

JWKS endpoint: `GET /api/.well-known/jwks.json` — clients can discover all active public keys.

## Stripe API Key Rotation

```bash
# 1. Create new restricted key in Stripe dashboard (same permissions)
# 2. Set new key in environment / secrets manager
export STRIPE_SECRET_KEY=sk_live_new_key_here

# 3. Deploy new key to all app servers
# 4. Run test charge to verify
curl -X POST https://api.stripe.com/v1/charges \
  -u "$STRIPE_SECRET_KEY:" \
  -d amount=100 -d currency=usd -d source=tok_visa

# 5. Revoke old key in Stripe dashboard
```

## Redis AUTH Password Rotation

```bash
# 1. Set new AUTH password (Redis 6+ supports ACL)
redis-cli ACL SETUSER default >new_password on ~* &* +@all

# 2. Update REDIS_URL in environment:
#    redis://:new_password@redis:6379/0

# 3. Rolling restart of app servers to pick up new password
# 4. Verify: redis-cli -a new_password ping → PONG

# 5. Remove old password from ACL
redis-cli ACL SETUSER default >old_password off
```

## Fernet Encryption Key Rotation (PII columns)

The `EncryptedString` TypeDecorator in `gdx_dispatch/core/pii.py` uses a Fernet key derived from `PII_ENCRYPTION_KEY` env var.

```bash
# 1. Generate new key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Set PII_ENCRYPTION_KEY_NEW env var (dual-key read window):
#    Modify EncryptedString.process_result_value() to try new key first, fall back to old key

# 3. Run re-encryption script:
python3 gdx/scripts/reencrypt_pii.py --old-key $OLD_KEY --new-key $NEW_KEY

# 4. After re-encryption verified, set PII_ENCRYPTION_KEY=$NEW_KEY, remove old key

# 5. Re-encryption script should:
#    - Iterate all Customer rows
#    - Decrypt each PII field with old key
#    - Re-encrypt with new key
#    - Commit in batches of 100
```

Rotation schedule: Quarterly or immediately after any suspected key compromise.

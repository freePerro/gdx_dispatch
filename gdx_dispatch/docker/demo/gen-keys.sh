#!/usr/bin/env bash
# Generate fresh demo-only secrets. Appends KEY=value lines to stdout.
# Usage:  bash gen-keys.sh >> .env.demo   (then dedupe the REPLACE placeholders)
set -euo pipefail

hex()    { python3 -c "import secrets;print(secrets.token_hex($1))"; }
fernet() { python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"; }
uuid()   { python3 -c "import uuid;print(uuid.uuid4())"; }

echo "# --- generated $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"
echo "SECRET_KEY=$(hex 32)"
echo "JWT_SECRET=$(hex 32)"
echo "FERNET_KEY=$(fernet)"
echo "GDX_FERNET_KEY=$(fernet)"
echo "MASTER_ENCRYPTION_KEY=$(fernet)"
echo "DB_PASSWORD=$(hex 16)"
echo "GDX_ADMIN_PASSWORD=demo-$(hex 4)"
echo "GDX_TENANT_ID=$(uuid)"
TID=$(uuid); echo "GDX_DEFAULT_TENANT_ID=$TID"

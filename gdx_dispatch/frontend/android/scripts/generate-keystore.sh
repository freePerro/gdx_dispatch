#!/usr/bin/env bash
# Generates a production Android release signing keystore for GDX Dispatch.
#
# Usage:
#   ./generate-keystore.sh [keystore_path] [key_alias]
#
set -euo pipefail

KEYSTORE_PATH="${1:-release.jks}"
KEY_ALIAS="${2:-gdx-dispatch}"

echo "Generating release keystore at: ${KEYSTORE_PATH} (alias: ${KEY_ALIAS})"

keytool -genkeypair \
  -v \
  -keystore "${KEYSTORE_PATH}" \
  -alias "${KEY_ALIAS}" \
  -keyalg RSA \
  -keysize 2048 \
  -validity 10000 \
  -storetype JKS

echo ""
echo "Keystore created successfully!"
echo "To build a signed release bundle, run:"
echo "  export GDX_KEYSTORE_PATH=$(realpath "${KEYSTORE_PATH}")"
echo "  export GDX_KEYSTORE_PASSWORD=<your-store-password>"
echo "  export GDX_KEY_ALIAS=${KEY_ALIAS}"
echo "  export GDX_KEY_PASSWORD=<your-key-password>"
echo "  ./gradlew bundleRelease"

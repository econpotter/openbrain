#!/usr/bin/env bash
# setup-caddy.sh — Extract and trust Caddy's internal CA cert on this machine.
# Run this on each client that connects to the openbrain MCP server.
# The Caddy container must already be running on the server.
#
# Usage: bash scripts/setup-caddy.sh <server-hostname>
# Example: bash scripts/setup-caddy.sh potter.nicholas-fuji.nord
set -euo pipefail

SERVER="${1:-}"
if [ -z "$SERVER" ]; then
    echo "Usage: bash scripts/setup-caddy.sh <server-hostname>"
    echo "Example: bash scripts/setup-caddy.sh potter.nicholas-fuji.nord"
    exit 1
fi

CERT_FILE="caddy-root.crt"

echo "=== Trusting Caddy internal CA from $SERVER ==="

echo "[1/2] Fetching root cert from server..."
ssh "$SERVER" "docker exec caddy cat /data/caddy/pki/authorities/local/root.crt" > "$CERT_FILE"
echo "      Saved to $CERT_FILE"

echo "[2/2] Installing cert into system trust store..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    sudo security add-trusted-cert -d -r trustRoot \
        -k /Library/Keychains/System.keychain "$CERT_FILE"
else
    sudo cp "$CERT_FILE" /usr/local/share/ca-certificates/caddy-root.crt
    sudo update-ca-certificates
fi
echo "      Done."

rm -f "$CERT_FILE"

echo
echo "=== Certificate trusted ==="
echo "You can now connect to: https://$SERVER/mcp"

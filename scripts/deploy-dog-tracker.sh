#!/usr/bin/env bash
# Deploy Dog Tracker Service to DGX2
#
# Usage:
#   ./scripts/deploy-dog-tracker.sh [DGX2_HOST]
#
# Defaults:
#   DGX2_HOST = 192.168.1.52  (override via arg or DGX2_HOST env var)
#
# What it does:
#   1. SCP the service files to DGX2
#   2. Install Python dependencies
#   3. Optionally start the service

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_DIR="$REPO_DIR/services/dog-tracker"
DGX2_HOST="${1:-${DGX2_HOST:-192.168.1.49}}"
DGX2_USER="${DGX2_USER:-dgx2}"
REMOTE_DIR="/home/$DGX2_USER/dog-tracker"

echo "=== Dog Tracker Deploy ==="
echo "Target: $DGX2_USER@$DGX2_HOST:$REMOTE_DIR"
echo ""

# Create remote directory
echo "[1/3] Creating remote directory..."
ssh "$DGX2_USER@$DGX2_HOST" "mkdir -p $REMOTE_DIR"

# Copy files
echo "[2/3] Copying service files..."
scp -r \
    "$SERVICE_DIR/service.py" \
    "$SERVICE_DIR/config.yaml" \
    "$SERVICE_DIR/requirements.txt" \
    "$DGX2_USER@$DGX2_HOST:$REMOTE_DIR/"

# Install dependencies in venv
echo "[3/3] Setting up venv and installing dependencies..."
ssh "$DGX2_USER@$DGX2_HOST" "cd $REMOTE_DIR && python3 -m venv venv && ./venv/bin/pip install --no-cache-dir -r requirements.txt"

echo ""
echo "=== Deploy complete ==="
echo ""

# Push credentials from Keychain to remote env file
SCRIPT_DIR_ABS="$(cd "$(dirname "$0")" && pwd)"
if command -v security &>/dev/null; then
    echo "[+] Pushing credentials to remote .env file..."
    NVR_PASS=$(security find-generic-password -s "nvr/dog-tracker" -a "admin" -w 2>/dev/null || true)
    if [ -n "$NVR_PASS" ]; then
        ssh "$DGX2_USER@$DGX2_HOST" "cat > $REMOTE_DIR/.env << 'ENVEOF'
RTSP_URL=rtsp://admin:${NVR_PASS}@192.168.1.46:554/h264Preview_01_main
ONVIF_HOST=192.168.1.46
ONVIF_PORT=8000
ONVIF_PASSWORD=${NVR_PASS}
ENVEOF
chmod 600 $REMOTE_DIR/.env"
        echo "    Credentials written to $REMOTE_DIR/.env (mode 600)"
    else
        echo "    WARNING: NVR password not in Keychain. Run: ./scripts/nvr-credential.sh store"
    fi
fi

echo ""
echo "To start the service:"
echo "  ssh $DGX2_USER@$DGX2_HOST 'cd $REMOTE_DIR && set -a && source .env && set +a && ./venv/bin/python3 service.py'"

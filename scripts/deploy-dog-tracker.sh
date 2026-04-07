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
echo "To configure camera, edit on DGX2:"
echo "  ssh $DGX2_USER@$DGX2_HOST"
echo "  nano $REMOTE_DIR/config.yaml"
echo ""
echo "To start the service:"
echo "  ssh $DGX2_USER@$DGX2_HOST 'cd $REMOTE_DIR && ./venv/bin/python3 service.py'"
echo ""
echo "Or with env overrides:"
echo "  ssh $DGX2_USER@$DGX2_HOST 'cd $REMOTE_DIR && RTSP_URL=rtsp://... ONVIF_HOST=... ./venv/bin/python3 service.py'"

#!/usr/bin/env bash
# Store / retrieve NVR credentials via macOS Keychain.
#
# Usage:
#   ./scripts/nvr-credential.sh store           # store password in Keychain
#   ./scripts/nvr-credential.sh get             # print password to stdout
#   ./scripts/nvr-credential.sh rtsp [channel]  # print full RTSP URL (default ch01)
#   ./scripts/nvr-credential.sh env             # export env vars for dog-tracker service
#
# Keychain entry: service=nvr/dog-tracker, account=admin

set -euo pipefail

SERVICE="nvr/dog-tracker"
ACCOUNT="admin"
NVR_HOST="192.168.1.46"
ONVIF_PORT=8000

case "${1:-help}" in
  store)
    echo "Storing NVR password in Keychain (service=$SERVICE, account=$ACCOUNT)"
    read -rsp "NVR password: " PASS
    echo
    # Delete existing entry if present (ignore error)
    security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" 2>/dev/null || true
    security add-generic-password -s "$SERVICE" -a "$ACCOUNT" -w "$PASS"
    echo "Stored."
    ;;

  get)
    security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w
    ;;

  rtsp)
    CHANNEL="${2:-01}"
    PASS=$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w)
    echo "rtsp://${ACCOUNT}:${PASS}@${NVR_HOST}:554/h264Preview_${CHANNEL}_main"
    ;;

  env)
    PASS=$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w)
    echo "export RTSP_URL=\"rtsp://${ACCOUNT}:${PASS}@${NVR_HOST}:554/h264Preview_01_main\""
    echo "export ONVIF_HOST=\"${NVR_HOST}\""
    echo "export ONVIF_PORT=\"${ONVIF_PORT}\""
    echo "export ONVIF_PASSWORD=\"${PASS}\""
    ;;

  help|*)
    echo "Usage: $0 {store|get|rtsp [channel]|env}"
    echo ""
    echo "  store          Store NVR password in macOS Keychain"
    echo "  get            Print password to stdout"
    echo "  rtsp [channel] Print full RTSP URL (default channel 01)"
    echo "  env            Print export statements for dog-tracker env vars"
    ;;
esac

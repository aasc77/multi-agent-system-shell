#!/usr/bin/env bash
# notify.sh -- macOS text-to-speech notification helper
#
# Usage:
#   ./scripts/notify.sh "message to speak"
#   ./scripts/notify.sh -v Samantha "message"
#
# Default voice: Samantha (natural-sounding US English)

VOICE="Samantha"
if [ "$1" = "-v" ]; then
    VOICE="$2"
    shift 2
fi

say -v "$VOICE" "$*" &

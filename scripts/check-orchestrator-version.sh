#!/usr/bin/env bash
# check-orchestrator-version.sh — detect running-binary vs on-disk
# code drift for the orchestrator process (issue #31).
#
# Sends a NATS request to `system.orchestrator.version`, parses the
# response's `startup_sha`, and compares it against `git rev-parse
# --short HEAD` in the repo root. Prints a human-readable status
# line and exits with a machine-readable code:
#
#   0  in sync          — running binary matches on-disk HEAD
#   1  drift detected   — running binary is a different SHA than on-disk
#   2  indeterminate    — unknown running SHA, nats timeout, git missing,
#                         or any other condition where we can't make the
#                         in-sync-vs-drift determination
#
# Intended use: operator diagnostic during incident triage, or as a
# gate step in CI / cron / watchdog scripts that need to bail out
# when the orchestrator is running stale code. Motivation: the #28
# smoke test arc wasted two runs because we chased ghost failures
# against a stale orchestrator binary.
#
# Usage:
#   scripts/check-orchestrator-version.sh [NATS_URL]
#
# NATS_URL defaults to nats://127.0.0.1:4222. Override to point at a
# remote orchestrator via e.g. `nats://angels-macbook-pro.local:4222`.
#
# Requirements: `nats` CLI (installed by scripts/setup-nats.sh),
# `jq` (ubiquitous, installed via brew on macOS), `git` in PATH.

set -u

NATS_URL="${1:-nats://127.0.0.1:4222}"
SUBJECT="system.orchestrator.version"
REQUEST_TIMEOUT="2s"

# --- Paths ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- Exit-code constants ---
EXIT_IN_SYNC=0
EXIT_DRIFT=1
EXIT_INDETERMINATE=2

err() { echo "check-version: $*" >&2; }

# --- Preflight: required tools ---
for tool in nats jq git; do
    if ! command -v "$tool" &>/dev/null; then
        err "required tool '$tool' not found on PATH"
        exit "$EXIT_INDETERMINATE"
    fi
done

# --- 1. Read running orchestrator's startup SHA via NATS request ---
# `nats req` writes the reply body to stdout. Use --raw so we don't
# get the CLI's timestamp/header wrapper — just the JSON payload.
response=$(nats --server="$NATS_URL" req --raw --timeout="$REQUEST_TIMEOUT" "$SUBJECT" '' 2>/dev/null)
rc=$?
if [ "$rc" -ne 0 ] || [ -z "$response" ]; then
    err "no response from $SUBJECT on $NATS_URL (timeout ${REQUEST_TIMEOUT} or no subscriber)"
    err "is the orchestrator running and connected to NATS?"
    exit "$EXIT_INDETERMINATE"
fi

running_sha=$(echo "$response" | jq -r '.startup_sha // empty' 2>/dev/null)
running_sha_full=$(echo "$response" | jq -r '.startup_sha_full // empty' 2>/dev/null)
started_at=$(echo "$response" | jq -r '.started_at // empty' 2>/dev/null)
running_pid=$(echo "$response" | jq -r '.pid // empty' 2>/dev/null)

if [ -z "$running_sha" ] || [ "$running_sha" = "null" ]; then
    err "orchestrator responded but reports startup_sha=null"
    err "running process has no git info (detached HEAD? shallow clone? non-git install?)"
    err "pid=${running_pid:-unknown} started_at=${started_at:-unknown}"
    exit "$EXIT_INDETERMINATE"
fi

# --- 2. Read on-disk SHA via git ---
disk_sha_full=$(cd "$REPO_ROOT" && git rev-parse HEAD 2>/dev/null)
if [ -z "$disk_sha_full" ]; then
    err "git rev-parse HEAD failed in $REPO_ROOT (not a git checkout?)"
    exit "$EXIT_INDETERMINATE"
fi
disk_sha="${disk_sha_full:0:7}"

# --- 3. Compare + report ---
if [ "$running_sha_full" = "$disk_sha_full" ]; then
    echo "✓ in sync: orchestrator pid=${running_pid} running ${running_sha} (booted ${started_at})"
    exit "$EXIT_IN_SYNC"
fi

# Drift detected. Count commits between the two SHAs so operators
# get a one-line signal of how far the binary is behind or ahead.
# Best-effort — if either SHA isn't in the local history, skip the
# delta and just report the mismatch.
#
# Symmetric check: running could be behind (typical case — orchestrator
# launched before the new commit) OR ahead (rarer — operator rolled
# disk back via reset or force-pushed a different branch). Both paths
# emit a clean delta string.
delta=""
if git -C "$REPO_ROOT" merge-base --is-ancestor "$running_sha_full" "$disk_sha_full" 2>/dev/null; then
    commits_behind=$(git -C "$REPO_ROOT" rev-list --count "${running_sha_full}..${disk_sha_full}" 2>/dev/null)
    if [ -n "$commits_behind" ] && [ "$commits_behind" -gt 0 ] 2>/dev/null; then
        delta=" (${commits_behind} commits behind)"
    fi
elif git -C "$REPO_ROOT" merge-base --is-ancestor "$disk_sha_full" "$running_sha_full" 2>/dev/null; then
    commits_ahead=$(git -C "$REPO_ROOT" rev-list --count "${disk_sha_full}..${running_sha_full}" 2>/dev/null)
    if [ -n "$commits_ahead" ] && [ "$commits_ahead" -gt 0 ] 2>/dev/null; then
        delta=" (${commits_ahead} commits ahead of disk)"
    fi
fi

echo "✗ DRIFT: orchestrator pid=${running_pid} running ${running_sha}, disk HEAD ${disk_sha}${delta}"
echo "  running:  ${running_sha_full}"
echo "  on disk:  ${disk_sha_full}"
echo "  booted:   ${started_at}"
echo "  fix:      bash scripts/bounce-orchestrator.sh <project>"
exit "$EXIT_DRIFT"

#!/usr/bin/env python3
"""Send a push notification via the Pushover API.

Usage:
    python3 scripts/push-notify.py "Your message here"
    python3 scripts/push-notify.py -t "Custom Title" "Message body"
    python3 scripts/push-notify.py -p 1 "High priority message"

Credentials are read from macOS Keychain (mas:pushover/*) via secure-credential.sh.
Required Keychain entries:
    pushover/app_token  — Application API token from https://pushover.net/apps
    pushover/user_key   — User key from https://pushover.net
"""

import argparse
import subprocess
import sys

import requests

CREDENTIAL_SCRIPT = "~/Repositories/operations/scripts/secure-credential.sh"
PUSHOVER_API = "https://api.pushover.net/1/messages.json"


def get_credential(service: str, account: str) -> str:
    """Retrieve a credential from macOS Keychain via secure-credential.sh."""
    result = subprocess.run(
        ["bash", "-c", f"{CREDENTIAL_SCRIPT} get {service} {account}"],
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value or result.returncode != 0:
        print(f"Error: could not retrieve {service}/{account} from Keychain", file=sys.stderr)
        print(f"Store it with: {CREDENTIAL_SCRIPT} store {service} {account}", file=sys.stderr)
        sys.exit(1)
    return value


def send_push(message: str, title: str | None = None, priority: int = 0) -> None:
    """Send a push notification via Pushover."""
    app_token = get_credential("pushover", "app_token")
    user_key = get_credential("pushover", "user_key")

    data = {
        "token": app_token,
        "user": user_key,
        "message": message,
        "priority": priority,
    }
    if title:
        data["title"] = title

    resp = requests.post(PUSHOVER_API, data=data)
    result = resp.json()

    if result.get("status") == 1:
        print(f"Push sent: request={result.get('request')}")
    else:
        errors = result.get("errors", [])
        print(f"Push failed: {errors}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Send push notification via Pushover")
    parser.add_argument("message", help="Notification message text")
    parser.add_argument("-t", "--title", help="Notification title (defaults to app name)")
    parser.add_argument(
        "-p", "--priority", type=int, default=0,
        help="Priority: -2 (silent) to 2 (emergency). Default: 0",
    )
    args = parser.parse_args()

    send_push(args.message, title=args.title, priority=args.priority)


if __name__ == "__main__":
    main()

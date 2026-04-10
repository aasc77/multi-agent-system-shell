#!/usr/bin/env python3
"""Send an SMS notification via Twilio.

Usage:
    python3 scripts/sms-notify.py "Your message here"
    python3 scripts/sms-notify.py -t +15551234567 "Message to specific number"

Credentials are read from macOS Keychain (mas:twilio/*) via secure-credential.sh.
"""

import argparse
import subprocess
import sys


CREDENTIAL_SCRIPT = "~/Repositories/operations/scripts/secure-credential.sh"


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


def send_sms(to_number: str, message: str) -> None:
    """Send an SMS via the Twilio API."""
    from twilio.rest import Client

    account_sid = get_credential("twilio", "account_sid")
    auth_token = get_credential("twilio", "auth_token")
    from_number = get_credential("twilio", "from_number")

    client = Client(account_sid, auth_token)
    msg = client.messages.create(
        body=message,
        from_=from_number,
        to=to_number,
    )
    print(f"SMS sent: SID={msg.sid}, to={to_number}")


def main():
    parser = argparse.ArgumentParser(description="Send SMS via Twilio")
    parser.add_argument("message", help="Message text to send")
    parser.add_argument(
        "-t", "--to",
        help="Recipient phone number (E.164 format). Defaults to user's number from Keychain.",
    )
    args = parser.parse_args()

    to_number = args.to or get_credential("twilio", "user_phone")
    send_sms(to_number, args.message)


if __name__ == "__main__":
    main()

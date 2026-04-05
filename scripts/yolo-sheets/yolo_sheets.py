#!/usr/bin/env python3
"""
YOLO-World Camera Class Manager — Google Sheets ↔ DGX sync tool.

Manages per-camera YOLO-World detection classes via a Google Sheet.
Reads the sheet and pushes updated class config to the DGX detection server.

Usage:
    # First-time auth (opens browser for OAuth consent)
    python3 scripts/yolo-sheets/yolo_sheets.py auth

    # Create the Google Sheet pre-populated with current DGX config
    python3 scripts/yolo-sheets/yolo_sheets.py create

    # Sync: read sheet and push config to DGX
    python3 scripts/yolo-sheets/yolo_sheets.py sync

    # Show current sheet config
    python3 scripts/yolo-sheets/yolo_sheets.py show

Environment:
    DGX_HOST -- DGX hostname/IP (default: 192.168.1.51)
"""

import argparse
import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET_FILE = os.path.join(SCRIPT_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.json")
SHEET_ID_FILE = os.path.join(SCRIPT_DIR, "sheet_id.txt")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

DGX_HOST = os.environ.get("DGX_HOST", "192.168.1.51")
DGX_DETECT_PORT = 8501
DGX_SSH = f"dgx@{DGX_HOST}"

# Current default config (from DGX)
DEFAULT_CONFIG = [
    {"camera": "backyard", "channel": "01", "scene": "outdoor", "classes": "person, dog, tree, grass, fence, shed, house, sky, wall", "enabled": "yes"},
    {"camera": "right_side", "channel": "02", "scene": "street", "classes": "person, dog, car, truck, bicycle, motorcycle, house, fence, wall", "enabled": "yes"},
    {"camera": "deck", "channel": "03", "scene": "outdoor", "classes": "person, dog, tree, grass, fence, shed, house, sky, wall", "enabled": "yes"},
    {"camera": "front_right", "channel": "04", "scene": "street", "classes": "person, dog, car, truck, bicycle, motorcycle, house, fence, wall", "enabled": "yes"},
    {"camera": "kitchen", "channel": "05", "scene": "indoor", "classes": "person, dog, chair, table, refrigerator, bottle, door", "enabled": "yes"},
    {"camera": "front_left", "channel": "06", "scene": "street", "classes": "person, dog, car, truck, bicycle, motorcycle, house, fence, wall", "enabled": "yes"},
    {"camera": "front_door", "channel": "07", "scene": "street", "classes": "person, dog, car, truck, bicycle, motorcycle, house, fence, wall", "enabled": "yes"},
    {"camera": "living_room", "channel": "08", "scene": "indoor", "classes": "person, dog, chair, table, refrigerator, bottle, door", "enabled": "yes"},
    {"camera": "carport_gate", "channel": "09", "scene": "carport", "classes": "person, dog, car, gate, fence, wall, stairs, porch, hose, tools", "enabled": "yes"},
]

SHEET_TITLE = "YOLO-World Camera Classes"


# ---------------------------------------------------------------------------
# Google Sheets auth
# ---------------------------------------------------------------------------

def get_credentials() -> Credentials:
    """Get or refresh Google OAuth2 credentials."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_FILE):
                print(f"Error: {CLIENT_SECRET_FILE} not found", file=sys.stderr)
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        print(f"Token saved to {TOKEN_FILE}")

    return creds


def get_sheets_service():
    """Build Google Sheets API service."""
    creds = get_credentials()
    return build("sheets", "v4", credentials=creds)


def get_sheet_id() -> str:
    """Read saved sheet ID."""
    if not os.path.exists(SHEET_ID_FILE):
        print("Error: Sheet not created yet. Run: yolo_sheets.py create", file=sys.stderr)
        sys.exit(1)
    with open(SHEET_ID_FILE) as f:
        return f.read().strip()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_auth():
    """Authenticate with Google (opens browser)."""
    creds = get_credentials()
    print(f"Authenticated successfully. Token: {TOKEN_FILE}")


def cmd_create():
    """Create Google Sheet pre-populated with DGX camera config."""
    service = get_sheets_service()

    # Create spreadsheet
    spreadsheet = service.spreadsheets().create(body={
        "properties": {"title": SHEET_TITLE},
        "sheets": [{
            "properties": {
                "title": "Cameras",
                "gridProperties": {"frozenRowCount": 1},
            },
        }],
    }).execute()

    sheet_id = spreadsheet["spreadsheetId"]
    sheet_url = spreadsheet["spreadsheetUrl"]

    # Save sheet ID
    with open(SHEET_ID_FILE, "w") as f:
        f.write(sheet_id)

    # Write header + data
    header = ["Camera Name", "Channel", "Scene Type", "Classes", "Enabled"]
    rows = [header]
    for cam in DEFAULT_CONFIG:
        rows.append([cam["camera"], cam["channel"], cam["scene"], cam["classes"], cam["enabled"]])

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Cameras!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()

    # Format header row (bold)
    sheet_gid = spreadsheet["sheets"][0]["properties"]["sheetId"]
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_gid, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            },
            {
                "autoResizeDimensions": {
                    "dimensions": {"sheetId": sheet_gid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 5},
                }
            },
        ]},
    ).execute()

    print(f"Sheet created: {sheet_url}")
    print(f"Sheet ID saved to: {SHEET_ID_FILE}")
    return sheet_id, sheet_url


def cmd_show():
    """Show current sheet config."""
    service = get_sheets_service()
    sheet_id = get_sheet_id()

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="Cameras!A1:E20",
    ).execute()

    rows = result.get("values", [])
    if not rows:
        print("Sheet is empty.")
        return

    # Print as table
    col_widths = [max(len(str(row[i])) if i < len(row) else 0 for row in rows) for i in range(5)]
    for row in rows:
        line = "  ".join(str(row[i] if i < len(row) else "").ljust(col_widths[i]) for i in range(5))
        print(line)


def cmd_sync():
    """Read sheet and push class config to DGX detection server."""
    import subprocess

    service = get_sheets_service()
    sheet_id = get_sheet_id()

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="Cameras!A2:E20",
    ).execute()

    rows = result.get("values", [])
    if not rows:
        print("No camera data found in sheet.")
        return

    # Parse config from sheet
    cameras = []
    for row in rows:
        if len(row) < 5:
            continue
        camera, channel, scene, classes, enabled = row[0], row[1], row[2], row[3], row[4]
        if enabled.strip().lower() not in ("yes", "true", "1"):
            continue
        class_list = [c.strip() for c in classes.split(",") if c.strip()]
        cameras.append({
            "camera": camera.strip(),
            "channel": channel.strip(),
            "scene": scene.strip(),
            "classes": class_list,
        })

    if not cameras:
        print("No enabled cameras found.")
        return

    print(f"Syncing {len(cameras)} cameras to DGX...")

    # Build the Python config dict for the DGX stream server
    scene_classes = {}
    camera_scenes = {}
    for cam in cameras:
        scene = cam["scene"]
        camera_scenes[cam["camera"]] = {"channel": cam["channel"], "scene": scene}
        if scene not in scene_classes:
            scene_classes[scene] = set()
        scene_classes[scene].update(cam["classes"])

    # Convert sets to sorted lists
    for scene in scene_classes:
        scene_classes[scene] = sorted(scene_classes[scene])

    config_json = json.dumps({
        "scene_classes": scene_classes,
        "cameras": camera_scenes,
    }, indent=2)

    print("\nConfig to push:")
    print(config_json)

    # Write config to DGX
    config_path = "/tmp/yolo_camera_config.json"
    remote_config_path = "/home/dgx/voice-services/yolo/camera_config.json"

    with open(config_path, "w") as f:
        f.write(config_json)

    # SCP config to DGX
    scp_cmd = [
        "scp", "-o", "StrictHostKeyChecking=no", "-o", "IdentitiesOnly=yes",
        config_path, f"{DGX_SSH}:{remote_config_path}",
    ]
    result = subprocess.run(scp_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: SCP failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"\nConfig pushed to {DGX_SSH}:{remote_config_path}")

    # Notify DGX agent to reload
    print("Config pushed. DGX stream server needs to reload the config.")
    print(f"Remote config path: {remote_config_path}")
    print("\nTo apply: DGX agent should reload the stream server with the new config.")

    return cameras


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="YOLO-World Camera Class Manager")
    parser.add_argument("command", choices=["auth", "create", "show", "sync"],
                        help="auth: Google OAuth | create: make sheet | show: display config | sync: push to DGX")
    args = parser.parse_args()

    if args.command == "auth":
        cmd_auth()
    elif args.command == "create":
        cmd_create()
    elif args.command == "show":
        cmd_show()
    elif args.command == "sync":
        cmd_sync()


if __name__ == "__main__":
    main()

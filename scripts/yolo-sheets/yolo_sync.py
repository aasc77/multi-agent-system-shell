#!/usr/bin/env python3
"""
YOLO Sync — reads camera class config from Google Sheet and pushes to DGX.

Wrapper around the generic gsheets-config tool. Reads the "yolo-classes" sheet,
transforms it into camera_config.json, SCPs to DGX, and optionally restarts
the stream server.

Usage:
    python3 scripts/yolo-sheets/yolo_sync.py              # sync sheet to DGX
    python3 scripts/yolo-sheets/yolo_sync.py --dry-run     # show config without pushing
    python3 scripts/yolo-sheets/yolo_sync.py --restart      # sync + restart stream server

Environment:
    DGX_HOST -- DGX hostname/IP (default: 192.168.1.51)
"""

import argparse
import json
import os
import subprocess
import sys

# Add gsheets-config to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gsheets-config"))
import gsheets_config

SHEET_ALIAS = "yolo-classes"
DGX_HOST = os.environ.get("DGX_HOST", "192.168.1.51")
DGX_SSH = f"dgx@{DGX_HOST}"
REMOTE_CONFIG_PATH = "/home/dgx/voice-services/yolo/camera_config.json"
LOCAL_TMP = "/tmp/yolo_camera_config.json"


def read_yolo_config() -> list[dict]:
    """Read camera config from Google Sheet."""
    registry = gsheets_config.list_sheets()
    entry = registry.get(SHEET_ALIAS)
    if not entry:
        print(f"Error: '{SHEET_ALIAS}' not in registry. Run yolo_sheets.py create first.", file=sys.stderr)
        sys.exit(1)

    return gsheets_config.read_sheet(entry["sheet_id"])


def transform_to_dgx_config(rows: list[dict]) -> dict:
    """Transform sheet rows into DGX camera_config.json format."""
    scene_classes = {}
    cameras = {}

    for row in rows:
        camera = row.get("Camera Name", "").strip()
        channel = row.get("Channel", "").strip()
        scene = row.get("Scene Type", "").strip()
        classes = row.get("Classes", "").strip()
        enabled = row.get("Enabled", "").strip().lower()

        if not camera or enabled not in ("yes", "true", "1"):
            continue

        class_list = [c.strip() for c in classes.split(",") if c.strip()]
        cameras[camera] = {"channel": channel, "scene": scene}

        if scene not in scene_classes:
            scene_classes[scene] = set()
        scene_classes[scene].update(class_list)

    # Convert sets to sorted lists
    for scene in scene_classes:
        scene_classes[scene] = sorted(scene_classes[scene])

    return {"scene_classes": scene_classes, "cameras": cameras}


def push_to_dgx(config: dict) -> bool:
    """SCP config JSON to DGX."""
    with open(LOCAL_TMP, "w") as f:
        json.dump(config, f, indent=2)

    result = subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=no", "-o", "IdentitiesOnly=yes",
         LOCAL_TMP, f"{DGX_SSH}:{REMOTE_CONFIG_PATH}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error: SCP failed: {result.stderr}", file=sys.stderr)
        return False
    return True


def restart_stream_server() -> bool:
    """Restart the YOLO stream server on DGX."""
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "IdentitiesOnly=yes",
         DGX_SSH, "pkill -f stream_server.py; sleep 1; cd ~/voice-services/yolo && nohup python3 stream_server.py > /tmp/stream_server.log 2>&1 &"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="Sync YOLO camera classes from Google Sheet to DGX")
    parser.add_argument("--dry-run", action="store_true", help="Show config without pushing")
    parser.add_argument("--restart", action="store_true", help="Restart DGX stream server after sync")
    args = parser.parse_args()

    print("Reading Google Sheet...")
    rows = read_yolo_config()
    if not rows:
        print("No data in sheet.")
        return

    config = transform_to_dgx_config(rows)
    enabled_count = len(config["cameras"])
    scene_count = len(config["scene_classes"])

    print(f"\n{enabled_count} cameras, {scene_count} scene types:")
    print(json.dumps(config, indent=2))

    if args.dry_run:
        print("\n(dry run — not pushing)")
        return

    print(f"\nPushing to {DGX_SSH}:{REMOTE_CONFIG_PATH}...")
    if push_to_dgx(config):
        print("Config pushed successfully.")
    else:
        sys.exit(1)

    if args.restart:
        print("Restarting stream server...")
        if restart_stream_server():
            print("Stream server restarted.")
        else:
            print("Warning: restart may have failed. Check DGX manually.", file=sys.stderr)

    print("\nDone. DGX should pick up new classes on next detection request.")


if __name__ == "__main__":
    main()

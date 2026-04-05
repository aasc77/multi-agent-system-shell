#!/usr/bin/env python3
"""
Google Sheets Config Manager — generic tool for managing config via Google Sheets.

Create, read, write, and list Google Sheets for any config purpose.
OAuth credentials are stored alongside this script.

Usage:
    # First-time auth (opens browser)
    python3 scripts/gsheets-config/gsheets_config.py auth

    # Create a new sheet
    python3 scripts/gsheets-config/gsheets_config.py create --name "My Config" --columns "Name,Value,Enabled"

    # List managed sheets
    python3 scripts/gsheets-config/gsheets_config.py list

    # Read sheet data as JSON
    python3 scripts/gsheets-config/gsheets_config.py read --sheet-id <id>
    python3 scripts/gsheets-config/gsheets_config.py read --alias yolo-classes

    # Write data to a sheet
    python3 scripts/gsheets-config/gsheets_config.py write --sheet-id <id> --data '[["a","b"],["c","d"]]'

    # Delete a sheet from the registry (does not delete from Google)
    python3 scripts/gsheets-config/gsheets_config.py unregister --alias <alias>
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
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET_FILE = os.path.join(SCRIPT_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.json")
REGISTRY_FILE = os.path.join(SCRIPT_DIR, "registry.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ---------------------------------------------------------------------------
# Auth
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

    return creds


def get_service():
    """Build Google Sheets API service."""
    return build("sheets", "v4", credentials=get_credentials())


# ---------------------------------------------------------------------------
# Registry — tracks managed sheets by alias
# ---------------------------------------------------------------------------

def _load_registry() -> dict:
    if os.path.exists(REGISTRY_FILE):
        with open(REGISTRY_FILE) as f:
            return json.load(f)
    return {}


def _save_registry(registry: dict):
    with open(REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=2)


def register_sheet(alias: str, sheet_id: str, name: str, columns: list[str]):
    """Register a sheet in the local registry."""
    registry = _load_registry()
    registry[alias] = {
        "sheet_id": sheet_id,
        "name": name,
        "columns": columns,
    }
    _save_registry(registry)


def resolve_sheet_id(sheet_id: str | None, alias: str | None) -> str:
    """Resolve a sheet ID from --sheet-id or --alias."""
    if sheet_id:
        return sheet_id
    if alias:
        registry = _load_registry()
        entry = registry.get(alias)
        if not entry:
            print(f"Error: alias '{alias}' not found in registry. Run 'list' to see available.", file=sys.stderr)
            sys.exit(1)
        return entry["sheet_id"]
    print("Error: provide --sheet-id or --alias", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# API functions (importable by other scripts and MCP server)
# ---------------------------------------------------------------------------

def create_sheet(name: str, columns: list[str], alias: str | None = None, data: list[list[str]] | None = None) -> dict:
    """Create a new Google Sheet with given columns. Returns {sheet_id, url, alias}."""
    service = get_service()

    spreadsheet = service.spreadsheets().create(body={
        "properties": {"title": name},
        "sheets": [{
            "properties": {
                "title": "Sheet1",
                "gridProperties": {"frozenRowCount": 1},
            },
        }],
    }).execute()

    sheet_id = spreadsheet["spreadsheetId"]
    sheet_url = spreadsheet["spreadsheetUrl"]
    sheet_gid = spreadsheet["sheets"][0]["properties"]["sheetId"]

    # Write header + optional data
    rows = [columns]
    if data:
        rows.extend(data)

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Sheet1!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()

    # Bold header
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
                    "dimensions": {"sheetId": sheet_gid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": len(columns)},
                }
            },
        ]},
    ).execute()

    # Register
    if alias is None:
        alias = name.lower().replace(" ", "-").replace("/", "-")[:40]
    register_sheet(alias, sheet_id, name, columns)

    return {"sheet_id": sheet_id, "url": sheet_url, "alias": alias}


def read_sheet(sheet_id: str, include_header: bool = False) -> list[dict] | list[list[str]]:
    """Read all data from a sheet. Returns list of dicts (col_name -> value) if header exists."""
    service = get_service()

    # Get the first sheet's name dynamically
    meta = service.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties.title").execute()
    sheet_name = meta["sheets"][0]["properties"]["title"]

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!A1:Z1000",
    ).execute()

    rows = result.get("values", [])
    if not rows:
        return []

    if include_header:
        return rows

    header = rows[0]
    data = []
    for row in rows[1:]:
        entry = {}
        for i, col in enumerate(header):
            entry[col] = row[i] if i < len(row) else ""
        data.append(entry)
    return data


def write_sheet(sheet_id: str, data: list[list[str]], start_cell: str = "A2"):
    """Write data rows to a sheet (preserves header)."""
    service = get_service()

    # Get the first sheet's name dynamically
    meta = service.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties.title").execute()
    sheet_name = meta["sheets"][0]["properties"]["title"]

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!{start_cell}",
        valueInputOption="RAW",
        body={"values": data},
    ).execute()

    return {"rows_written": len(data)}


def list_sheets() -> dict:
    """Return the registry of managed sheets."""
    return _load_registry()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_auth(args):
    get_credentials()
    print(f"Authenticated. Token: {TOKEN_FILE}")


def cmd_create(args):
    columns = [c.strip() for c in args.columns.split(",")]
    result = create_sheet(
        name=args.name,
        columns=columns,
        alias=args.alias,
    )
    print(f"Sheet created: {result['url']}")
    print(f"Alias: {result['alias']}")
    print(f"Sheet ID: {result['sheet_id']}")


def cmd_list(args):
    registry = list_sheets()
    if not registry:
        print("No managed sheets.")
        return
    for alias, info in registry.items():
        print(f"  {alias}: {info['name']} ({info['sheet_id']})")
        print(f"    Columns: {', '.join(info['columns'])}")


def cmd_read(args):
    sheet_id = resolve_sheet_id(args.sheet_id, args.alias)
    data = read_sheet(sheet_id, include_header=args.raw)
    print(json.dumps(data, indent=2))


def cmd_write(args):
    sheet_id = resolve_sheet_id(args.sheet_id, args.alias)
    data = json.loads(args.data)
    result = write_sheet(sheet_id, data)
    print(f"Wrote {result['rows_written']} rows.")


def cmd_unregister(args):
    registry = _load_registry()
    if args.alias in registry:
        del registry[args.alias]
        _save_registry(registry)
        print(f"Removed '{args.alias}' from registry.")
    else:
        print(f"Alias '{args.alias}' not found.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Google Sheets Config Manager")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("auth", help="Authenticate with Google")

    p_create = sub.add_parser("create", help="Create a new sheet")
    p_create.add_argument("--name", required=True, help="Sheet title")
    p_create.add_argument("--columns", required=True, help="Comma-separated column names")
    p_create.add_argument("--alias", help="Short alias for the sheet (auto-generated if omitted)")

    sub.add_parser("list", help="List managed sheets")

    p_read = sub.add_parser("read", help="Read sheet data as JSON")
    p_read.add_argument("--sheet-id", help="Google Sheet ID")
    p_read.add_argument("--alias", help="Sheet alias from registry")
    p_read.add_argument("--raw", action="store_true", help="Return raw rows including header")

    p_write = sub.add_parser("write", help="Write data to sheet")
    p_write.add_argument("--sheet-id", help="Google Sheet ID")
    p_write.add_argument("--alias", help="Sheet alias from registry")
    p_write.add_argument("--data", required=True, help="JSON array of arrays")

    p_unreg = sub.add_parser("unregister", help="Remove sheet from registry")
    p_unreg.add_argument("--alias", required=True, help="Alias to remove")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "auth": cmd_auth,
        "create": cmd_create,
        "list": cmd_list,
        "read": cmd_read,
        "write": cmd_write,
        "unregister": cmd_unregister,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()

"""
Google Sheets storage for fitness check-ins.

Sheet columns: timestamp, user_id, username, current_weight, last_week_weight,
               starting_weight, proud_of, can_work_on
"""

import os
import json
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

HEADERS = [
    "Timestamp",
    "User ID",
    "Username",
    "Current Weight",
    "Last Week Weight",
    "Starting Weight",
    "Proud Of",
    "Can Work On",
]


def _get_client() -> gspread.Client:
    """Build an authenticated gspread client from env vars."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # Fall back to a local file for development
        creds = Credentials.from_service_account_file(
            os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
            scopes=SCOPES,
        )
    return gspread.authorize(creds)


def _get_sheet() -> gspread.Worksheet:
    """Open (or create) the worksheet."""
    client = _get_client()
    spreadsheet_id = os.environ["GOOGLE_SHEET_ID"]
    sheet_name = os.environ.get("GOOGLE_SHEET_TAB", "Check-ins")

    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS)

    # Ensure headers exist if sheet is empty
    if ws.row_count == 0 or ws.row_values(1) != HEADERS:
        ws.insert_row(HEADERS, 1)

    return ws


def log_checkin(
    user_id: int,
    username: str,
    current_weight: str,
    last_week_weight: str,
    starting_weight: str,
    proud_of: str,
    can_work_on: str,
) -> None:
    """Append one check-in row to the sheet."""
    ws = _get_sheet()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ws.append_row(
        [
            ts,
            str(user_id),
            username,
            current_weight,
            last_week_weight,
            starting_weight,
            proud_of,
            can_work_on,
        ],
        value_input_option="USER_ENTERED",
    )


def get_latest_checkins(limit: int = 20) -> list[dict]:
    """Return the most recent `limit` check-ins as a list of dicts."""
    ws = _get_sheet()
    records = ws.get_all_records()  # list of dicts keyed by header
    return records[-limit:] if len(records) > limit else records


def get_starting_weight(user_id: int) -> str | None:
    """Return the starting weight from this user's most recent check-in, or None."""
    ws = _get_sheet()
    records = ws.get_all_records()
    # Scan in reverse to find their latest entry
    for record in reversed(records):
        if str(record.get("User ID")) == str(user_id):
            weight = record.get("Starting Weight", "").strip()
            return weight if weight else None
    return None


def get_sheet_url() -> str:
    """Return a direct link to the spreadsheet."""
    client = _get_client()
    spreadsheet_id = os.environ["GOOGLE_SHEET_ID"]
    spreadsheet = client.open_by_key(spreadsheet_id)
    return spreadsheet.url

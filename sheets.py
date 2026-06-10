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


def get_user_prefill(user_id: int) -> tuple[str | None, str | None]:
    """Return (starting_weight, last_week_weight) for a user.

    starting_weight: from the user's FIRST check-in (its Starting Weight,
                     falling back to its Current Weight).
    last_week_weight: the Current Weight from the user's MOST RECENT check-in.

    Note: gspread returns numeric cells as int/float, so values are coerced
    to str before use.
    """
    ws = _get_sheet()
    records = ws.get_all_records()
    user_records = [r for r in records if str(r.get("User ID")) == str(user_id)]
    if not user_records:
        return None, None

    def _clean(value) -> str | None:
        s = str(value).strip()
        return s if s else None

    first, latest = user_records[0], user_records[-1]
    starting = _clean(first.get("Starting Weight", "")) or _clean(first.get("Current Weight", ""))
    last_week = _clean(latest.get("Current Weight", ""))
    return starting, last_week


def parse_weight(value) -> float | None:
    """Extract a numeric weight from a cell like '185 lbs' or 184.6."""
    s = "".join(c for c in str(value) if c.isdigit() or c == ".")
    try:
        return float(s)
    except ValueError:
        return None


def get_user_history(user_id: int) -> list[dict]:
    """Return all of a user's check-ins, oldest first.

    Each item: {"date": datetime (UTC), "weight": float}.
    Rows with unparseable timestamps or weights are skipped.
    """
    ws = _get_sheet()
    records = ws.get_all_records()
    history: list[dict] = []
    for r in records:
        if str(r.get("User ID")) != str(user_id):
            continue
        try:
            dt = datetime.strptime(
                str(r.get("Timestamp", "")), "%Y-%m-%d %H:%M UTC"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        weight = parse_weight(r.get("Current Weight"))
        if weight is None:
            continue
        history.append({"date": dt, "weight": weight})
    history.sort(key=lambda h: h["date"])
    return history


def get_sheet_url() -> str:
    """Return a direct link to the spreadsheet."""
    client = _get_client()
    spreadsheet_id = os.environ["GOOGLE_SHEET_ID"]
    spreadsheet = client.open_by_key(spreadsheet_id)
    return spreadsheet.url

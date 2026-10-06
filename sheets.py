"""
Google Sheets storage for fitness check-ins.

Sheet columns: timestamp, user_id, username, current_weight, last_week_weight,
               starting_weight, proud_of, can_work_on

Credentials are resolved in this order:
  1. GOOGLE_CREDENTIALS_JSON env var (full service-account JSON — recommended)
  2. GOOGLE_CREDENTIALS_FILE path (local development)
  3. Application Default Credentials (e.g. the Cloud Run runtime service
     account — only works if that account has been granted Sheets access
     and the token carries a Sheets-accepted scope)
"""

import os
import json
import logging
import threading
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger(__name__)

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

# Per-user photo state lives in its own tab so the append-only Check-ins tab
# (and its existing rows) stay untouched. One row per user, updated in place.
PHOTO_HEADERS = [
    "User ID",
    "Username",
    "Consent",       # "yes" once the user opts into public photo sharing
    "Day1 Ref",      # Discord message id holding the durable Day 1 photo
    "Pending URL",   # a not-yet-consented photo's (short-lived) signed CDN URL
    "Updated At",
    # What the user was actually doing when they attached the pending photo.
    # Without this, consent granted later can only guess, and it guessed wrong:
    # a pending /day1 posted a before/after instead of resetting the baseline,
    # and a pending /photo-replace lost its date and posted as a photo for today.
    "Pending Kind",  # "checkin" | "day1" | "replace"  (blank = legacy/checkin)
    "Pending Date",  # the /photo-replace target date, when Pending Kind=replace
]

# One row per photo ever archived — the history the Photos tab (one row per
# user) can't express. /collage reads it, /photo-replace edits it. Append-mostly:
# a replaced photo's row is marked inactive rather than deleted, so the record of
# what was submitted when survives.
PHOTO_LOG_HEADERS = [
    "Timestamp",     # when the row was written (UTC)
    "User ID",
    "Username",
    "Taken On",      # YYYY-MM-DD — the date the photo represents; the match key
    "Archive Ref",   # message id in ARCHIVE_CHANNEL_ID holding the raw PNG
    "Post Ref",      # message id in the check-in channel, for cleanup on replace
    "Kind",          # "day1" or "progress"
    "Active",        # "yes", or blank once superseded
]

# One row per user: the weight they're aiming for. Its own tab so the append-only
# Check-ins tab stays untouched. The tab name is fixed rather than an env var — a
# fourth GOOGLE_*_TAB would need mirroring across three docs for no real gain.
GOAL_HEADERS = ["User ID", "Username", "Goal Weight", "Set On"]


def _get_client() -> gspread.Client:
    """Build an authenticated gspread client."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    elif os.path.exists(creds_file):
        creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    else:
        import google.auth

        creds, _ = google.auth.default(scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_headers(ws: gspread.Worksheet, headers: list[str]) -> None:
    """Reconcile a worksheet's header row with `headers`, without moving data.

    Columns are only ever appended on the right, so a header row that differs
    only in length still has every shared column in the same position. Three
    cases, and the last one is what makes a rollback safe:

    * identical — nothing to do.
    * sheet is NARROWER (an older sheet, newer code) — widen the header in
      place. Inserting a row instead would push every record down one and leave
      the old header being read as data.
    * sheet is WIDER (a newer sheet, older code — i.e. someone rolled back after
      a schema change) — leave it completely alone. The columns this build knows
      about are still where it expects them, and the extra ones are ignored by
      get_all_records(). Rewriting the header here would destroy the newer
      columns' data and strand any rollback.

    Anything else is an unrecognisable header, so insert a correct one.
    """
    existing = ws.row_values(1) if ws.row_count else []
    if existing == headers:
        return

    if existing and headers[: len(existing)] == existing:
        if ws.col_count < len(headers):
            ws.add_cols(len(headers) - ws.col_count)
        for col, header in enumerate(headers[len(existing):], start=len(existing) + 1):
            ws.update_cell(1, col, header)
        return

    if existing[: len(headers)] == headers:
        log.info(
            "%s has %d extra column(s) from a newer schema — leaving them alone",
            ws.title, len(existing) - len(headers),
        )
        return

    ws.insert_row(headers, 1)


# One client and one Worksheet handle per tab, built once per process. Opening a
# tab from scratch is four serial Google round trips (auth, spreadsheet
# metadata, tab lookup, header check) — ~0.7s of the ~0.85s a /checkin prefill
# read took, against a budget of at most 1.4s, so a warm instance still timed
# out and opened the modal with Last Week's Weight blank. Reusing the handle
# leaves only the read itself (~0.2s). google-auth refreshes the token in place.
_client: gspread.Client | None = None
_tabs: dict[str, gspread.Worksheet] = {}
_tabs_lock = threading.Lock()


def _open_tab(sheet_name: str, headers: list[str]) -> gspread.Worksheet:
    """Open (or create) a worksheet, reusing the process-wide handle."""
    global _client
    ws = _tabs.get(sheet_name)
    if ws is not None:
        return ws
    with _tabs_lock:
        ws = _tabs.get(sheet_name)
        if ws is not None:
            return ws
        if _client is None:
            _client = _get_client()
        spreadsheet = _client.open_by_key(os.environ["GOOGLE_SHEET_ID"])

        try:
            ws = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(
                title=sheet_name, rows=1000, cols=len(headers)
            )
            ws.append_row(headers)

        _ensure_headers(ws, headers)
        _tabs[sheet_name] = ws
        return ws


def _get_sheet() -> gspread.Worksheet:
    """Open (or create) the worksheet."""
    return _open_tab(os.environ.get("GOOGLE_SHEET_TAB", "Check-ins"), HEADERS)


def _get_photos_sheet() -> gspread.Worksheet:
    """Open (or create) the per-user Photos worksheet."""
    return _open_tab(os.environ.get("GOOGLE_PHOTOS_TAB", "Photos"), PHOTO_HEADERS)


def _get_photo_log_sheet() -> gspread.Worksheet:
    """Open (or create) the append-only Photo Log worksheet."""
    return _open_tab(
        os.environ.get("GOOGLE_PHOTO_LOG_TAB", "Photo Log"), PHOTO_LOG_HEADERS
    )


def _get_goals_sheet() -> gspread.Worksheet:
    """Open (or create) the per-user Goals worksheet."""
    return _open_tab("Goals", GOAL_HEADERS)


def warmup() -> None:
    """Open the Check-ins tab off the interaction deadline (keep-warm ping)."""
    _get_sheet()


def append_photo_log(
    user_id: int,
    username: str,
    taken_on: str,
    archive_ref: str,
    post_ref: str = "",
    kind: str = "progress",
) -> None:
    """Record one archived photo."""
    ws = _get_photo_log_sheet()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    row = [
        ts,
        str(user_id),
        username,
        taken_on,
        str(archive_ref),
        str(post_ref or ""),
        kind,
        "yes",
    ]
    # RAW keeps the 18-digit snowflakes exact — USER_ENTERED coerces them to
    # floats and silently loses the low digits.
    ws.append_row(row, value_input_option="RAW")


def get_photo_log(user_id: int, active_only: bool = True) -> list[dict]:
    """A user's archived photos, oldest first by Taken On.

    Each item: {"taken_on": str, "archive_ref": str, "post_ref": str,
    "kind": str}. Rows missing an archive reference are skipped — without one
    there is no image to fetch.
    """
    ws = _get_photo_log_sheet()
    out: list[dict] = []
    for r in ws.get_all_records():
        if str(r.get("User ID")) != str(user_id):
            continue
        if active_only and str(r.get("Active", "")).strip().lower() != "yes":
            continue
        archive_ref = str(r.get("Archive Ref", "")).strip()
        taken_on = str(r.get("Taken On", "")).strip()
        if not archive_ref or not taken_on:
            continue
        out.append(
            {
                "taken_on": taken_on,
                "archive_ref": archive_ref,
                "post_ref": str(r.get("Post Ref", "")).strip(),
                "kind": str(r.get("Kind", "progress")).strip() or "progress",
            }
        )
    out.sort(key=lambda p: p["taken_on"])
    return out


def deactivate_photo_log_row(user_id: int, taken_on: str) -> dict | None:
    """Mark a user's photo for `taken_on` inactive; return the row's data.

    Returns None when there's no active row for that date. When several exist
    (a date replaced more than once) the most recent wins, matching what the
    user last saw.
    """
    ws = _get_photo_log_sheet()
    records = ws.get_all_records()
    match_index = None
    for i, r in enumerate(records):
        if (
            str(r.get("User ID")) == str(user_id)
            and str(r.get("Taken On", "")).strip() == taken_on
            and str(r.get("Active", "")).strip().lower() == "yes"
        ):
            match_index = i
    if match_index is None:
        return None

    r = records[match_index]
    # +2: one for the header row, one for gspread's 1-based indexing.
    ws.update_cell(match_index + 2, PHOTO_LOG_HEADERS.index("Active") + 1, "")
    return {
        "taken_on": str(r.get("Taken On", "")).strip(),
        "archive_ref": str(r.get("Archive Ref", "")).strip(),
        "post_ref": str(r.get("Post Ref", "")).strip(),
        "kind": str(r.get("Kind", "progress")).strip() or "progress",
    }


def get_photo_state(user_id: int) -> dict:
    """A user's photo state: consent, Day 1 ref, and any pending photo.

    ``pending_kind`` says what the user was doing when they attached the pending
    photo ("checkin", "day1" or "replace"), and ``pending_date`` carries the
    target date for a pending "replace". Rows written before those columns
    existed report None, which callers treat as an ordinary check-in photo.

    Missing user / missing fields fall back to consent=False and None refs.
    """
    ws = _get_photos_sheet()
    records = ws.get_all_records()
    row = next(
        (r for r in records if str(r.get("User ID")) == str(user_id)), None
    )
    if not row:
        return {"consent": False, "day1_ref": None, "pending_url": None,
                "pending_kind": None, "pending_date": None}

    def _clean(value) -> str | None:
        s = str(value).strip()
        return s or None

    return {
        "consent": str(row.get("Consent", "")).strip().lower() == "yes",
        "day1_ref": _clean(row.get("Day1 Ref", "")),
        "pending_url": _clean(row.get("Pending URL", "")),
        "pending_kind": _clean(row.get("Pending Kind", "")),
        "pending_date": _clean(row.get("Pending Date", "")),
    }


def upsert_photo_state(user_id: int, username: str, **fields) -> None:
    """Create or update a user's row in the Photos tab.

    `fields` may include any of: consent (bool), day1_ref (str), pending_url
    (str), pending_kind (str), pending_date (str). Pass an empty string to clear
    a cell (e.g. pending_url="").
    """
    ws = _get_photos_sheet()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    updates = {"Username": username, "Updated At": ts}
    if "consent" in fields:
        updates["Consent"] = "yes" if fields["consent"] else ""
    if "day1_ref" in fields:
        updates["Day1 Ref"] = fields["day1_ref"]
    if "pending_url" in fields:
        updates["Pending URL"] = fields["pending_url"]
    if "pending_kind" in fields:
        updates["Pending Kind"] = fields["pending_kind"]
    if "pending_date" in fields:
        updates["Pending Date"] = fields["pending_date"]

    # Locate the user's existing row (column 1 = User ID), else append a fresh one.
    cell = ws.find(str(user_id), in_column=1)
    if cell is None:
        row = [""] * len(PHOTO_HEADERS)
        row[0] = str(user_id)
        for header, value in updates.items():
            row[PHOTO_HEADERS.index(header)] = value
        # RAW so the snowflake User ID is stored verbatim as text (USER_ENTERED
        # would coerce the 18-digit id to a number and lose precision).
        ws.append_row(row, value_input_option="RAW")
        return

    for header, value in updates.items():
        ws.update_cell(cell.row, PHOTO_HEADERS.index(header) + 1, value)


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


def get_user_prefill(user_id: int) -> tuple[str | None, str | None, str | None]:
    """Return (starting_weight, last_week_weight, last_focus) for a user.

    starting_weight: from the user's FIRST check-in (its Starting Weight,
                     falling back to its Current Weight).
    last_week_weight: the Current Weight from the user's MOST RECENT check-in.
    last_focus: the "Can Work On" from that same most recent check-in, so the
                next form can hold the user to it. One read serves all three:
                this runs inside the /checkin modal's time budget, where a
                second Sheets round trip is exactly what cannot be afforded.

    Note: gspread returns numeric cells as int/float, so values are coerced
    to str before use.
    """
    ws = _get_sheet()
    records = ws.get_all_records()
    user_records = [r for r in records if str(r.get("User ID")) == str(user_id)]
    if not user_records:
        return None, None, None

    def _clean(value) -> str | None:
        s = str(value).strip()
        return s if s else None

    first, latest = user_records[0], user_records[-1]
    starting = _clean(first.get("Starting Weight", "")) or _clean(first.get("Current Weight", ""))
    last_week = _clean(latest.get("Current Weight", ""))
    last_focus = _clean(latest.get("Can Work On", ""))
    return starting, last_week, last_focus


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
    """Direct link to the spreadsheet (no API call needed)."""
    return f"https://docs.google.com/spreadsheets/d/{os.environ['GOOGLE_SHEET_ID']}"


# ── Goals ──────────────────────────────────────────────────────────────────────
def get_goal(user_id: int) -> float | None:
    """The user's goal weight, or None when unset, cleared or unparseable."""
    ws = _get_goals_sheet()
    row = next(
        (r for r in ws.get_all_records() if str(r.get("User ID")) == str(user_id)), None
    )
    if not row:
        return None
    return parse_weight(row.get("Goal Weight", ""))


def set_goal(user_id: int, username: str, goal: float) -> None:
    """Create or update the user's Goals row (one per user, like Photos)."""
    ws = _get_goals_sheet()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    values = {"Username": username, "Goal Weight": f"{goal:.1f}", "Set On": ts}

    cell = ws.find(str(user_id), in_column=1)
    if cell is None:
        # RAW so the snowflake User ID is stored verbatim as text (USER_ENTERED
        # would coerce the 18-digit id to a number and lose precision).
        ws.append_row(
            [str(user_id)] + [values[h] for h in GOAL_HEADERS[1:]],
            value_input_option="RAW",
        )
        return
    for header, value in values.items():
        ws.update_cell(cell.row, GOAL_HEADERS.index(header) + 1, value)


def clear_goal(user_id: int) -> bool:
    """Blank the user's goal; returns whether there was one to clear.

    The row stays (with its Set On stamp) so the tab remains one row per user.
    """
    ws = _get_goals_sheet()
    cell = ws.find(str(user_id), in_column=1)
    if cell is None:
        return False
    col = GOAL_HEADERS.index("Goal Weight") + 1
    had = bool(str(ws.cell(cell.row, col).value or "").strip())
    ws.update_cell(cell.row, col, "")
    ws.update_cell(
        cell.row, GOAL_HEADERS.index("Set On") + 1,
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    return had

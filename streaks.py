"""
Consecutive-week check-in streaks. Pure: no Sheets, no Discord, no Flask.

A "week" is a Monday-to-Sunday week in the server's local zone, not UTC: a
check-in at 22:00 on Sunday in Los Angeles is 05:00 or 06:00 UTC on Monday and
would otherwise be counted against the following week, splitting a perfectly
regular Sunday-night habit into a streak of ones. The weekly reminder fires in
the same zone, so the two agree on what "this week" means.
"""

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except Exception as e:  # ZoneInfoNotFoundError on an image without a tz database
    # python:3.12-slim ships no /usr/share/zoneinfo; the pure-Python `tzdata`
    # wheel in requirements.txt provides it. If that is ever missing, degrade
    # to UTC rather than refuse to count streaks at all.
    log.warning("No tz database for America/Los_Angeles (%s); using UTC", e)
    LOCAL_TZ = timezone.utc

# Streak lengths that earn a public celebration post.
MILESTONES = (4, 8, 12, 26, 52)


def week_index(dt: datetime) -> int:
    """A monotonic week number: the ordinal of the local Monday, in weeks.

    Consecutive weeks differ by exactly 1, including across a year boundary,
    which is what makes "consecutive" a plain `b - a == 1` below.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(LOCAL_TZ)
    monday = local.date() - timedelta(days=local.weekday())
    return monday.toordinal() // 7


def compute_streaks(history: list[dict], as_of: datetime | None = None) -> dict:
    """Streak stats for a list of check-ins ({"date": datetime, ...}).

    Returns {"current", "longest", "weeks"}: the run of consecutive weeks ending
    at the most recent check-in, the longest run ever, and the number of
    distinct weeks with a check-in. Several check-ins in one week count once.

    With `as_of`, check-ins after that moment are ignored and `current` is 0
    unless the most recent counted week is `as_of`'s own week — the recap uses
    this so a member who skipped last week reads as "streak reset" rather than
    as still carrying the run they had before.
    """
    weeks = sorted({week_index(h["date"]) for h in history})
    cutoff = week_index(as_of) if as_of is not None else None
    if cutoff is not None:
        weeks = [w for w in weeks if w <= cutoff]
    if not weeks:
        return {"current": 0, "longest": 0, "weeks": 0}

    longest = run = 1
    for a, b in zip(weeks, weeks[1:]):
        run = run + 1 if b - a == 1 else 1
        longest = max(longest, run)
    current = run
    if cutoff is not None and weeks[-1] != cutoff:
        current = 0
    return {"current": current, "longest": longest, "weeks": len(weeks)}

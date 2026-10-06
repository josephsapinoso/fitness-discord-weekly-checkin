"""
The Monday recap: who showed up last week, their streaks, and how the group
moved. Pure: takes rows, returns a summary dict; no Sheets, no Discord.

"Last week" is the Monday–Sunday week (in streaks.LOCAL_TZ) before the week
`today` falls in, so a recap run on Monday morning — or on demand with /recap
any day — always describes the week that just ended.
"""

from datetime import date, datetime, timedelta

import streaks


def last_week_bounds(today: date) -> tuple[date, date]:
    """(Monday, Sunday) of the week before the one containing `today`."""
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def _local_noon(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, tzinfo=streaks.LOCAL_TZ)


def summarize_week(checkins: list[dict], today: date) -> dict:
    """Per-member and group stats for last week.

    `checkins` rows: {"user_id", "username", "date" (aware datetime), "weight",
    "starting" (float | None)}, any order.

    Each member: username (their latest), checked_in, streak (as of the end of
    last week, so a miss reads as 0), change and pct (last week's latest weight
    vs their latest weight before last week; None without both), total (latest
    weight minus their starting weight). Checked-in members sort first.
    Group: combined total, and the biggest percentage loser last week, if any.
    """
    monday, sunday = last_week_bounds(today)
    last_wk = streaks.week_index(_local_noon(monday))
    as_of = _local_noon(sunday) + timedelta(hours=11, minutes=59)

    by_user: dict[str, list[dict]] = {}
    for c in checkins:
        by_user.setdefault(str(c["user_id"]), []).append(c)

    members = []
    for rows in by_user.values():
        rows = sorted(rows, key=lambda r: r["date"])
        in_week = [r for r in rows if streaks.week_index(r["date"]) == last_wk]
        before = [r for r in rows if streaks.week_index(r["date"]) < last_wk]

        change = pct = None
        if in_week and before:
            prev = before[-1]["weight"]
            change = in_week[-1]["weight"] - prev
            pct = change / prev * 100 if prev else None

        starting = rows[0].get("starting") or rows[0]["weight"]
        members.append({
            "username": rows[-1]["username"],
            "checked_in": bool(in_week),
            "streak": streaks.compute_streaks(rows, as_of=as_of)["current"],
            "change": change,
            "pct": pct,
            "total": rows[-1]["weight"] - starting,
        })

    members.sort(key=lambda m: (not m["checked_in"], m["username"].lower()))
    movers = [m for m in members if m["pct"] is not None and m["pct"] < 0]
    return {
        "week_label": f"{monday:%b %d} – {sunday:%b %d}",
        "members": members,
        "combined": sum(m["total"] for m in members),
        "biggest_mover": min(movers, key=lambda m: m["pct"]) if movers else None,
    }

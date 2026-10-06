"""
Goal-weight maths. Pure: no Sheets, no Discord, no matplotlib.

Kept out of charts.py on purpose — that module imports matplotlib, and the
check-in task needs these few lines without paying for a plotting stack.
"""

from datetime import date, timedelta

# A projection further out than this is noise: a slope near zero puts the goal
# in the next decade, and saying nothing beats saying that.
MAX_PROJECTION_DAYS = 730


def progress(starting: float, current: float, goal: float) -> dict | None:
    """Where `current` sits on the road from `starting` to `goal`.

    None when goal == starting (there is no direction to measure). Otherwise:
      direction: -1 when the goal is below the start (losing), +1 when above
      remaining: lbs still to go; zero or negative once the goal is reached
      pct:       0–100, how far along from start to goal
      reached:   remaining <= 0
    """
    if goal == starting:
        return None
    direction = -1 if goal < starting else 1
    remaining = (current - goal) * -direction
    total = abs(starting - goal)
    pct = max(0.0, min(100.0, (total - remaining) / total * 100))
    return {
        "direction": direction,
        "remaining": remaining,
        "pct": pct,
        "reached": remaining <= 0,
    }


def projected_date(
    remaining: float, pace_per_week: float | None, direction: int, today: date
) -> tuple[date, float] | None:
    """(date, weeks) when the goal lands at the current pace, or None.

    None when there is no pace, the pace runs away from the goal, the goal is
    already reached, or the date is more than MAX_PROJECTION_DAYS out.
    """
    if pace_per_week is None or remaining <= 0:
        return None
    if pace_per_week * direction <= 0:
        return None
    weeks = remaining / abs(pace_per_week)
    if weeks * 7 > MAX_PROJECTION_DAYS:
        return None
    return today + timedelta(days=round(weeks * 7)), weeks


def describe(goal: float, gp: dict | None) -> str:
    """One embed-field line: '170.0 lbs — 12.0 to go (56% there)'."""
    if gp is None:
        return f"{goal:.1f} lbs"
    if gp["reached"]:
        return f"{goal:.1f} lbs — ✅ reached!"
    return f"{goal:.1f} lbs — {gp['remaining']:.1f} to go ({gp['pct']:.0f}% there)"

"""
Chart rendering and stats for the /progress command.

Renders a weight-over-time chart (Discord dark theme) as a PNG in memory,
and computes summary stats: total change, pace of loss/gain, etc.
"""

import io
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")  # headless rendering — must come before pyplot import

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

# Discord dark-theme palette
BG = "#2b2d31"
PANEL = "#313338"
FG = "#e0e1e5"
MUTED = "#80848e"
BLURPLE = "#5865f2"
GREEN = "#57f287"
RED = "#ed4245"

VIEWS = {
    "all": {"label": "All-Time", "days": None},
    "6m": {"label": "Past 6 Months", "days": 183},
    "30d": {"label": "Past 30 Days", "days": 30},
}


def filter_history(history: list[dict], view: str) -> list[dict]:
    """Return only the check-ins inside the selected window."""
    days = VIEWS[view]["days"]
    if days is None:
        return history
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [h for h in history if h["date"] >= cutoff]


def compute_stats(history: list[dict], period: list[dict]) -> dict:
    """Summary stats from full history + the currently viewed period.

    Pace uses a least-squares trend over the period rather than just
    endpoints, so one bad weigh-in doesn't distort it.
    """
    stats = {
        "starting": history[0]["weight"],
        "current": history[-1]["weight"],
        "total_change": history[-1]["weight"] - history[0]["weight"],
        "checkins": len(history),
        "period_change": None,
        "pace_per_week": None,
    }
    if len(period) >= 2:
        stats["period_change"] = period[-1]["weight"] - period[0]["weight"]
        x = np.array([mdates.date2num(h["date"]) for h in period])  # days
        y = np.array([h["weight"] for h in period])
        if x.max() > x.min():
            slope = np.polyfit(x, y, 1)[0]  # lbs per day
            stats["pace_per_week"] = slope * 7
    return stats


def render_progress_chart(
    period: list[dict], view: str, display_name: str
) -> io.BytesIO:
    """Render the weight chart for one view window; returns PNG bytes."""
    dates = [h["date"] for h in period]
    weights = [h["weight"] for h in period]
    losing = weights[-1] <= weights[0]
    accent = GREEN if losing else RED

    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=160)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)

    # Weight line + soft fill underneath
    ax.plot(dates, weights, color=BLURPLE, linewidth=2.2, marker="o",
            markersize=4.5, markerfacecolor=FG, markeredgecolor=BLURPLE, zorder=3)
    ax.fill_between(dates, weights, min(weights) - 2, color=BLURPLE, alpha=0.12, zorder=2)

    # Trend line (least squares)
    if len(period) >= 3:
        x = np.array([mdates.date2num(d) for d in dates])
        slope, intercept = np.polyfit(x, weights, 1)
        ax.plot(dates, slope * x + intercept, color=accent, linewidth=1.5,
                linestyle="--", alpha=0.85, zorder=2, label="Trend")
        ax.legend(loc="upper right", frameon=False, labelcolor=MUTED, fontsize=8)

    # Annotate first and latest points
    ax.annotate(f"{weights[0]:.1f}", (dates[0], weights[0]),
                textcoords="offset points", xytext=(0, 10),
                ha="center", color=MUTED, fontsize=8)
    ax.annotate(f"{weights[-1]:.1f}", (dates[-1], weights[-1]),
                textcoords="offset points", xytext=(0, 10),
                ha="center", color=FG, fontsize=9, fontweight="bold")

    # Styling
    ax.set_title(f"{display_name} — {VIEWS[view]['label']}",
                 color=FG, fontsize=12, fontweight="bold", pad=12)
    ax.tick_params(colors=MUTED, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(MUTED)
        spine.set_alpha(0.3)
    ax.grid(True, color=MUTED, alpha=0.15, linewidth=0.6)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
    pad = max((max(weights) - min(weights)) * 0.15, 1.5)
    ax.set_ylim(min(weights) - pad, max(weights) + pad)
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf

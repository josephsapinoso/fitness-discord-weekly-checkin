"""
Fitness Discord Bot — weekly check-in facilitator (Cloud Run edition).

Instead of a persistent gateway connection (discord.py), this is a stateless
HTTP "interactions endpoint" server, which lets the bot run for $0/month on
Cloud Run's free tier:

  POST /interactions  — all Discord slash commands / modals / buttons land here
                        (signature-verified, must be acked within 3 seconds)
  POST /process       — Cloud Tasks calls back here to do the slow work
                        (Sheets reads/writes, chart rendering) after the ack
  POST /reminder      — Cloud Scheduler hits this every Monday to post the
                        weekly check-in prompt
  GET  /              — health check

Commands (unchanged from the gateway version):
  /checkin   — opens a modal to submit this week's check-in
  /summary   — posts the latest check-ins for the group
  /progress  — your personal weight chart (all-time / 6 months / 30 days)
  /history   — link to the full Google Sheet
"""

import concurrent.futures
import json
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from werkzeug.middleware.proxy_fix import ProxyFix

import discord_api

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
# Cloud Run sits behind a proxy; trust X-Forwarded-* so request.host_url is right
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ── Discord interaction constants ──────────────────────────────────────────────
# Incoming interaction types
PING, APPLICATION_COMMAND, MESSAGE_COMPONENT, MODAL_SUBMIT = 1, 2, 3, 5
# Response types
PONG = 1
CHANNEL_MESSAGE = 4
DEFERRED_CHANNEL_MESSAGE = 5
DEFERRED_UPDATE_MESSAGE = 6
MODAL = 9
# Message flags
EPHEMERAL = 64

# Small pool for the modal-prefill Sheets read (which has a hard time budget —
# Discord gives us only 3 seconds to open a modal, and modals can't be deferred)
_prefill_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
PREFILL_TIMEOUT_S = float(os.environ.get("PREFILL_TIMEOUT_S", "1.4"))


# ── Helpers ────────────────────────────────────────────────────────────────────
def _self_url() -> str:
    """Base URL of this service (for Cloud Tasks callbacks)."""
    return os.environ.get("SELF_URL") or request.host_url


def _interaction_user(interaction: dict) -> tuple[dict, dict | None]:
    """Return (user, member) from an interaction (guild or DM)."""
    member = interaction.get("member")
    user = member["user"] if member else interaction["user"]
    return user, member


def _verify_signature() -> None:
    """Verify the Ed25519 signature Discord attaches to every request."""
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    body = request.get_data(as_text=True)
    try:
        verify_key = VerifyKey(bytes.fromhex(os.environ["DISCORD_PUBLIC_KEY"]))
        verify_key.verify(f"{timestamp}{body}".encode(), bytes.fromhex(signature))
    except (BadSignatureError, ValueError, KeyError):
        abort(401, "invalid request signature")


def _check_secret(header_name: str) -> None:
    if request.headers.get(header_name, "") != os.environ["TASK_SECRET"]:
        abort(403)


def _num(value: str) -> float:
    return float("".join(c for c in value if c.isdigit() or c == "."))


def _change_suffix(current: str, last_week: str) -> str:
    """'  📉 -1.2' style suffix comparing two weight strings (best-effort)."""
    try:
        diff = _num(current) - _num(last_week)
    except (ValueError, TypeError):
        return ""
    arrow = "📉" if diff < 0 else ("📈" if diff > 0 else "➡️")
    return f"  {arrow} {diff:+.1f}"


# ── Embed builders (ported unchanged in spirit from the gateway bot) ───────────
def _build_checkin_embed(
    user: dict,
    member: dict | None,
    current: str,
    last_week: str,
    starting: str,
    proud_of: str,
    can_work_on: str,
) -> dict:
    week_str = datetime.now(timezone.utc).strftime("Week of %B %d, %Y")
    return {
        "title": f"Weekly Check-in — {discord_api.display_name(user, member)}",
        "description": week_str,
        "color": discord_api.COLOR_GREEN,
        "thumbnail": {"url": discord_api.avatar_url(user)},
        "fields": [
            {"name": "⚖️ Current Weight", "value": f"{current}{_change_suffix(current, last_week)}", "inline": True},
            {"name": "📅 Last Week", "value": last_week, "inline": True},
            {"name": "🚀 Starting Weight", "value": starting, "inline": True},
            {"name": "🌟 Proud of", "value": proud_of, "inline": False},
            {"name": "🎯 Can Work On", "value": can_work_on, "inline": False},
        ],
        "footer": {"text": "Keep it up! 💪"},
    }


def _build_summary_embed(records: list[dict]) -> dict:
    import sheets

    fields = []
    for r in records[-5:]:  # show last 5 in the embed
        # gspread returns numeric cells as int/float — coerce everything to str
        username = str(r.get("Username") or "Unknown")
        cur = str(r.get("Current Weight") or "—")
        lw = str(r.get("Last Week Weight") or "—")
        proud = str(r.get("Proud Of") or "—")
        work = str(r.get("Can Work On") or "—")
        fields.append(
            {
                "name": username[:256],
                "value": (
                    f"**Weight:** {cur}{_change_suffix(cur, lw)}\n"
                    f"**🌟** {proud[:80]}\n"
                    f"**🎯** {work[:80]}"
                )[:1024],
                "inline": False,
            }
        )
    fields.append(
        {
            "name": "📄 Full history",
            "value": f"[Open Google Sheet]({sheets.get_sheet_url()})",
            "inline": False,
        }
    )
    return {
        "title": "📊 Latest Check-ins",
        "color": discord_api.COLOR_BLUE,
        "description": f"{len(records)} most recent entries",
        "fields": fields,
    }


def _reminder_embed() -> dict:
    return {
        "title": "🏋️ Weekly Check-in Time!",
        "description": (
            "It's time for your weekly fitness check-in! "
            "Use `/checkin` to log your progress.\n\n"
            "**This week, share:**\n"
            "⚖️ Current weight\n"
            "📅 Last week's weight\n"
            "🚀 Starting weight\n"
            "🌟 Something you're proud of\n"
            "🎯 Something to work on"
        ),
        "color": discord_api.COLOR_GOLD,
        "footer": {"text": "Consistency is key 💪"},
    }


# ── /progress payload (embed + optional chart + view buttons) ──────────────────
def _progress_buttons(current_view: str, owner_id: str) -> list[dict]:
    labels = {"all": "All-Time", "6m": "6 Months", "30d": "30 Days"}
    buttons = [
        {
            "type": 2,  # button
            "style": 1 if key == current_view else 2,  # primary / secondary
            "label": label,
            "custom_id": f"progress:{key}:{owner_id}",
        }
        for key, label in labels.items()
    ]
    return [{"type": 1, "components": buttons}]  # one action row


def _build_progress_payload(history: list[dict], view: str, user: dict, member: dict | None):
    """Returns (embed_dict, chart_BytesIO_or_None)."""
    import charts

    period = charts.filter_history(history, view)
    stats = charts.compute_stats(history, period)
    view_label = charts.VIEWS[view]["label"]
    name = discord_api.display_name(user, member)

    losing = stats["total_change"] <= 0
    embed: dict = {
        "title": f"📈 Progress — {name}",
        "color": discord_api.COLOR_GREEN if losing else discord_api.COLOR_ORANGE,
        "fields": [],
        "footer": {"text": "Pace is a trend over the selected window, not week-to-week."},
    }

    # Overall (all-time) stats — always shown so context never disappears
    total = stats["total_change"]
    total_arrow = "📉" if total < 0 else ("📈" if total > 0 else "➡️")
    embed["fields"] += [
        {"name": "🚀 Starting", "value": f"{stats['starting']:.1f} lbs", "inline": True},
        {"name": "⚖️ Current", "value": f"{stats['current']:.1f} lbs", "inline": True},
        {"name": "Overall", "value": f"{total_arrow} {total:+.1f} lbs", "inline": True},
    ]

    if len(period) >= 2:
        pc = stats["period_change"]
        pc_arrow = "📉" if pc < 0 else ("📈" if pc > 0 else "➡️")
        embed["fields"].append(
            {"name": f"{view_label} Change", "value": f"{pc_arrow} {pc:+.1f} lbs", "inline": True}
        )
        pace = stats["pace_per_week"]
        if pace is not None:
            direction = "losing" if pace < 0 else ("gaining" if pace > 0 else "holding at")
            embed["fields"].append(
                {"name": "Pace", "value": f"{direction} {abs(pace):.2f} lbs/week", "inline": True}
            )
        embed["fields"].append(
            {"name": "Check-ins", "value": str(stats["checkins"]), "inline": True}
        )
        chart_buf = charts.render_progress_chart(period, view, name)
        embed["image"] = {"url": "attachment://progress.png"}
        return embed, chart_buf

    embed["description"] = (
        f"Not enough check-ins in the **{view_label}** window to chart — "
        "showing overall stats only."
    )
    return embed, None


# ── The check-in modal ─────────────────────────────────────────────────────────
def _checkin_modal(starting: str | None, last_week: str | None) -> dict:
    def text_input(custom_id, label, placeholder, *, paragraph=False, value=None, max_length=50):
        item = {
            "type": 4,  # text input
            "custom_id": custom_id,
            "label": label,
            "style": 2 if paragraph else 1,
            "placeholder": placeholder,
            "required": True,
            "max_length": max_length,
        }
        if value:
            item["value"] = value
        return {"type": 1, "components": [item]}

    return {
        "type": MODAL,
        "data": {
            "custom_id": "checkin_modal",
            "title": "Weekly Fitness Check-in 💪",
            "components": [
                text_input("current_weight", "Current Weight", "e.g. 185 lbs"),
                text_input(
                    "last_week_weight", "Last Week's Weight",
                    "Auto-filled from your last check-in", value=last_week,
                ),
                text_input(
                    "starting_weight", "Starting Weight",
                    "Auto-filled from your first check-in", value=starting,
                ),
                text_input(
                    "proud_of", "Proud of 🌟",
                    "Something you accomplished this week", paragraph=True, max_length=500,
                ),
                text_input(
                    "can_work_on", "Can Work On 🎯",
                    "Something to improve next week", paragraph=True, max_length=500,
                ),
            ],
        },
    }


def _modal_values(interaction: dict) -> dict:
    """Flatten modal-submit components into {custom_id: value}."""
    out = {}
    for row in interaction["data"]["components"]:
        for comp in row["components"]:
            out[comp["custom_id"]] = comp.get("value", "")
    return out


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return "ok", 200


@app.post("/interactions")
def interactions():
    _verify_signature()
    interaction = request.get_json(force=True)
    itype = interaction["type"]

    if itype == PING:
        return jsonify({"type": PONG})

    if itype == APPLICATION_COMMAND:
        return _handle_command(interaction)

    if itype == MODAL_SUBMIT:
        return _handle_modal_submit(interaction)

    if itype == MESSAGE_COMPONENT:
        return _handle_component(interaction)

    return jsonify(
        {"type": CHANNEL_MESSAGE, "data": {"content": "Unsupported interaction.", "flags": EPHEMERAL}}
    )


def _handle_command(interaction: dict):
    import tasks_queue

    name = interaction["data"]["name"]
    user, member = _interaction_user(interaction)

    if name == "checkin":
        # Modals can't be deferred, so the prefill Sheets read gets a hard
        # time budget; on a slow/cold read the modal opens without prefill.
        starting = last_week = None
        try:
            import sheets

            future = _prefill_pool.submit(sheets.get_user_prefill, user["id"])
            starting, last_week = future.result(timeout=PREFILL_TIMEOUT_S)
        except Exception as e:
            log.warning("Prefill skipped: %s", e)
        return jsonify(_checkin_modal(starting, last_week))

    if name == "summary":
        tasks_queue.enqueue(
            {"kind": "summary", "token": interaction["token"]}, _self_url()
        )
        return jsonify({"type": DEFERRED_CHANNEL_MESSAGE})

    if name == "progress":
        options = {o["name"]: o.get("value") for o in interaction["data"].get("options", [])}
        share = bool(options.get("share", False))
        tasks_queue.enqueue(
            {
                "kind": "progress",
                "view": "all",
                "token": interaction["token"],
                "user": user,
                "member_nick": (member or {}).get("nick"),
            },
            _self_url(),
        )
        data = {} if share else {"flags": EPHEMERAL}
        return jsonify({"type": DEFERRED_CHANNEL_MESSAGE, "data": data})

    if name == "history":
        import sheets

        return jsonify(
            {
                "type": CHANNEL_MESSAGE,
                "data": {
                    "content": f"📄 [Full check-in history in Google Sheets]({sheets.get_sheet_url()})",
                    "flags": EPHEMERAL,
                },
            }
        )

    return jsonify(
        {"type": CHANNEL_MESSAGE, "data": {"content": f"Unknown command `{name}`.", "flags": EPHEMERAL}}
    )


def _handle_modal_submit(interaction: dict):
    import tasks_queue

    if interaction["data"]["custom_id"] != "checkin_modal":
        return jsonify(
            {"type": CHANNEL_MESSAGE, "data": {"content": "Unknown form.", "flags": EPHEMERAL}}
        )

    user, member = _interaction_user(interaction)
    values = _modal_values(interaction)
    tasks_queue.enqueue(
        {
            "kind": "checkin_submit",
            "token": interaction["token"],
            "user": user,
            "member_nick": (member or {}).get("nick"),
            "username": f"{user.get('username', 'unknown')}"
            + (f"#{user['discriminator']}" if user.get("discriminator") not in (None, "0") else ""),
            "values": values,
        },
        _self_url(),
    )
    return jsonify({"type": DEFERRED_CHANNEL_MESSAGE, "data": {"flags": EPHEMERAL}})


def _handle_component(interaction: dict):
    import tasks_queue

    custom_id = interaction["data"].get("custom_id", "")
    if not custom_id.startswith("progress:"):
        return jsonify({"type": DEFERRED_UPDATE_MESSAGE})

    _, view, owner_id = custom_id.split(":", 2)
    user, member = _interaction_user(interaction)
    if user["id"] != owner_id:
        return jsonify(
            {
                "type": CHANNEL_MESSAGE,
                "data": {
                    "content": "This is someone else's progress view — run `/progress` for your own!",
                    "flags": EPHEMERAL,
                },
            }
        )

    tasks_queue.enqueue(
        {
            "kind": "progress",
            "view": view,
            "token": interaction["token"],
            "user": user,
            "member_nick": (member or {}).get("nick"),
        },
        _self_url(),
    )
    return jsonify({"type": DEFERRED_UPDATE_MESSAGE})


# ── Deferred work (called back by Cloud Tasks) ─────────────────────────────────
@app.post("/process")
def process_task():
    _check_secret("X-Task-Secret")
    payload = request.get_json(force=True)
    kind = payload.get("kind")
    token = payload.get("token", "")

    try:
        if kind == "checkin_submit":
            _task_checkin_submit(payload)
        elif kind == "summary":
            _task_summary(payload)
        elif kind == "progress":
            _task_progress(payload)
        else:
            log.error("Unknown task kind: %s", kind)
    except Exception as e:
        # Return 200 so Cloud Tasks does NOT retry — retries could double-write
        # check-ins to the sheet. Surface the error to the user instead.
        log.exception("Task %s failed: %s", kind, e)
        try:
            discord_api.edit_original_response(
                token, {"content": "⚠️ Something went wrong. Please try again."}
            )
        except Exception:
            pass
    return "ok", 200


def _task_checkin_submit(payload: dict) -> None:
    import sheets

    user = payload["user"]
    member = {"nick": payload.get("member_nick")} if payload.get("member_nick") else None
    v = payload["values"]

    sheets.log_checkin(
        user_id=user["id"],
        username=payload["username"],
        current_weight=v["current_weight"],
        last_week_weight=v["last_week_weight"],
        starting_weight=v["starting_weight"],
        proud_of=v["proud_of"],
        can_work_on=v["can_work_on"],
    )

    embed = _build_checkin_embed(
        user, member,
        current=v["current_weight"],
        last_week=v["last_week_weight"],
        starting=v["starting_weight"],
        proud_of=v["proud_of"],
        can_work_on=v["can_work_on"],
    )
    discord_api.post_channel_message(os.environ["CHECKIN_CHANNEL_ID"], {"embeds": [embed]})
    discord_api.edit_original_response(payload["token"], {"content": "✅ Check-in submitted!"})


def _task_summary(payload: dict) -> None:
    import sheets

    records = sheets.get_latest_checkins(limit=10)
    if not records:
        discord_api.edit_original_response(
            payload["token"],
            {"content": "No check-ins logged yet. Be the first with `/checkin`!"},
        )
        return
    discord_api.edit_original_response(payload["token"], {"embeds": [_build_summary_embed(records)]})


def _task_progress(payload: dict) -> None:
    import sheets

    user = payload["user"]
    member = {"nick": payload.get("member_nick")} if payload.get("member_nick") else None
    view = payload.get("view", "all")

    history = sheets.get_user_history(user["id"])
    if len(history) < 2:
        discord_api.edit_original_response(
            payload["token"],
            {
                "content": (
                    "You need at least **2 check-ins** to chart progress. "
                    "Log one with `/checkin` and check back next week! 💪"
                ),
                "embeds": [],
                "components": [],
                "attachments": [],
            },
        )
        return

    embed, chart_buf = _build_progress_payload(history, view, user, member)
    body = {
        "embeds": [embed],
        "components": _progress_buttons(view, user["id"]),
        "content": "",
    }
    if chart_buf is None:
        body["attachments"] = []  # clear any previous chart
    discord_api.edit_original_response(payload["token"], body, file_buf=chart_buf)


# ── Weekly reminder (called by Cloud Scheduler) ────────────────────────────────
@app.post("/reminder")
def reminder():
    _check_secret("X-Reminder-Secret")
    discord_api.post_channel_message(
        os.environ["CHECKIN_CHANNEL_ID"], {"embeds": [_reminder_embed()]}
    )
    log.info("Weekly reminder posted.")
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)

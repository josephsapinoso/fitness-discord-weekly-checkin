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

Commands:
  /checkin        — opens a modal to submit this week's check-in (+ optional photo)
  /summary        — posts the latest check-ins for the group
  /progress       — your personal weight chart (all-time / 6 months / 30 days)
  /history        — link to the full Google Sheet
  /day1           — set or replace your before/after baseline photo
  /collage        — a grid of your archived progress photos
  /photo-replace  — swap the photo stored for a given date
  /howto          — a pinnable explainer for the weekly check-in
"""

import concurrent.futures
import io
import json
import logging
import os
import time
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
PING, APPLICATION_COMMAND, MESSAGE_COMPONENT, AUTOCOMPLETE, MODAL_SUBMIT = 1, 2, 3, 4, 5
# Response types
PONG = 1
CHANNEL_MESSAGE = 4
DEFERRED_CHANNEL_MESSAGE = 5
DEFERRED_UPDATE_MESSAGE = 6
AUTOCOMPLETE_RESULT = 8
MODAL = 9
# Message flags
EPHEMERAL = 64

# Small pool for the modal-prefill Sheets read (which has a hard time budget —
# Discord gives us only 3 seconds to open a modal, and modals can't be deferred)
_prefill_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
PREFILL_TIMEOUT_S = float(os.environ.get("PREFILL_TIMEOUT_S", "1.4"))

# Discord's hard interaction deadline. A modal (and an autocomplete result)
# can't be deferred, so the response has to reach Discord within this window.
INTERACTION_DEADLINE_S = 3.0
# Time reserved to serialize and ship our response back to Discord after the
# Sheets read finishes; keeps the read from spending the whole budget.
RESPONSE_MARGIN_S = float(os.environ.get("RESPONSE_MARGIN_S", "0.6"))
# Below this, an inline Sheets read isn't worth attempting — open the modal /
# return autocomplete immediately rather than risk overrunning the deadline.
MIN_PREFILL_BUDGET_S = float(os.environ.get("MIN_PREFILL_BUDGET_S", "0.25"))


def _interaction_budget() -> float:
    """Seconds we can safely spend on an inline (non-deferrable) Sheets read.

    ``PREFILL_TIMEOUT_S`` alone isn't enough: it bounds only the work done once
    this handler is running, but on a cold Cloud Run start (this bot scales to
    zero) the container boot can eat most of Discord's 3-second budget *before*
    the handler is even entered. A 1.4s read stacked on a ~1.9s cold start
    overran the deadline and surfaced to users as "The application did not
    respond".

    Discord's ``X-Signature-Timestamp`` marks when it sent the interaction, so
    ``now - that`` is how much of the 3 seconds has already elapsed (cold start
    included). Bound the read to whatever remains, minus a margin to ship the
    reply, capped at the configured timeout. Returns 0 when no time is left.
    """
    ts = request.headers.get("X-Signature-Timestamp", "")
    try:
        elapsed = time.time() - int(ts)
    except (ValueError, TypeError):
        # No usable timestamp (shouldn't happen post-verification) — fall back
        # to the fixed timeout rather than skip the prefill outright.
        return PREFILL_TIMEOUT_S
    # A negative value means our clock trails Discord's; assume none is gone.
    elapsed = max(0.0, elapsed)
    remaining = INTERACTION_DEADLINE_S - elapsed - RESPONSE_MARGIN_S
    return max(0.0, min(PREFILL_TIMEOUT_S, remaining))


def _prefill_last_week(user_id: str):
    """Worker for the /checkin modal prefill (import kept off the hot path).

    Importing ``sheets`` pulls in gspread + google-auth, which on a cold start
    is itself slow; doing it here means that cost is covered by the caller's
    ``future.result(timeout=...)`` bound instead of running unbounded on the
    critical path before the modal can open.
    """
    import sheets

    return sheets.get_user_prefill(user_id)


def _photo_log_worker(user_id: str):
    """Worker for the /photo-replace autocomplete read (see _prefill_last_week)."""
    import sheets

    return sheets.get_photo_log(user_id)


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


def _build_day1_embed(user: dict, member: dict | None, date_str: str) -> dict:
    return {
        "title": f"📸 Day 1 — {discord_api.display_name(user, member)}",
        "description": f"Starting photo set on **{date_str}**. Every journey begins somewhere! 💪",
        "color": discord_api.COLOR_BLUE,
        "thumbnail": {"url": discord_api.avatar_url(user)},
        "image": {"url": "attachment://day1.png"},
        "footer": {"text": "Your before & after will build from here."},
    }


def _pretty_date(iso_date: str) -> str:
    """'2026-07-24' → 'Jul 24, 2026'; passes anything unparseable straight through."""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return iso_date


def _build_replacement_embed(user: dict, member: dict | None, date_str: str) -> dict:
    return {
        "title": f"🔄 Updated photo — {discord_api.display_name(user, member)}",
        "description": f"Progress photo for **{date_str}** was replaced.",
        "color": discord_api.COLOR_BLUE,
        "thumbnail": {"url": discord_api.avatar_url(user)},
        "image": {"url": "attachment://progress.png"},
    }


def _build_howto_embed() -> dict:
    """The pinnable how-to. Kept in sync with the actual /checkin modal."""
    return {
        "title": "📌 How the weekly check-in works",
        "color": discord_api.COLOR_GOLD,
        "description": "Run `/checkin` any time during the week — it opens a private form.",
        "fields": [
            {
                "name": "1️⃣ Fill in the form",
                "value": (
                    "⚖️ **Current weight**\n"
                    "📅 **Last week's weight** — filled in for you from your last check-in\n"
                    "🌟 **Proud of** — a win from this week\n"
                    "🎯 **Can work on** — something for next week\n\n"
                    "*Your starting weight is remembered automatically — no need to type it.*"
                ),
            },
            {
                "name": "2️⃣ Add a photo (optional)",
                "value": (
                    "The form has a **Progress photo** slot at the bottom. Skipping it is "
                    "completely fine — the check-in posts either way."
                ),
            },
            {
                "name": "3️⃣ One-time opt-in",
                "value": (
                    "The first time you attach a photo, only **you** see it. The bot asks "
                    "once whether to share photos publicly. Nothing is posted until you say yes."
                ),
            },
            {
                "name": "4️⃣ Day 1 and before/after",
                "value": (
                    "Your first shared photo becomes **Day 1**. Every photo after that posts "
                    "as a **before & after** next to it. Use `/day1` to reset your baseline."
                ),
            },
            {
                "name": "🛠️ Other commands",
                "value": (
                    "`/progress` — your weight chart\n"
                    "`/collage` — a grid of your progress photos\n"
                    "`/photo-replace` — swap the photo for a specific date\n"
                    "`/summary` — everyone's latest check-ins\n"
                    "`/history` — link to the full spreadsheet"
                ),
            },
        ],
        "footer": {"text": "Consistency beats perfection 💪"},
    }


def _build_before_after_embed(user: dict, member: dict | None) -> dict:
    week_str = datetime.now(timezone.utc).strftime("Week of %B %d, %Y")
    return {
        "title": f"🔥 Before & After — {discord_api.display_name(user, member)}",
        "description": week_str,
        "color": discord_api.COLOR_GREEN,
        "thumbnail": {"url": discord_api.avatar_url(user)},
        "image": {"url": "attachment://beforeafter.png"},
        "footer": {"text": "Progress, not perfection. 💪"},
    }


def _consent_prompt(user_id: str) -> dict:
    """Ephemeral message + button asking the user to opt into public photos."""
    return {
        "content": (
            "📸 You attached a progress photo! Progress photos are shared "
            "**publicly** in the check-in channel so everyone can cheer on your "
            "before & after. Share it? *(one-time choice — your text check-in "
            "was already posted.)*"
        ),
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,  # button
                        "style": 3,  # success / green
                        "label": "Share it 📸",
                        "custom_id": f"photo_consent:{user_id}",
                    }
                ],
            }
        ],
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
            # Starting weight is no longer asked for — it's derived from your
            # first check-in — so listing it here would send people looking for
            # a field the form doesn't have.
            "**This week, share:**\n"
            "⚖️ Current weight\n"
            "📅 Last week's weight\n"
            "🌟 Something you're proud of\n"
            "🎯 Something to work on\n"
            "📸 A progress photo (optional)"
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
def _checkin_modal(last_week: str | None) -> dict:
    # Discord caps a modal at 5 components ("Between 1 and 5 (inclusive)"), and
    # the file upload below spends one of them. Starting Weight is therefore NOT
    # asked for — _task_checkin_submit reads it back out of the sheet, which is
    # where it came from anyway (it was only ever prefilled for confirmation).
    def text_input(custom_id, label, placeholder, *, paragraph=False, value=None, max_length=50):
        item = {
            "type": 4,  # text input
            "custom_id": custom_id,
            "style": 2 if paragraph else 1,
            "placeholder": placeholder,
            "required": True,
            "max_length": max_length,
        }
        if value:
            item["value"] = value
        # Label (type 18) rather than an Action Row: action rows wrapping text
        # inputs are deprecated in modals, and the file upload has to be
        # Label-wrapped, so the whole modal uses one consistent format.
        return {"type": 18, "label": label, "component": item}

    # A Label-wrapped File Upload (component type 19, inside a Label type 18)
    # collects an optional progress photo right inside the modal — no separate
    # command, no cross-interaction stash. GA on Discord since Nov 2025.
    photo_upload = {
        "type": 18,  # label
        "label": "Progress photo (optional)",
        "description": "Shared publicly for your before/after — one-time opt-in.",
        "component": {
            "type": 19,  # file upload
            "custom_id": "progress_pic",
            "max_values": 1,
            "required": False,
        },
    }

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
                    "proud_of", "Proud of 🌟",
                    "Something you accomplished this week", paragraph=True, max_length=500,
                ),
                text_input(
                    "can_work_on", "Can Work On 🎯",
                    "Something to improve next week", paragraph=True, max_length=500,
                ),
                photo_upload,
            ],
        },
    }


def _iter_modal_components(interaction: dict):
    """Yield each leaf component of a modal-submit payload.

    Handles both action rows (type 1, plural ``components`` list) and the newer
    Label wrappers (type 18, singular ``component``) used for File Upload.
    """
    for row in interaction["data"]["components"]:
        if "components" in row:
            yield from row["components"]
        elif "component" in row:
            yield row["component"]
        else:
            yield row


def _modal_values(interaction: dict) -> dict:
    """Flatten modal-submit TEXT inputs into {custom_id: value}.

    The File Upload component (type 19) is skipped here — its uploaded file is
    resolved separately by _modal_photo_url.
    """
    out = {}
    for comp in _iter_modal_components(interaction):
        cid = comp.get("custom_id")
        if cid and comp.get("type") != 19:
            out[cid] = comp.get("value", "")
    return out


def _modal_photo_url(interaction: dict) -> str | None:
    """Return the signed CDN URL of the modal's uploaded photo, or None.

    A File Upload component submits attachment id(s) under ``values``; the full
    attachment objects live in ``data.resolved.attachments`` keyed by that id.
    """
    resolved = interaction["data"].get("resolved", {}).get("attachments", {})
    for comp in _iter_modal_components(interaction):
        if comp.get("type") == 19:
            ids = comp.get("values") or []
            if ids:
                att = resolved.get(str(ids[0]))
                return att.get("url") if att else None
    return None


def _command_attachment_url(interaction: dict, option_name: str) -> str | None:
    """Resolve an attachment-type (11) slash-command option to its CDN URL."""
    opt = next(
        (o for o in interaction["data"].get("options", []) if o["name"] == option_name),
        None,
    )
    if not opt:
        return None
    resolved = interaction["data"].get("resolved", {}).get("attachments", {})
    att = resolved.get(str(opt.get("value")))
    return att.get("url") if att else None


def _username(user: dict) -> str:
    """Display username, appending a legacy discriminator when present."""
    disc = user.get("discriminator")
    suffix = f"#{disc}" if disc not in (None, "0") else ""
    return f"{user.get('username', 'unknown')}{suffix}"


# ── Routes ─────────────────────────────────────────────────────────────────────
_warmed = False


@app.get("/")
def health():
    """Health check, and the hook the keep-warm ping uses to prime the process.

    Keeping an instance alive is not enough on its own: the Cloud Tasks client
    and the Sheets stack are imported per process, and paying for them inside a
    real interaction is what blows Discord's 3-second deadline. The scheduled
    ping is the request that should absorb that cost, so it happens here — and
    never at the expense of the health check itself.
    """
    global _warmed
    if not _warmed:
        _warmed = True
        try:
            import sheets  # noqa: F401  (gspread + google-auth import)
            import tasks_queue

            tasks_queue.warmup()
        except Exception as e:
            log.warning("Warm-up skipped: %s", e)
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

    if itype == AUTOCOMPLETE:
        return _handle_autocomplete(interaction)

    return jsonify(
        {"type": CHANNEL_MESSAGE, "data": {"content": "Unsupported interaction.", "flags": EPHEMERAL}}
    )


def _handle_command(interaction: dict):
    import tasks_queue

    name = interaction["data"]["name"]
    user, member = _interaction_user(interaction)

    if name == "checkin":
        # Modals can't be deferred, so the prefill Sheets read gets a hard time
        # budget sized to whatever's left of Discord's 3s window — on a cold
        # start (most of the budget already gone) the modal opens immediately
        # without prefill rather than overrunning the deadline.
        last_week = None
        budget = _interaction_budget()
        if budget >= MIN_PREFILL_BUDGET_S:
            try:
                future = _prefill_pool.submit(_prefill_last_week, user["id"])
                _, last_week = future.result(timeout=budget)
            except Exception as e:
                log.warning("Prefill skipped: %s", e)
        else:
            log.warning(
                "Prefill skipped: only %.2fs of interaction budget left", budget
            )
        return jsonify(_checkin_modal(last_week))

    if name == "summary":
        tasks_queue.enqueue(
            {"kind": "summary", "token": interaction["token"], "user": user}, _self_url()
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

    if name == "collage":
        options = {o["name"]: o.get("value") for o in interaction["data"].get("options", [])}
        share = bool(options.get("share", False))
        tasks_queue.enqueue(
            {
                "kind": "collage",
                "token": interaction["token"],
                "user": user,
                "member_nick": (member or {}).get("nick"),
            },
            _self_url(),
        )
        data = {} if share else {"flags": EPHEMERAL}
        return jsonify({"type": DEFERRED_CHANNEL_MESSAGE, "data": data})

    if name == "howto":
        options = {o["name"]: o.get("value") for o in interaction["data"].get("options", [])}
        share = bool(options.get("share", False))
        data: dict = {"embeds": [_build_howto_embed()]}
        if not share:
            data["flags"] = EPHEMERAL
        return jsonify({"type": CHANNEL_MESSAGE, "data": data})

    if name == "photo-replace":
        options = {o["name"]: o.get("value") for o in interaction["data"].get("options", [])}
        tasks_queue.enqueue(
            {
                "kind": "photo_replace",
                "token": interaction["token"],
                "user": user,
                "member_nick": (member or {}).get("nick"),
                "username": _username(user),
                "taken_on": str(options.get("date", "")).strip(),
                "photo_url": _command_attachment_url(interaction, "photo"),
            },
            _self_url(),
        )
        return jsonify({"type": DEFERRED_CHANNEL_MESSAGE, "data": {"flags": EPHEMERAL}})

    if name == "day1":
        photo_url = _command_attachment_url(interaction, "photo")
        tasks_queue.enqueue(
            {
                "kind": "set_baseline",
                "token": interaction["token"],
                "user": user,
                "member_nick": (member or {}).get("nick"),
                "username": _username(user),
                "photo_url": photo_url,
            },
            _self_url(),
        )
        return jsonify({"type": DEFERRED_CHANNEL_MESSAGE, "data": {"flags": EPHEMERAL}})

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


def _handle_autocomplete(interaction: dict):
    """Suggest the user's own archived photo dates for /photo-replace.

    Shares the 3-second interaction deadline, so the Sheets read runs on the
    same bounded pool as the modal prefill. A slow sheet yields an empty list —
    the user can still type a date by hand — rather than a blown deadline.
    """
    user, _ = _interaction_user(interaction)
    typed = ""
    for opt in interaction["data"].get("options", []):
        if opt.get("focused"):
            typed = str(opt.get("value", "")).strip().lower()

    choices: list[dict] = []
    budget = _interaction_budget()
    if budget < MIN_PREFILL_BUDGET_S:
        log.warning("Autocomplete skipped: only %.2fs of interaction budget left", budget)
        return jsonify({"type": AUTOCOMPLETE_RESULT, "data": {"choices": choices}})
    try:
        future = _prefill_pool.submit(_photo_log_worker, user["id"])
        photos = future.result(timeout=budget)
        # Newest first: replacing a recent photo is the common case.
        for p in reversed(photos):
            # Match a leading "2026-0..." the obvious way, but also let a bare
            # "02" mean February — a plain substring test would match the "02"
            # inside the year 2026 and suggest everything.
            date = p["taken_on"].lower()
            if typed and not (date.startswith(typed) or typed in date[5:]):
                continue
            label = f"{p['taken_on']}" + (" (Day 1)" if p["kind"] == "day1" else "")
            choices.append({"name": label, "value": p["taken_on"]})
        choices = choices[:25]  # Discord's cap
    except Exception as e:
        log.warning("Autocomplete skipped: %s", e)

    return jsonify({"type": AUTOCOMPLETE_RESULT, "data": {"choices": choices}})


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
            "username": _username(user),
            "values": values,
            "photo_url": _modal_photo_url(interaction),
        },
        _self_url(),
    )
    return jsonify({"type": DEFERRED_CHANNEL_MESSAGE, "data": {"flags": EPHEMERAL}})


def _handle_component(interaction: dict):
    import tasks_queue

    custom_id = interaction["data"].get("custom_id", "")

    if custom_id.startswith("photo_consent:"):
        owner_id = custom_id.split(":", 1)[1]
        user, member = _interaction_user(interaction)
        if user["id"] != owner_id:
            return jsonify({"type": DEFERRED_UPDATE_MESSAGE})
        tasks_queue.enqueue(
            {
                "kind": "grant_consent",
                "token": interaction["token"],
                "user": user,
                "member_nick": (member or {}).get("nick"),
                "username": _username(user),
            },
            _self_url(),
        )
        return jsonify({"type": DEFERRED_UPDATE_MESSAGE})

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
def _photo_result_text(posted: str) -> str:
    """The ephemeral confirmation for whichever photo post actually happened."""
    if posted == "before_after":
        return "✅ Check-in submitted — your **before & after** is up!"
    if posted == "reset":
        return (
            "✅ Check-in submitted! Your old Day 1 photo couldn't be read any more, "
            "so this one is your new **Day 1** — the next photo will show a before & after."
        )
    return "✅ Check-in submitted with your photo!"


def _reply(token: str, user: dict | None, body: dict, file_buf=None, filename="progress.png") -> None:
    """Answer a deferred interaction, falling back when its token is dead.

    A missed 3-second ack permanently invalidates the interaction token, so the
    reply — including a consent prompt the user is waiting on — would otherwise
    vanish with only a stack trace to show for it. DM the user instead, and if
    even that is closed, nudge them publicly.
    """
    try:
        discord_api.edit_original_response(token, body, file_buf, filename)
        return
    except discord_api.DeadInteractionToken as e:
        log.error("Interaction token dead (%s) for user %s — falling back to DM",
                  e, (user or {}).get("id"))
    except Exception:
        log.exception("edit_original_response failed")
        return

    if not user:
        return
    try:
        discord_api.send_dm(user["id"], body, file_buf, filename)
        return
    except Exception as e:
        log.warning("DM fallback failed: %s", e)

    # Last resort. Deliberately says nothing about a photo: the user may not have
    # consented to anyone knowing they attached one.
    try:
        discord_api.post_channel_message(
            os.environ["CHECKIN_CHANNEL_ID"],
            {"content": f"<@{user['id']}> your last command didn't go through "
                        f"(the bot was waking up) — please run it again 🙏"},
        )
    except Exception as e:
        log.error("Channel fallback failed: %s", e)


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
        elif kind == "set_baseline":
            _task_set_baseline(payload)
        elif kind == "grant_consent":
            _task_grant_consent(payload)
        elif kind == "collage":
            _task_collage(payload)
        elif kind == "photo_replace":
            _task_photo_replace(payload)
        else:
            log.error("Unknown task kind: %s", kind)
    except Exception as e:
        # Return 200 so Cloud Tasks does NOT retry — retries could double-write
        # check-ins to the sheet. Surface the error to the user instead.
        log.exception("Task %s failed: %s", kind, e)
        _reply(token, payload.get("user"),
               {"content": "⚠️ Something went wrong. Please try again."})
    return "ok", 200


def _task_checkin_submit(payload: dict) -> None:
    import sheets

    user = payload["user"]
    member = {"nick": payload.get("member_nick")} if payload.get("member_nick") else None
    v = payload["values"]

    # Starting Weight is no longer a modal field (the modal is at Discord's
    # 5-component ceiling), so recover it from the user's first check-in. This
    # read happens BEFORE log_checkin so the row being written now can't be
    # mistaken for the first one. On a first-ever check-in there is nothing to
    # find, and today's weight is by definition the starting weight.
    try:
        starting, _ = sheets.get_user_prefill(user["id"])
    except Exception as e:
        log.warning("Starting-weight lookup failed: %s", e)
        starting = None
    starting = starting or v["current_weight"]

    sheets.log_checkin(
        user_id=user["id"],
        username=payload["username"],
        current_weight=v["current_weight"],
        last_week_weight=v["last_week_weight"],
        starting_weight=starting,
        proud_of=v["proud_of"],
        can_work_on=v["can_work_on"],
    )

    embed = _build_checkin_embed(
        user, member,
        current=v["current_weight"],
        last_week=v["last_week_weight"],
        starting=starting,
        proud_of=v["proud_of"],
        can_work_on=v["can_work_on"],
    )
    # The text check-in always posts and confirms independently of any photo, so
    # a photo/compose failure can never lose the written check-in.
    discord_api.post_channel_message(os.environ["CHECKIN_CHANNEL_ID"], {"embeds": [embed]})

    photo_url = payload.get("photo_url")
    if not photo_url:
        _reply(payload["token"], user, {"content": "✅ Check-in submitted!"})
        return

    if sheets.get_photo_state(user["id"])["consent"]:
        posted = _post_progress_photo(user, member, photo_url)
        _reply(payload["token"], user, {"content": _photo_result_text(posted)})
    else:
        # Hold the photo privately (Sheet) behind a one-time public-sharing opt-in.
        sheets.upsert_photo_state(user["id"], payload["username"], pending_url=photo_url)
        _reply(payload["token"], user, _consent_prompt(user["id"]))


def _archive_channel() -> str | None:
    """The private bot-only channel that retains raw photos, if configured."""
    return (os.environ.get("ARCHIVE_CHANNEL_ID") or "").strip() or None


def _archive_photo(user: dict, username: str, png: bytes, taken_on: str,
                   post_ref: str = "", kind: str = "progress") -> None:
    """Retain a raw photo in the archive channel and log it.

    Best-effort by design: the check-in and its public post have already
    succeeded by the time this runs, and losing the archive copy must never
    turn a successful check-in into a user-visible failure. A missing
    ARCHIVE_CHANNEL_ID simply means the history features are switched off.
    """
    import sheets

    channel = _archive_channel()
    if not channel:
        return
    try:
        msg = discord_api.post_channel_message(
            channel,
            {"content": f"{username} — {taken_on}"},
            file_buf=io.BytesIO(png),
            filename=f"{taken_on}.png",
        )
        sheets.append_photo_log(
            user_id=user["id"], username=username, taken_on=taken_on,
            archive_ref=str(msg["id"]), post_ref=post_ref, kind=kind,
        )
    except Exception as e:
        log.warning("Photo archive skipped: %s", e)


def _msg_date(msg: dict) -> str:
    """'Mon DD, YYYY' from a Discord message's ISO timestamp (best-effort)."""
    try:
        return datetime.fromisoformat(msg.get("timestamp", "")).strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return "Day 1"


def _message_image_url(msg: dict) -> str | None:
    """The image URL of a bot-posted photo message, whichever shape it is in.

    Discord *moves* an uploaded file into the embed that references it via
    ``attachment://``, leaving ``attachments`` empty — so a check-in-channel Day 1
    keeps its photo at ``embeds[0].image.url``. Archive-channel messages are posted
    without an embed and do keep a real attachment. Both are checked, so neither
    call site can be broken by the other's shape.
    """
    for att in msg.get("attachments") or []:
        if att.get("url"):
            return att["url"]
    for embed in msg.get("embeds") or []:
        url = (embed.get("image") or {}).get("url")
        if url:
            return url
    return None


def _day1_png(user_id: str, day1_msg: dict) -> bytes | None:
    """The user's Day 1 photo bytes, or None if it can no longer be recovered.

    Preferred source is the raw PNG in the private archive channel; the embed on
    the public Day 1 post is the fallback for users whose baseline predates the
    Photo Log.
    """
    import sheets

    try:
        for entry in sheets.get_photo_log(user_id):
            if entry["kind"] == "day1":
                png = _fetch_archived_png(entry["archive_ref"])
                if png:
                    return png
                break
    except Exception as e:
        log.warning("Photo Log lookup failed for %s: %s", user_id, e)

    url = _message_image_url(day1_msg)
    if not url:
        log.warning("Day 1 message %s has no recoverable image", day1_msg.get("id"))
        return None
    try:
        return discord_api.download_image(url)
    except Exception as e:
        log.warning("Day 1 image download failed: %s", e)
        return None


def _post_progress_photo(user: dict, member: dict | None, photo_url: str) -> str:
    """Post the user's photo as their Day 1 (first ever) or a Before/After.

    On the first photo, the posted message becomes the durable Day 1 reference.
    Thereafter, the stored Day 1 message is re-fetched (fresh signed URL) and
    composited against the new photo.

    Returns what was posted: ``"day1"``, ``"before_after"``, or ``"reset"`` when
    a stored baseline existed but its image could not be recovered, so this
    photo became the new Day 1.
    """
    import images
    import sheets

    channel = os.environ["CHECKIN_CHANNEL_ID"]
    now_png = images.normalize(discord_api.download_image(photo_url))
    now_date = datetime.now(timezone.utc).strftime("%b %d, %Y")

    taken_on = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day1_ref = sheets.get_photo_state(user["id"])["day1_ref"]

    day1_msg = day1_png = None
    if day1_ref:
        try:
            day1_msg = discord_api.get_message(channel, day1_ref)
        except Exception as e:
            # Deleted by hand, or the channel changed — treat as no baseline.
            log.warning("Day 1 message %s unavailable: %s", day1_ref, e)
        if day1_msg:
            day1_png = _day1_png(user["id"], day1_msg)

    if day1_png is None:
        # No baseline, or the stored one can no longer be read: this photo
        # becomes the new Day 1 rather than failing the whole check-in.
        msg = discord_api.post_channel_message(
            channel,
            {"embeds": [_build_day1_embed(user, member, now_date)]},
            file_buf=io.BytesIO(now_png),
            filename="day1.png",
        )
        sheets.upsert_photo_state(user["id"], _username(user), day1_ref=str(msg["id"]))
        _archive_photo(user, _username(user), now_png, taken_on,
                       post_ref=str(msg["id"]), kind="day1")
        return "reset" if day1_ref else "day1"

    composite = images.compose_before_after(
        day1_png, now_png, f"Day 1 — {_msg_date(day1_msg)}", f"Now — {now_date}"
    )
    msg = discord_api.post_channel_message(
        channel,
        {"embeds": [_build_before_after_embed(user, member)]},
        file_buf=composite,
        filename="beforeafter.png",
    )
    # Archive the raw photo, not the composite — the collage needs individual
    # panels, and the composite already contains a copy of Day 1.
    _archive_photo(user, _username(user), now_png, taken_on,
                   post_ref=str(msg.get("id", "")), kind="progress")
    return "before_after"


def _task_set_baseline(payload: dict) -> None:
    """/day1 — set or replace the user's Day 1 baseline photo."""
    import images
    import sheets

    user = payload["user"]
    member = {"nick": payload.get("member_nick")} if payload.get("member_nick") else None
    photo_url = payload.get("photo_url")
    token = payload["token"]

    if not photo_url:
        _reply(token, user, {"content": "⚠️ No photo received. Try again."})
        return

    if not sheets.get_photo_state(user["id"])["consent"]:
        sheets.upsert_photo_state(user["id"], payload["username"], pending_url=photo_url)
        _reply(token, user, _consent_prompt(user["id"]))
        return

    # Consented: post a fresh Day 1 and overwrite the stored reference.
    channel = os.environ["CHECKIN_CHANNEL_ID"]
    now_png = images.normalize(discord_api.download_image(photo_url))
    now_date = datetime.now(timezone.utc).strftime("%b %d, %Y")
    msg = discord_api.post_channel_message(
        channel,
        {"embeds": [_build_day1_embed(user, member, now_date)]},
        file_buf=io.BytesIO(now_png),
        filename="day1.png",
    )
    sheets.upsert_photo_state(user["id"], payload["username"], day1_ref=str(msg["id"]))
    _archive_photo(
        user, payload["username"], now_png,
        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        post_ref=str(msg["id"]), kind="day1",
    )
    _reply(
        token, user,
        {"content": "📸 Day 1 photo saved! Your next check-in photo will show your before & after."},
    )


def _fetch_archived_png(archive_ref: str) -> bytes | None:
    """Re-fetch an archived photo's bytes via a fresh signed URL."""
    channel = _archive_channel()
    if not channel:
        return None
    try:
        msg = discord_api.get_message(channel, archive_ref)
        url = _message_image_url(msg)
        if not url:
            log.warning("Archived photo %s has no image", archive_ref)
            return None
        return discord_api.download_image(url)
    except Exception as e:
        log.warning("Archived photo %s unavailable: %s", archive_ref, e)
        return None


def _task_collage(payload: dict) -> None:
    """/collage — a grid of the user's archived progress photos."""
    import images
    import sheets

    user = payload["user"]
    member = {"nick": payload.get("member_nick")} if payload.get("member_nick") else None
    token = payload["token"]
    name = discord_api.display_name(user, member)

    if not _archive_channel():
        _reply(
            token, user, {"content": "⚠️ Photo history isn't set up yet (no archive channel configured)."}
        )
        return

    photos = sheets.get_photo_log(user["id"])
    if not photos:
        _reply(
            token, user,
            {"content": "📭 No progress photos yet. Add one with `/checkin` and they'll show up here."},
        )
        return

    chosen = images.sample_timeline(photos)
    panels = []
    for p in chosen:
        png = _fetch_archived_png(p["archive_ref"])
        if png:  # a single unreadable photo shouldn't sink the whole collage
            panels.append((png, p["taken_on"]))

    if not panels:
        _reply(
            token, user, {"content": "⚠️ Couldn't load your photos right now. Try again shortly."}
        )
        return

    collage = images.render_collage(panels)
    embed = {
        "title": f"🖼️ Progress Collage — {name}",
        "description": (
            f"{len(panels)} photo{'s' if len(panels) != 1 else ''} "
            f"from {panels[0][1]} to {panels[-1][1]}."
        ),
        "color": discord_api.COLOR_GREEN,
        "image": {"url": "attachment://collage.png"},
        "footer": {"text": f"Showing {len(panels)} of {len(photos)} photos."},
    }
    _reply(
        token, user, {"embeds": [embed]}, file_buf=collage, filename="collage.png"
    )


def _task_photo_replace(payload: dict) -> None:
    """/photo-replace — swap the photo stored for one date."""
    import images
    import sheets

    user = payload["user"]
    member = {"nick": payload.get("member_nick")} if payload.get("member_nick") else None
    token = payload["token"]
    taken_on = payload.get("taken_on") or ""
    photo_url = payload.get("photo_url")

    if not photo_url:
        _reply(token, user, {"content": "⚠️ No photo received. Try again."})
        return
    if not _archive_channel():
        _reply(
            token, user, {"content": "⚠️ Photo history isn't set up yet (no archive channel configured)."}
        )
        return
    if not sheets.get_photo_state(user["id"])["consent"]:
        sheets.upsert_photo_state(user["id"], payload["username"], pending_url=photo_url)
        _reply(token, user, _consent_prompt(user["id"]))
        return

    old = sheets.deactivate_photo_log_row(user["id"], taken_on)
    if not old:
        _reply(
            token, user,
            {"content": f"⚠️ No photo found for **{taken_on}**. Pick a date from the suggestions."},
        )
        return

    # Remove the superseded copies — the bot's own messages, so no Manage
    # Messages permission is involved. The archive copy goes first: if the
    # public post's deletion fails, the log row is already inactive and a retry
    # won't double-post.
    discord_api.delete_message(_archive_channel(), old["archive_ref"])
    if old["post_ref"]:
        discord_api.delete_message(os.environ["CHECKIN_CHANNEL_ID"], old["post_ref"])

    png = images.normalize(discord_api.download_image(photo_url))
    pretty = _pretty_date(taken_on)
    is_day1 = old["kind"] == "day1"
    embed = (
        _build_day1_embed(user, member, pretty)
        if is_day1
        else _build_replacement_embed(user, member, pretty)
    )
    # Filename must match the embed's attachment:// reference or Discord shows
    # the embed with no image.
    filename = "day1.png" if is_day1 else "progress.png"
    msg = discord_api.post_channel_message(
        os.environ["CHECKIN_CHANNEL_ID"],
        {"embeds": [embed]},
        file_buf=io.BytesIO(png),
        filename=filename,
    )
    if is_day1:
        # Keep the baseline pointer aimed at the new post, or before/after
        # comparisons would re-fetch a message that no longer exists.
        sheets.upsert_photo_state(user["id"], payload["username"], day1_ref=str(msg["id"]))

    _archive_photo(user, payload["username"], png, taken_on,
                   post_ref=str(msg["id"]), kind=old["kind"])
    _reply(
        token, user, {"content": f"✅ Replaced your photo for **{pretty}**."}
    )


def _task_grant_consent(payload: dict) -> None:
    """Photo-sharing opt-in button → record consent and post the pending photo."""
    import sheets

    user = payload["user"]
    member = {"nick": payload.get("member_nick")} if payload.get("member_nick") else None
    token = payload["token"]

    state = sheets.get_photo_state(user["id"])
    sheets.upsert_photo_state(user["id"], payload["username"], consent=True)

    if state["pending_url"]:
        try:
            _post_progress_photo(user, member, state["pending_url"])
        except Exception as e:
            # Discord photo links are short-lived. Clear the dead reference either
            # way, or consent is recorded, the button is gone, and the stale URL
            # sits in the sheet forever with no way to retry.
            log.warning("Pending photo unusable: %s", e)
            sheets.upsert_photo_state(user["id"], payload["username"], pending_url="")
            _reply(
                token, user,
                {
                    "content": (
                        "✅ Sharing is on — but that photo's link had already expired "
                        "(Discord photo links are short-lived). Send it again with "
                        "`/day1` or your next `/checkin` and it'll post."
                    ),
                    "components": [],
                },
            )
            return
        sheets.upsert_photo_state(user["id"], payload["username"], pending_url="")

    _reply(
        token, user,
        {
            "content": "✅ Shared! Your photos will now appear with your check-ins. 💪",
            "components": [],
        },
    )


def _task_summary(payload: dict) -> None:
    import sheets

    user = payload.get("user")
    records = sheets.get_latest_checkins(limit=10)
    if not records:
        _reply(
            payload["token"], user,
            {"content": "No check-ins logged yet. Be the first with `/checkin`!"},
        )
        return
    _reply(payload["token"], user, {"embeds": [_build_summary_embed(records)]})


def _task_progress(payload: dict) -> None:
    import sheets

    user = payload["user"]
    member = {"nick": payload.get("member_nick")} if payload.get("member_nick") else None
    view = payload.get("view", "all")

    history = sheets.get_user_history(user["id"])
    if len(history) < 2:
        _reply(
            payload["token"], user,
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
    _reply(payload["token"], user, body, file_buf=chart_buf)


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

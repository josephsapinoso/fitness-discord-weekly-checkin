"""
Test suite for the Cloud Run interactions server.

Run:  python tests/test_app.py   (from the repo root)

Uses real Flask + real matplotlib + real Ed25519 (via the `cryptography`
backed nacl stub in tests/stubs). Sheets / Cloud Tasks / Discord REST calls
are monkeypatched.
"""

import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

# Check names contain arrows and emoji. Windows consoles default to cp1252 and
# raise UnicodeEncodeError on the first one; CI (Linux) is already UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "stubs"))
sys.path.insert(0, ROOT)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# ── Env before importing app ───────────────────────────────────────────────────
PRIVATE_KEY = Ed25519PrivateKey.generate()
PUBLIC_HEX = PRIVATE_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

os.environ.update(
    {
        "DISCORD_PUBLIC_KEY": PUBLIC_HEX,
        "DISCORD_TOKEN": "test-token",
        "DISCORD_APPLICATION_ID": "111222333",
        "CHECKIN_CHANNEL_ID": "999888777",
        "GOOGLE_SHEET_ID": "SHEET123",
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "TASK_SECRET": "s3cret",
        "PREFILL_TIMEOUT_S": "0.5",
        "SELF_URL": "https://example.run.app",
        # Set, but empty: _archive_channel() reads that as unconfigured, which the
        # archive-off checks below require. It has to be *present* rather than
        # absent because both app.py and register_commands.py call load_dotenv(),
        # which fills in missing keys — so a developer's real .env would otherwise
        # decide whether this suite passes. CI has no .env; locally there is one.
        "ARCHIVE_CHANNEL_ID": "",
        # Same reasoning: empty means "no admin alerts", and it must be present so
        # a developer's real .env can't switch them on under the suite.
        "ADMIN_USER_ID": "",
    }
)

import app as app_module  # noqa: E402
import discord_api  # noqa: E402
import goals  # noqa: E402
import recap  # noqa: E402
import sheets  # noqa: E402
import streaks  # noqa: E402
import tasks_queue  # noqa: E402

# The health route's warm-up opens the real sheet; keep the suite offline.
REAL_SHEETS_WARMUP = sheets.warmup
sheets.warmup = lambda: None

client = app_module.app.test_client()

PASS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS
    if not cond:
        print(f"FAIL  {name}  {extra}")
        sys.exit(1)
    PASS += 1
    print(f"ok    {name}")


# ── Helpers ────────────────────────────────────────────────────────────────────
def signed_post(body: dict, *, bad_sig: bool = False):
    raw = json.dumps(body)
    ts = str(int(time.time()))
    sig = PRIVATE_KEY.sign(f"{ts}{raw}".encode()).hex()
    if bad_sig:
        sig = ("00" * 64) if sig[:2] != "00" else ("11" * 64)
    return client.post(
        "/interactions",
        data=raw,
        content_type="application/json",
        headers={"X-Signature-Ed25519": sig, "X-Signature-Timestamp": ts},
    )


USER = {"id": "42", "username": "joe", "global_name": "Joe", "avatar": None, "discriminator": "0"}


def cmd_interaction(name: str, options: list | None = None) -> dict:
    d: dict = {"name": name}
    if options is not None:
        d["options"] = options
    return {"type": 2, "token": "tok-abc", "data": d, "member": {"user": USER, "nick": None}}


enqueued: list[tuple[dict, str]] = []


def fake_enqueue(payload, self_url):
    enqueued.append((payload, self_url))


tasks_queue.enqueue = fake_enqueue

# ── 1. Health + signature verification ────────────────────────────────────────
check("GET / health", client.get("/").status_code == 200)

resp = signed_post({"type": 1})
check("PING → PONG", resp.status_code == 200 and resp.get_json() == {"type": 1})

resp = signed_post({"type": 1}, bad_sig=True)
check("bad signature → 401", resp.status_code == 401)

resp = client.post("/interactions", data="{}", content_type="application/json")
check("missing signature → 401", resp.status_code == 401)

# ── 2. /checkin modal (with prefill) ──────────────────────────────────────────
sheets.get_user_prefill = lambda uid: ("200 lbs", "190 lbs", "Sleep more")
resp = signed_post(cmd_interaction("checkin"))
modal = resp.get_json()
check("checkin → modal type 9", modal["type"] == 9)
check("modal custom_id", modal["data"]["custom_id"] == "checkin_modal")
rows = modal["data"]["components"]
# Discord rejects any modal with more than 5 components ("Between 1 and 5
# (inclusive) components that make up the modal") — and a rejected modal
# surfaces to the user as "The application did not respond", with the service
# still logging a healthy 200. Guard the ceiling explicitly.
check("modal within Discord's 5-component cap", len(rows) <= 5, f"got {len(rows)}")
check("modal has 4 text inputs + photo upload", len(rows) == 5)
check("all components Label-wrapped", all(r["type"] == 18 for r in rows))
inputs = {
    r["component"]["custom_id"]: r["component"]
    for r in rows
    if r["component"]["type"] == 4
}
check("starting weight not asked for", "starting_weight" not in inputs)
check("prefill last week", inputs["last_week_weight"].get("value") == "190 lbs")
check("current not prefilled", "value" not in inputs["current_weight"])
check("paragraph styles", inputs["proud_of"]["style"] == 2 and inputs["can_work_on"]["style"] == 2)
# Last week's "Can Work On" comes back as the Proud-of placeholder — from the
# same single read as the weight prefill, so nothing is added to the 3s path.
check(
    "proud-of placeholder echoes last week's focus",
    inputs["proud_of"]["placeholder"] == "Last week you wanted to work on: Sleep more",
    inputs["proud_of"]["placeholder"],
)
_fp = app_module._focus_placeholder
check("focus placeholder: none → default", _fp(None) == "Something you accomplished this week")
check("focus placeholder: blank → default", _fp("   ") == "Something you accomplished this week")
check("focus placeholder: newlines collapsed", _fp("sleep\nmore") == "Last week you wanted to work on: sleep more")
check("focus placeholder: 67 chars fits exactly", len(_fp("a" * 67)) == 100 and not _fp("a" * 67).endswith("…"))
check("focus placeholder: 68 chars is cut to 100", len(_fp("a" * 68)) == 100 and _fp("a" * 68).endswith("…"))
check("focus placeholder: long text capped at 100", len(_fp("word " * 80)) == 100)
photo_row = next(r for r in rows if r["component"]["type"] == 19)
check(
    "photo upload component",
    photo_row["component"]["type"] == 19
    and photo_row["component"]["custom_id"] == "progress_pic"
    and photo_row["component"]["required"] is False,
)

# Slow prefill: modal must still open (without values) inside the time budget
def slow_prefill(uid):
    time.sleep(3)
    return ("x", "y", "z")

sheets.get_user_prefill = slow_prefill
t0 = time.time()
resp = signed_post(cmd_interaction("checkin"))
elapsed = time.time() - t0
modal = resp.get_json()
rows = {
    r["component"]["custom_id"]: r["component"]
    for r in modal["data"]["components"]
    if r["component"]["type"] == 4
}
check("slow prefill → modal within budget", modal["type"] == 9 and elapsed < 2.0, f"{elapsed:.2f}s")
check("slow prefill → no values", "value" not in rows["last_week_weight"])
check("slow prefill → default proud-of placeholder",
      rows["proud_of"]["placeholder"] == "Something you accomplished this week")

# Restore a fast stub: the checkin_submit task now calls get_user_prefill to
# recover Starting Weight, so leaving slow_prefill bound would add 3s per task.
sheets.get_user_prefill = lambda uid: ("200 lbs", "190 lbs", "Sleep more")


# Cold-start guard: a modal can't be deferred, so it must reach Discord inside
# the 3s interaction window. On a cold Cloud Run start the container boot alone
# eats most of that budget before the handler runs — stacking a Sheets read on
# top overran the deadline and surfaced as "The application did not respond".
# X-Signature-Timestamp lets us see how much of the 3s is already gone; when
# it's spent, the modal must open immediately WITHOUT the prefill read, even if
# the stub is instant.
def stale_signed_post(body: dict, age_s: int):
    raw = json.dumps(body)
    ts = str(int(time.time()) - age_s)  # sign with a timestamp `age_s` in the past
    sig = PRIVATE_KEY.sign(f"{ts}{raw}".encode()).hex()
    return client.post(
        "/interactions",
        data=raw,
        content_type="application/json",
        headers={"X-Signature-Ed25519": sig, "X-Signature-Timestamp": ts},
    )


_calls = {"n": 0}


def counting_prefill(uid):
    _calls["n"] += 1
    return ("200 lbs", "190 lbs", "Sleep more")


sheets.get_user_prefill = counting_prefill
resp = stale_signed_post(cmd_interaction("checkin"), age_s=5)  # 5s ago → past the 3s deadline
modal = resp.get_json()
stale_rows = {
    r["component"]["custom_id"]: r["component"]
    for r in modal["data"]["components"]
    if r["component"]["type"] == 4
}
check("cold start → modal still opens", modal["type"] == 9)
check("cold start → prefill skipped, no value", "value" not in stale_rows["last_week_weight"])
check("cold start → sheet not even read", _calls["n"] == 0)
check("cold start → default proud-of placeholder",
      stale_rows["proud_of"]["placeholder"] == "Something you accomplished this week")
sheets.get_user_prefill = lambda uid: ("200 lbs", "190 lbs", "Sleep more")

# ── 3. Deferred commands enqueue tasks ─────────────────────────────────────────
enqueued.clear()
resp = signed_post(cmd_interaction("summary"))
check("summary → deferred", resp.get_json()["type"] == 5)
check("summary enqueued", enqueued[-1][0]["kind"] == "summary" and enqueued[-1][1] == "https://example.run.app")

resp = signed_post(cmd_interaction("progress"))
body = resp.get_json()
check("progress default ephemeral", body["type"] == 5 and body["data"]["flags"] == 64)
check("progress enqueued view=all", enqueued[-1][0]["kind"] == "progress" and enqueued[-1][0]["view"] == "all")

resp = signed_post(cmd_interaction("progress", [{"name": "share", "value": True}]))
check("progress share → public defer", resp.get_json()["data"] == {})

resp = signed_post(cmd_interaction("recap"))
check("recap → deferred, public", resp.get_json() == {"type": 5})
check("recap enqueued with its token",
      enqueued[-1][0]["kind"] == "weekly_recap" and enqueued[-1][0]["token"] == "tok-abc")

resp = signed_post(cmd_interaction("goal", [{"name": "set", "type": 1, "options": [{"name": "weight", "type": 10, "value": 170}]}]))
check("goal set → deferred ephemeral", resp.get_json() == {"type": 5, "data": {"flags": 64}})
check("goal set enqueued with the weight",
      enqueued[-1][0]["kind"] == "goal_set" and enqueued[-1][0]["goal"] == 170 and enqueued[-1][0]["username"] == "joe")
resp = signed_post(cmd_interaction("goal", [{"name": "clear", "type": 1}]))
check("goal clear → deferred ephemeral", resp.get_json() == {"type": 5, "data": {"flags": 64}})
check("goal clear enqueued", enqueued[-1][0]["kind"] == "goal_clear" and "goal" not in enqueued[-1][0])

resp = signed_post(cmd_interaction("history"))
body = resp.get_json()
check(
    "history → sheet link, ephemeral",
    body["type"] == 4
    and "https://docs.google.com/spreadsheets/d/SHEET123" in body["data"]["content"]
    and body["data"]["flags"] == 64,
)

# ── 4. Modal submit ────────────────────────────────────────────────────────────
modal_submit = {
    "type": 5,
    "token": "tok-modal",
    "member": {"user": USER, "nick": "Joey"},
    "data": {
        "custom_id": "checkin_modal",
        "components": [
            {"components": [{"custom_id": "current_weight", "value": "185 lbs"}]},
            {"components": [{"custom_id": "last_week_weight", "value": "186.2"}]},
            {"components": [{"custom_id": "starting_weight", "value": "200"}]},
            {"components": [{"custom_id": "proud_of", "value": "Ran 3x"}]},
            {"components": [{"custom_id": "can_work_on", "value": "Sleep"}]},
        ],
    },
}
enqueued.clear()
resp = signed_post(modal_submit)
check("modal submit → ephemeral defer", resp.get_json() == {"type": 5, "data": {"flags": 64}})
task = enqueued[-1][0]
check("modal submit task", task["kind"] == "checkin_submit" and task["values"]["current_weight"] == "185 lbs")
check("username no discriminator", task["username"] == "joe")
check("nick captured", task["member_nick"] == "Joey")

# ── 5. Progress buttons (components) ───────────────────────────────────────────
component = {
    "type": 3,
    "token": "tok-comp",
    "member": {"user": USER, "nick": None},
    "data": {"custom_id": "progress:6m:42", "component_type": 2},
}
enqueued.clear()
resp = signed_post(component)
check("button (owner) → deferred update", resp.get_json()["type"] == 6)
check("button enqueued view=6m", enqueued[-1][0]["kind"] == "progress" and enqueued[-1][0]["view"] == "6m")

other = dict(component)
other["member"] = {"user": {**USER, "id": "777"}, "nick": None}
resp = signed_post(other)
body = resp.get_json()
check(
    "button (not owner) → ephemeral rebuff",
    body["type"] == 4 and body["data"]["flags"] == 64 and "someone else" in body["data"]["content"],
)

# ── 6. /process endpoint ───────────────────────────────────────────────────────
resp = client.post("/process", json={"kind": "summary"}, headers={"X-Task-Secret": "wrong"})
check("process wrong secret → 403", resp.status_code == 403)

calls: dict[str, list] = {"edit": [], "post": []}
discord_api.edit_original_response = lambda token, payload, file_buf=None, filename="progress.png": calls[
    "edit"
].append((token, payload, file_buf))
discord_api.post_channel_message = lambda cid, payload, file_buf=None, filename="progress.png": calls[
    "post"
].append((cid, payload, file_buf))
app_module.discord_api = discord_api

def _raise_sheets(uid):
    raise RuntimeError("sheets down")


# checkin_submit task
logged = []
sheets.log_checkin = lambda **kw: logged.append(kw)
resp = client.post(
    "/process",
    json={
        "kind": "checkin_submit",
        "token": "tok-modal",
        "user": USER,
        "member_nick": "Joey",
        "username": "joe",
        "values": {
            "current_weight": "185 lbs",
            "last_week_weight": "186.2",
            "proud_of": "Ran 3x",
            "can_work_on": "Sleep",
        },
    },
    headers={"X-Task-Secret": "s3cret"},
)
check("checkin task → 200", resp.status_code == 200)
check("checkin row logged", logged[0]["current_weight"] == "185 lbs" and logged[0]["user_id"] == "42")
# Starting Weight is no longer submitted by the modal — it comes from the sheet.
check("starting weight recovered from sheet", logged[0]["starting_weight"] == "200 lbs")
cid, embed_payload, _ = calls["post"][0]
embed = embed_payload["embeds"][0]
check("checkin embed → right channel", cid == "999888777")
check("checkin embed title uses nick", embed["title"] == "Weekly Check-in — Joey")
weight_field = embed["fields"][0]
check("weight change computed", "📉 -1.2" in weight_field["value"], weight_field["value"])
total_field = next(f for f in embed["fields"] if f["name"] == "📊 Total Change")
check("total change computed", total_field["value"] == "📉 -15.0 lbs", total_field["value"])
_names = [f["name"] for f in embed["fields"]]
check("last week's focus shown on the embed", "🔁 Last week's focus" in _names, str(_names))
check(
    "last week's focus sits right above Proud of",
    _names.index("🔁 Last week's focus") + 1 == _names.index("🌟 Proud of"),
)
check(
    "last week's focus carries last week's Can Work On",
    next(f for f in embed["fields"] if f["name"] == "🔁 Last week's focus")["value"] == "Sleep more",
)
check("checkin ephemeral confirmed", calls["edit"][0][1]["content"].startswith("✅"))

# First-ever check-in: nothing in the sheet to recover, so today's weight IS the
# starting weight. And a Sheets outage must not lose the check-in entirely.
submit_body = {
    "kind": "checkin_submit", "token": "t", "user": USER, "username": "joe",
    "values": {"current_weight": "185 lbs", "last_week_weight": "", "proud_of": "x", "can_work_on": "y"},
}
for label, stub, expected in [
    ("first check-in", lambda uid: (None, None, None), "185 lbs"),
    ("prefill raises", _raise_sheets, "185 lbs"),
]:
    logged.clear()
    sheets.get_user_prefill = stub
    resp = client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
    check(f"{label} → 200", resp.status_code == 200)
    check(f"{label} → starting falls back to current", logged[0]["starting_weight"] == expected)
    total_field = next(f for f in calls["post"][-1][1]["embeds"][0]["fields"] if f["name"] == "📊 Total Change")
    check(f"{label} → total change is zero", total_field["value"] == "➡️ +0.0 lbs", total_field["value"])
    check(f"{label} → no focus field",
          not any(f["name"] == "🔁 Last week's focus" for f in calls["post"][-1][1]["embeds"][0]["fields"]))
sheets.get_user_prefill = lambda uid: ("200 lbs", "190 lbs", "Sleep more")

# Streaks on the check-in embed. The task reads history before writing the row
# and adds this check-in itself, so the footer counts the week being logged.
_now = datetime.now(timezone.utc)
_weekly = lambda ks: [{"date": _now - timedelta(weeks=k), "weight": 190.0} for k in ks]  # noqa: E731


def _footer():
    return calls["post"][-1][1]["embeds"][0]["footer"]["text"]


def _milestones():
    return [p for p in calls["post"] if p[1].get("embeds", [{}])[0].get("title", "").startswith("🏅")]


sheets.get_user_history = lambda uid: _weekly([3, 2, 1])
calls["post"].clear()
client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
_posts = list(calls["post"])
_checkin_footer = _posts[0][1]["embeds"][0]["footer"]["text"]
check("4th consecutive week → streak in footer", _checkin_footer.startswith("🔥 4-week streak"), _checkin_footer)
check("4th consecutive week → milestone posted", len(_milestones()) == 1 and _milestones()[0][0] == "999888777")
check("milestone names the streak and the member",
      _milestones()[0][1]["embeds"][0]["title"] == "🏅 4-week streak — Joe")
check("check-in embed posts before the milestone", _posts[0][1]["embeds"][0]["title"].startswith("Weekly Check-in"))

# Already checked in this week: same streak, but no second celebration.
sheets.get_user_history = lambda uid: _weekly([3, 2, 1, 0])
calls["post"].clear()
client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
check("second check-in this week → streak unchanged", _footer().startswith("🔥 4-week streak"), _footer())
check("second check-in this week → no repeat milestone", _milestones() == [])

# A gap: last check-in three weeks ago → this is week 1 of a new streak.
sheets.get_user_history = lambda uid: _weekly([3])
calls["post"].clear()
client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
check("broken streak → plain footer", _footer() == "Keep it up! 💪", _footer())

# Two weeks running shows, but isn't a milestone.
sheets.get_user_history = lambda uid: _weekly([1])
calls["post"].clear()
client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
check("2-week streak → footer", _footer().startswith("🔥 2-week streak"), _footer())
check("2-week streak → no milestone", _milestones() == [])

# History unavailable → the check-in still posts, just without a streak.
sheets.get_user_history = _raise_sheets
calls["post"].clear()
resp = client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
check("history raises → still 200 and posted", resp.status_code == 200 and len(calls["post"]) == 1)
check("history raises → plain footer", _footer() == "Keep it up! 💪")

# The fixture list must not be mutated by the task adding today's point.
_fixture = _weekly([2, 1])
sheets.get_user_history = lambda uid: _fixture
client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
check("task does not mutate the history it was given", len(_fixture) == 2)

# Goal tasks.
goal_writes: list = []
sheets.set_goal = lambda uid, username, goal: goal_writes.append((uid, username, goal))
sheets.clear_goal = lambda uid: True
GOAL_TASK = {"kind": "goal_set", "token": "t", "user": USER, "username": "joe"}
calls["edit"].clear()
client.post("/process", json={**GOAL_TASK, "goal": 170}, headers={"X-Task-Secret": "s3cret"})
check("goal_set writes the goal", goal_writes == [("42", "joe", 170.0)])
check("goal_set confirms with the number", "170.0 lbs" in calls["edit"][-1][1]["content"])
for bad in (-5, 0, "abc", None):
    goal_writes.clear(); calls["edit"].clear()
    client.post("/process", json={**GOAL_TASK, "goal": bad}, headers={"X-Task-Secret": "s3cret"})
    check(f"goal_set rejects {bad!r}", goal_writes == [] and "⚠️" in calls["edit"][-1][1]["content"])
calls["edit"].clear()
client.post("/process", json={**GOAL_TASK, "kind": "goal_clear"}, headers={"X-Task-Secret": "s3cret"})
check("goal_clear confirms", "cleared" in calls["edit"][-1][1]["content"])
sheets.clear_goal = lambda uid: False
calls["edit"].clear()
client.post("/process", json={**GOAL_TASK, "kind": "goal_clear"}, headers={"X-Task-Secret": "s3cret"})
check("goal_clear with nothing set says so", "don't have a goal" in calls["edit"][-1][1]["content"])

# The check-in embed shows the goal line: starting 200 (prefill), current 185, goal 170.
sheets.get_goal = lambda uid: 170.0
calls["post"].clear()
client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
_goal_field = next((f for f in calls["post"][0][1]["embeds"][0]["fields"] if f["name"] == "🏁 Goal"), None)
check("check-in embed shows goal progress",
      _goal_field is not None and _goal_field["value"] == "170.0 lbs — 15.0 to go (50% there)", str(_goal_field))
sheets.get_goal = _raise_sheets
calls["post"].clear()
resp = client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
check("goal lookup failing → check-in still posts, no goal field",
      resp.status_code == 200 and not any(f["name"] == "🏁 Goal" for f in calls["post"][0][1]["embeds"][0]["fields"]))
sheets.get_goal = lambda uid: None

# summary task
sheets.get_latest_checkins = lambda limit=10: [
    {"Username": "joe", "Current Weight": "185", "Last Week Weight": 186.2, "Proud Of": "Ran", "Can Work On": "Sleep"},
    {"Username": "amy", "Current Weight": 150, "Last Week Weight": 151, "Proud Of": "Lifted", "Can Work On": "Water"},
]
calls["edit"].clear()
resp = client.post("/process", json={"kind": "summary", "token": "t"}, headers={"X-Task-Secret": "s3cret"})
embed = calls["edit"][0][1]["embeds"][0]
check("summary embed built", embed["title"] == "📊 Latest Check-ins" and len(embed["fields"]) == 3)
check("summary sheet link", "SHEET123" in embed["fields"][-1]["value"])
check("summary mixed types ok", "150" in embed["fields"][1]["value"])

# empty summary
sheets.get_latest_checkins = lambda limit=10: []
calls["edit"].clear()
client.post("/process", json={"kind": "summary", "token": "t"}, headers={"X-Task-Secret": "s3cret"})
check("empty summary message", "Be the first" in calls["edit"][0][1]["content"])

# progress task — real chart rendering
now = datetime.now(timezone.utc)
fake_history = [
    {"date": now - timedelta(days=90 - i * 7), "weight": 200 - i * 1.1} for i in range(13)
]
sheets.get_user_history = lambda uid: fake_history
calls["edit"].clear()
resp = client.post(
    "/process",
    json={"kind": "progress", "view": "all", "token": "t", "user": USER, "member_nick": None},
    headers={"X-Task-Secret": "s3cret"},
)
token, payload, file_buf = calls["edit"][0]
embed = payload["embeds"][0]
check("progress embed title", embed["title"] == "📈 Progress — Joe")
check("progress chart is real PNG", file_buf is not None and file_buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n")
check("progress image attachment ref", embed["image"]["url"] == "attachment://progress.png")
buttons = payload["components"][0]["components"]
check(
    "progress buttons",
    [b["custom_id"] for b in buttons] == ["progress:all:42", "progress:6m:42", "progress:30d:42"],
)
check("active button highlighted", buttons[0]["style"] == 1 and buttons[1]["style"] == 2)
field_names = [f["name"] for f in embed["fields"]]
check("progress stats fields", {"🚀 Starting", "⚖️ Current", "Overall", "Pace", "Check-ins"} <= set(field_names))
check("progress shows the streak",
      next(f for f in embed["fields"] if f["name"] == "🔥 Streak")["value"] == "13 wk (best 13)")
check("progress without a goal → no goal fields", not {"🎯 Goal", "📅 Projected"} & set(field_names))


def _progress_with_goal(goal_stub):
    sheets.get_goal = goal_stub
    calls["edit"].clear()
    client.post("/process", json={"kind": "progress", "view": "all", "token": "t", "user": USER, "member_nick": None},
                headers={"X-Task-Secret": "s3cret"})
    _, p, buf = calls["edit"][0]
    fields = {f["name"]: f["value"] for f in p["embeds"][0]["fields"]}
    return fields, buf


# fake_history: 200 → 186.8 over 12 weeks, pace ≈ −1.1 lbs/week.
fields, buf = _progress_with_goal(lambda uid: 180.0)
check("progress goal field", fields.get("🎯 Goal") == "180.0 lbs — 6.8 to go (66% there)", fields.get("🎯 Goal"))
check("progress projects a date", "📅 Projected" in fields and "weeks at this pace" in fields["📅 Projected"], fields.get("📅 Projected"))
_proj_date = datetime.strptime(fields["📅 Projected"].split(" (")[0], "%b %d, %Y").date()
_days_out = (_proj_date - datetime.now(timezone.utc).date()).days
check("projection lands ~6 weeks out", 35 <= _days_out <= 50, str(_days_out))
check("chart still renders with a goal line", buf is not None and buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n")
fields, _ = _progress_with_goal(lambda uid: 60.0)
check("goal too far at this pace → no projection", "🎯 Goal" in fields and "📅 Projected" not in fields)
fields, _ = _progress_with_goal(lambda uid: 250.0)
check("goal in the wrong direction → shown, not projected",
      fields.get("🎯 Goal") == "250.0 lbs — 63.2 to go (0% there)" and "📅 Projected" not in fields, fields.get("🎯 Goal"))
fields, _ = _progress_with_goal(lambda uid: 190.0)
check("goal already reached", fields.get("🎯 Goal") == "190.0 lbs — ✅ reached!" and "📅 Projected" not in fields)
fields, buf = _progress_with_goal(_raise_sheets)
check("goal lookup failing → chart still returned", "🎯 Goal" not in fields and buf is not None)
sheets.get_goal = lambda uid: None

# A recorded Starting Weight that predates the first logged weigh-in (v1 asked
# for it explicitly) must drive Starting / Overall / goal %, as it already does
# on the check-in embed. Found live: 191 logged first, 201 recorded, so
# /progress said −2.0 and 8% while the check-in said −12.0.
_recorded = [{**h, "starting": 210.0} for h in fake_history]
sheets.get_user_history = lambda uid: _recorded
fields, _ = _progress_with_goal(lambda uid: 180.0)
check("progress uses the recorded starting weight", fields["🚀 Starting"] == "210.0 lbs", fields["🚀 Starting"])
check("overall change measured from the recorded start", fields["Overall"] == "📉 -23.2 lbs", fields["Overall"])
check("goal percent measured from the recorded start",
      fields["🎯 Goal"] == "180.0 lbs — 6.8 to go (77% there)", fields["🎯 Goal"])
_blank_start = [{**h, "starting": None} for h in fake_history]
sheets.get_user_history = lambda uid: _blank_start
fields, _ = _progress_with_goal(lambda uid: None)
check("blank starting column → first weigh-in, as before", fields["🚀 Starting"] == "200.0 lbs")
sheets.get_user_history = lambda uid: fake_history
sheets.get_goal = lambda uid: None

# progress: 30d view via button — only recent points
calls["edit"].clear()
client.post(
    "/process",
    json={"kind": "progress", "view": "30d", "token": "t", "user": USER, "member_nick": None},
    headers={"X-Task-Secret": "s3cret"},
)
buttons = calls["edit"][0][1]["components"][0]["components"]
check("30d button now primary", buttons[2]["style"] == 1 and buttons[0]["style"] == 2)

# progress with <2 checkins
sheets.get_user_history = lambda uid: fake_history[:1]
calls["edit"].clear()
client.post(
    "/process",
    json={"kind": "progress", "view": "all", "token": "t", "user": USER, "member_nick": None},
    headers={"X-Task-Secret": "s3cret"},
)
check("progress <2 checkins message", "at least **2 check-ins**" in calls["edit"][0][1]["content"])

# task failure → still 200, user notified
def boom(uid):
    raise RuntimeError("sheets down")

sheets.get_user_history = boom
calls["edit"].clear()
resp = client.post(
    "/process",
    json={"kind": "progress", "view": "all", "token": "t", "user": USER, "member_nick": None},
    headers={"X-Task-Secret": "s3cret"},
)
check("task error → 200 (no retry)", resp.status_code == 200)
check("task error → user warned", "⚠️" in calls["edit"][0][1]["content"])

# ── 6b. Admin alerts: background failures reach a human ───────────────────────
# /process swallows errors by design (200, so Cloud Tasks never retries and
# double-writes), which is why every v1 outage sat in the logs for hours. With
# ADMIN_USER_ID set the same failure also lands in the admin's DMs.
dms_admin: list = []
_real_send_dm = discord_api.send_dm
_stub_send_dm = (
    lambda uid, payload, file_buf=None, filename="progress.png": dms_admin.append((str(uid), payload))
)
discord_api.send_dm = _stub_send_dm
os.environ["ADMIN_USER_ID"] = "777"
PROGRESS_TASK = {"kind": "progress", "view": "all", "token": "t", "user": USER, "member_nick": None}

calls["edit"].clear(); dms_admin.clear()
resp = client.post("/process", json=PROGRESS_TASK, headers={"X-Task-Secret": "s3cret"})
check("admin alert → still 200", resp.status_code == 200)
check("admin alert → user still warned", "⚠️" in calls["edit"][0][1]["content"])
check("admin alert → one DM to the admin", len(dms_admin) == 1 and dms_admin[0][0] == "777")
_text = dms_admin[0][1]["content"]
check(
    "admin alert names the task, user and error",
    "progress" in _text and "42" in _text and "RuntimeError" in _text and "sheets down" in _text,
)
check("admin alert carries a traceback", "```" in _text and "boom" in _text)
check("admin alert fits Discord's limit", len(_text) <= 2000)


# A huge exception message must neither exceed 2000 chars nor eat the fence.
def boom_long(uid):
    raise RuntimeError("x" * 5000)


sheets.get_user_history = boom_long
dms_admin.clear()
client.post("/process", json=PROGRESS_TASK, headers={"X-Task-Secret": "s3cret"})
_text = dms_admin[0][1]["content"]
check("oversized error → DM capped at 2000", len(_text) <= 2000)
check("oversized error → fence still closed", _text.endswith("```"))


# The alert failing must never break the always-200 rule or the user's reply.
def dm_fails(uid, payload, file_buf=None, filename="progress.png"):
    raise RuntimeError("Cannot send messages to this user")


discord_api.send_dm = dm_fails
sheets.get_user_history = boom
calls["edit"].clear()
resp = client.post("/process", json=PROGRESS_TASK, headers={"X-Task-Secret": "s3cret"})
check("admin DM failure → still 200", resp.status_code == 200)
check("admin DM failure → user still warned", "⚠️" in calls["edit"][0][1]["content"])
discord_api.send_dm = _stub_send_dm

# An unknown kind is a deploy/registration mismatch — worth a DM too.
dms_admin.clear()
client.post("/process", json={"kind": "no-such-kind", "token": "t", "user": USER},
            headers={"X-Task-Secret": "s3cret"})
check("unknown kind → admin DM names it", len(dms_admin) == 1 and "no-such-kind" in dms_admin[0][1]["content"])

# The admin_alert task kind is how the ack path reports (it can't DM inline).
dms_admin.clear()
client.post("/process", json={"kind": "admin_alert", "text": "hi"}, headers={"X-Task-Secret": "s3cret"})
check("admin_alert task → DM sent", dms_admin == [("777", {"content": "hi"})])

# Unset (the default, and CI) → nothing is sent.
os.environ["ADMIN_USER_ID"] = ""
dms_admin.clear()
client.post("/process", json=PROGRESS_TASK, headers={"X-Task-Secret": "s3cret"})
check("no ADMIN_USER_ID → no DM", dms_admin == [])

# A skipped /checkin prefill is the silent precursor to "did not respond". It is
# reported through a Cloud Task from a worker thread — never inline on the 3s
# path — and at most once per cooldown so a cold-start burst is one DM.
os.environ["ADMIN_USER_ID"] = "777"
app_module._prefill_skips = 0
app_module._last_prefill_alert_ts = 0.0
enqueued.clear()
resp = stale_signed_post(cmd_interaction("checkin"), age_s=5)
check("prefill skipped → modal still opens", resp.get_json()["type"] == 9)
for _ in range(200):
    if any(p["kind"] == "admin_alert" for p, _ in enqueued):
        break
    time.sleep(0.02)
_alerts = [p for p, _ in enqueued if p["kind"] == "admin_alert"]
check(
    "prefill skipped → admin alert enqueued from a worker",
    len(_alerts) == 1 and "prefill skipped" in _alerts[0]["text"] and "1 time(s)" in _alerts[0]["text"],
)
check("prefill alert task goes to this service", enqueued[-1][1] == "https://example.run.app")
stale_signed_post(cmd_interaction("checkin"), age_s=5)
time.sleep(0.1)
check(
    "prefill skipped again → inside cooldown, still one alert",
    sum(1 for p, _ in enqueued if p["kind"] == "admin_alert") == 1,
)
check("skips inside the cooldown are still counted", app_module._prefill_skips == 1)
os.environ["ADMIN_USER_ID"] = ""
discord_api.send_dm = _real_send_dm
enqueued.clear()

# ── 7. /reminder ───────────────────────────────────────────────────────────────
resp = client.post("/reminder", headers={"X-Reminder-Secret": "nope"})
check("reminder wrong secret → 403", resp.status_code == 403)

# Scheduler only waits on the enqueue; the recap is built and posted by /process.
calls["post"].clear(); enqueued.clear()
resp = client.post("/reminder", headers={"X-Reminder-Secret": "s3cret"})
check("reminder → 200", resp.status_code == 200)
check("reminder enqueues the recap", enqueued[-1][0] == {"kind": "weekly_recap"})
check("reminder posts nothing inline", calls["post"] == [])


# If the enqueue itself fails, the plain prompt still goes out — a silent Monday
# is the one outcome the reminder exists to prevent.
def _enqueue_down(payload, self_url):
    raise RuntimeError("tasks down")


tasks_queue.enqueue = _enqueue_down
calls["post"].clear()
resp = client.post("/reminder", headers={"X-Reminder-Secret": "s3cret"})
check("enqueue down → still 200", resp.status_code == 200)
cid, payload, _ = calls["post"][0]
check("enqueue down → plain reminder posted inline",
      payload["embeds"][0]["title"].startswith("🏋️") and cid == "999888777" and "fields" not in payload["embeds"][0])
tasks_queue.enqueue = fake_enqueue

# The recap task. Rows are placed relative to this Monday in the local zone.
_tz = streaks.LOCAL_TZ
_loc_today = datetime.now(timezone.utc).astimezone(_tz).date()
_this_mon = _loc_today - timedelta(days=_loc_today.weekday())


def _on(weeks_ago: int, uid: str, name: str, weight: float, dow: int = 2):
    d = _this_mon - timedelta(weeks=weeks_ago) + timedelta(days=dow)
    return {"user_id": uid, "username": name, "weight": weight, "starting": None,
            "date": datetime(d.year, d.month, d.day, 12, tzinfo=_tz).astimezone(timezone.utc)}


_recap_rows = (
    [_on(k, "1", "joe", w) for k, w in zip((5, 4, 3, 2, 1), (200, 199, 198, 197, 195))]
    + [_on(3, "2", "amy", 150)]
    + [_on(2, "3", "bob", 190), _on(1, "3", "bob", 189)]
)
sheets.get_all_checkins = lambda: _recap_rows
calls["post"].clear(); calls["edit"].clear()
resp = client.post("/process", json={"kind": "weekly_recap"}, headers={"X-Task-Secret": "s3cret"})
check("recap task → 200, one channel post", resp.status_code == 200 and len(calls["post"]) == 1 and calls["edit"] == [])
_recap = calls["post"][0][1]["embeds"][0]
check("recap keeps the check-in prompt", _recap["title"].startswith("🏋️") and "/checkin" in _recap["description"])
_rf = {f["name"].split(" (")[0]: f["value"] for f in _recap["fields"]}
check("recap lists last week", "📋 Last week" in _rf, str(list(_rf)))
_week = _rf["📋 Last week"]
check("recap: joe checked in with a 5-week streak and his change",
      "✅ **joe** · 🔥 5 wk · 📉 -2.0 lbs" in _week, _week)
check("recap: bob's 2-week streak and change", "✅ **bob** · 🔥 2 wk · 📉 -1.0 lbs" in _week, _week)
check("recap: amy missed", "— **amy** · missed last week" in _week, _week)
check("recap: checked-in members first", _week.index("bob") < _week.index("joe") < _week.index("amy"))
check("recap: group total", _rf["👥 Group"] == "Combined: 📉 -6.0 lbs since everyone started", _rf["👥 Group"])
check("recap: biggest mover by percent", _rf["🏆 Biggest mover"] == "**joe** (-1.0% last week)", _rf["🏆 Biggest mover"])

# /recap answers the deferred interaction instead of posting a second copy.
calls["post"].clear(); calls["edit"].clear()
client.post("/process", json={"kind": "weekly_recap", "token": "tok-recap", "user": USER}, headers={"X-Task-Secret": "s3cret"})
check("/recap → edits the deferred reply, no channel post",
      calls["post"] == [] and calls["edit"][0][0] == "tok-recap" and "fields" in calls["edit"][0][1]["embeds"][0])

# Nobody has checked in yet → just the prompt.
sheets.get_all_checkins = lambda: []
calls["post"].clear()
client.post("/process", json={"kind": "weekly_recap"}, headers={"X-Task-Secret": "s3cret"})
check("empty sheet → plain prompt, no fields", "fields" not in calls["post"][0][1]["embeds"][0])

# Sheets down → the prompt still posts (the admin alert path is covered in 6b).
def _all_checkins_down():
    raise RuntimeError("sheets down")


sheets.get_all_checkins = _all_checkins_down
calls["post"].clear()
resp = client.post("/process", json={"kind": "weekly_recap"}, headers={"X-Task-Secret": "s3cret"})
check("recap data failure → 200 and plain prompt posted",
      resp.status_code == 200 and len(calls["post"]) == 1 and "fields" not in calls["post"][0][1]["embeds"][0])

# ── 8. discord_api multipart building ──────────────────────────────────────────
import importlib

importlib.reload(discord_api)  # restore real functions after monkeypatching
kwargs = discord_api._multipart({"embeds": []}, io.BytesIO(b"png-bytes"), "progress.png")
pj = json.loads(kwargs["files"]["payload_json"][1])
check("multipart attachments field", pj["attachments"] == [{"id": 0, "filename": "progress.png"}])
check("multipart file bytes", kwargs["files"]["files[0]"][1] == b"png-bytes")
check("plain json when no file", discord_api._multipart({"a": 1}, None, "x") == {"json": {"a": 1}})

check("avatar url (default)", discord_api.avatar_url(USER).startswith("https://cdn.discordapp.com/embed/avatars/"))
check(
    "avatar url (hash)",
    discord_api.avatar_url({**USER, "avatar": "abc"}) == "https://cdn.discordapp.com/avatars/42/abc.png",
)

# ── 9. register_commands payload sanity ────────────────────────────────────────
import register_commands

by_name = {c["name"]: c for c in register_commands.COMMANDS}
check(
    "10 commands registered",
    sorted(by_name) == sorted(
        ["checkin", "summary", "progress", "history", "day1",
         "collage", "howto", "photo-replace", "goal", "recap"]
    ),
    str(sorted(by_name)),
)


def opt(cmd: str, name: str) -> dict:
    return next(o for o in by_name[cmd]["options"] if o["name"] == name)


check("share option is boolean+optional",
      opt("progress", "share")["type"] == 5 and opt("progress", "share")["required"] is False)
check("day1 photo option is attachment+required",
      opt("day1", "photo")["type"] == 11 and opt("day1", "photo")["required"] is True)
check("photo-replace date has autocomplete",
      opt("photo-replace", "date")["type"] == 3
      and opt("photo-replace", "date").get("autocomplete") is True)
check("photo-replace photo is attachment+required",
      opt("photo-replace", "photo")["type"] == 11 and opt("photo-replace", "photo")["required"] is True)
check("goal has set/clear sub-commands",
      [(o["name"], o["type"]) for o in by_name["goal"]["options"]] == [("set", 1), ("clear", 1)])
_goal_weight = opt("goal", "set")["options"][0]
check("goal set takes a required number",
      _goal_weight["name"] == "weight" and _goal_weight["type"] == 10 and _goal_weight["required"] is True)

# Drift guard: a command Discord knows about but app.py can't answer produces
# "the application did not respond" in the channel, which is invisible here
# unless we check the two lists agree.
handled = set(re.findall(r'if name == "([a-z0-9-]+)"', open(
    os.path.join(ROOT, "app.py"), encoding="utf-8").read()))
check("every registered command is handled in app.py",
      set(by_name) <= handled, str(set(by_name) - handled))
check("every handled command is registered",
      handled <= set(by_name), str(handled - set(by_name)))

# The docs have twice been left claiming a stale command count, which reads as a
# failed deploy to anyone following the checklist. Pin them to the real number.
_count = len(register_commands.COMMANDS)
for _doc in ("TODO.md", "docs/REDEPLOY_CHECKLIST.md", "docs/OPERATIONS_GUIDE.md",
             "docs/SETUP_GUIDE.md", "scripts/redeploy.sh"):
    _txt = open(os.path.join(ROOT, *_doc.split("/")), encoding="utf-8").read()
    _stale = [n for n in range(1, 21) if n != _count
              and (f"**{n}** commands" in _txt or f"{n} commands" in _txt
                   or f"(must print **{n}**)" in _txt)]
    check(f"{_doc} states the real command count", not _stale, f"claims {_stale}, is {_count}")


# ── 10. Progress photos: modal upload, /day1, consent, before/after ────────────
from PIL import Image as _PILImage  # noqa: E402

_pb = io.BytesIO()
_PILImage.new("RGB", (8, 8), (100, 130, 160)).save(_pb, format="PNG")
TINY_PNG = _pb.getvalue()

# Modal submit carrying a photo → checkin_submit task gets the resolved URL
modal_photo = {
    "type": 5,
    "token": "tok-photo",
    "member": {"user": USER, "nick": "Joey"},
    "data": {
        "custom_id": "checkin_modal",
        "components": [
            {"components": [{"type": 4, "custom_id": "current_weight", "value": "185 lbs"}]},
            {"components": [{"type": 4, "custom_id": "last_week_weight", "value": "186.2"}]},
            {"components": [{"type": 4, "custom_id": "starting_weight", "value": "200"}]},
            {"components": [{"type": 4, "custom_id": "proud_of", "value": "Ran 3x"}]},
            {"components": [{"type": 4, "custom_id": "can_work_on", "value": "Sleep"}]},
            {"type": 18, "component": {"type": 19, "custom_id": "progress_pic", "values": ["att-1"]}},
        ],
        "resolved": {"attachments": {"att-1": {"id": "att-1", "url": "https://cdn/att-1.png"}}},
    },
}
enqueued.clear()
signed_post(modal_photo)
task = enqueued[-1][0]
check("modal photo → photo_url resolved", task["photo_url"] == "https://cdn/att-1.png")
check(
    "modal photo → text values intact, file skipped",
    task["values"]["current_weight"] == "185 lbs" and "progress_pic" not in task["values"],
)

# Modal submit WITHOUT a photo → photo_url is None (existing flow unaffected)
no_photo = json.loads(json.dumps(modal_photo))
no_photo["data"]["components"] = no_photo["data"]["components"][:5]
no_photo["data"].pop("resolved")
enqueued.clear()
signed_post(no_photo)
check("modal no photo → photo_url None", enqueued[-1][0]["photo_url"] is None)

# /day1 command → set_baseline enqueued with the resolved attachment
day1_cmd = {
    "type": 2,
    "token": "tok-day1",
    "member": {"user": USER, "nick": None},
    "data": {
        "name": "day1",
        "options": [{"name": "photo", "type": 11, "value": "att-9"}],
        "resolved": {"attachments": {"att-9": {"id": "att-9", "url": "https://cdn/att-9.png"}}},
    },
}
enqueued.clear()
resp = signed_post(day1_cmd)
check("day1 → ephemeral defer", resp.get_json() == {"type": 5, "data": {"flags": 64}})
check(
    "day1 enqueued set_baseline",
    enqueued[-1][0]["kind"] == "set_baseline" and enqueued[-1][0]["photo_url"] == "https://cdn/att-9.png",
)

# Consent button → grant_consent enqueued (owner only)
enqueued.clear()
resp = signed_post(
    {"type": 3, "token": "tok-consent", "member": {"user": USER, "nick": None},
     "data": {"custom_id": "photo_consent:42", "component_type": 2}}
)
check("consent button → deferred update", resp.get_json()["type"] == 6)
check("consent enqueued grant_consent", enqueued[-1][0]["kind"] == "grant_consent")

resp = signed_post(
    {"type": 3, "token": "tok-consent", "member": {"user": {**USER, "id": "777"}, "nick": None},
     "data": {"custom_id": "photo_consent:42", "component_type": 2}}
)
check("consent button (not owner) → ignored", resp.get_json()["type"] == 6 and enqueued[-1][0]["kind"] == "grant_consent")

# ── Photo /process tasks — stub Sheet photo-state and Discord image IO ──────────
photo_state = {"consent": False, "day1_ref": None, "pending_url": None,
               "pending_kind": None, "pending_date": None}


def fake_get_photo_state(uid):
    s = dict(photo_state)
    s["day1_ref"] = s["day1_ref"] or None
    s["pending_url"] = s["pending_url"] or None
    s["pending_kind"] = s.get("pending_kind") or None
    s["pending_date"] = s.get("pending_date") or None
    return s


upserts: list = []


def fake_upsert(uid, username, **fields):
    upserts.append((uid, username, dict(fields)))
    for k, v in fields.items():
        photo_state[k] = v


sheets.get_photo_state = fake_get_photo_state
sheets.upsert_photo_state = fake_upsert
sheets.log_checkin = lambda **kw: None


def fake_post(cid, payload, file_buf=None, filename="progress.png"):
    calls["post"].append((cid, payload, file_buf, filename))
    return {"id": "stored-1", "timestamp": "2026-01-02T00:00:00+00:00",
            "attachments": [{"url": "https://cdn/stored-1.png"}]}


discord_api.post_channel_message = fake_post
discord_api.edit_original_response = (
    lambda token, payload, file_buf=None, filename="progress.png": calls["edit"].append((token, payload, file_buf))
)
discord_api.download_image = lambda url: TINY_PNG
# Discord's real shape for a Day 1 post: uploading a file *and* referencing it
# from an embed via attachment:// moves the file into the embed and leaves
# `attachments` empty. Modelling this wrongly is what hid the IndexError at
# _post_progress_photo for every before/after until 2026-07-27.
discord_api.get_message = lambda cid, mid: {
    "id": mid, "timestamp": "2026-01-01T00:00:00+00:00",
    "attachments": [],
    "embeds": [{"title": "📸 Day 1", "image": {"url": "https://cdn/day1-fresh.png"}}],
}
app_module.discord_api = discord_api


def checkin_photo_payload():
    return {
        "kind": "checkin_submit", "token": "tok-modal", "user": USER, "member_nick": "Joey",
        "username": "joe", "photo_url": "https://cdn/att-1.png",
        "values": {"current_weight": "185 lbs", "last_week_weight": "186.2", "starting_weight": "200",
                   "proud_of": "Ran 3x", "can_work_on": "Sleep"},
    }


def upsert_fields():
    return [f for (_, _, f) in upserts]


# (a) photo + NOT consented → only the text embed posts; pending stashed; consent button
photo_state.update({"consent": False, "day1_ref": None, "pending_url": None})
calls["post"].clear(); calls["edit"].clear(); upserts.clear()
client.post("/process", json=checkin_photo_payload(), headers={"X-Task-Secret": "s3cret"})
check("unconsented photo → only text embed posted", len(calls["post"]) == 1
      and calls["post"][0][1]["embeds"][0]["title"].startswith("Weekly Check-in"))
check("unconsented photo → pending stashed", any(f.get("pending_url") == "https://cdn/att-1.png" for f in upsert_fields()))
check("unconsented photo → consent button shown",
      calls["edit"][-1][1]["components"][0]["components"][0]["custom_id"] == "photo_consent:42")

# (b) grant consent → posts pending as Day 1 (no baseline yet); records consent + ref; clears pending
photo_state.update({"consent": False, "day1_ref": None, "pending_url": "https://cdn/att-1.png"})
calls["post"].clear(); calls["edit"].clear(); upserts.clear()
client.post("/process", json={"kind": "grant_consent", "token": "tok-c", "user": USER,
                              "member_nick": None, "username": "joe"}, headers={"X-Task-Secret": "s3cret"})
check("grant consent → Day 1 posted", len(calls["post"]) == 1
      and calls["post"][0][1]["embeds"][0]["title"].startswith("📸 Day 1"))
check("grant consent → real PNG uploaded",
      calls["post"][0][2] is not None and calls["post"][0][2].getvalue()[:8] == b"\x89PNG\r\n\x1a\n")
check("grant consent → consent recorded", any(f.get("consent") is True for f in upsert_fields()))
check("grant consent → day1_ref stored", any(f.get("day1_ref") == "stored-1" for f in upsert_fields()))
check("grant consent → pending cleared", any(f.get("pending_url") == "" for f in upsert_fields()))
check("grant consent → ephemeral confirm", "Shared" in calls["edit"][-1][1]["content"])

# (c) check-in photo + consented + HAS baseline → text embed + before/after composite
photo_state.update({"consent": True, "day1_ref": "day1-msg", "pending_url": None})
calls["post"].clear(); calls["edit"].clear()
client.post("/process", json=checkin_photo_payload(), headers={"X-Task-Secret": "s3cret"})
check("consented w/ baseline → text + composite posted", len(calls["post"]) == 2)
_ba = calls["post"][1]
check("before/after embed + filename", _ba[1]["embeds"][0]["title"].startswith("🔥 Before & After")
      and _ba[3] == "beforeafter.png")
check("before/after real PNG", _ba[2] is not None and _ba[2].getvalue()[:8] == b"\x89PNG\r\n\x1a\n")
check("consented photo confirm", "before & after" in calls["edit"][-1][1]["content"].lower())

# (d) /day1 consented → posts Day 1 and overwrites the stored reference
photo_state.update({"consent": True, "day1_ref": "old-ref", "pending_url": None})
calls["post"].clear(); calls["edit"].clear(); upserts.clear()
client.post("/process", json={"kind": "set_baseline", "token": "tok-b", "user": USER, "member_nick": None,
                              "username": "joe", "photo_url": "https://cdn/att-9.png"},
            headers={"X-Task-Secret": "s3cret"})
check("set_baseline → Day 1 posted", len(calls["post"]) == 1
      and calls["post"][0][1]["embeds"][0]["title"].startswith("📸 Day 1"))
check("set_baseline → ref overwritten", any(f.get("day1_ref") == "stored-1" for f in upsert_fields()))
check("set_baseline → confirm", "Day 1 photo saved" in calls["edit"][-1][1]["content"])

# (e) /day1 NOT consented → nothing posts publicly; pending stashed + consent button
photo_state.update({"consent": False, "day1_ref": None, "pending_url": None})
calls["post"].clear(); calls["edit"].clear(); upserts.clear()
client.post("/process", json={"kind": "set_baseline", "token": "tok-b2", "user": USER, "member_nick": None,
                              "username": "joe", "photo_url": "https://cdn/att-9.png"},
            headers={"X-Task-Secret": "s3cret"})
check("set_baseline unconsented → no public post", len(calls["post"]) == 0)
check("set_baseline unconsented → consent button",
      calls["edit"][-1][1]["components"][0]["components"][0]["custom_id"] == "photo_consent:42")

# ── 8. Photo history: sampling, collage, replace, autocomplete ────────────────
import images  # noqa: E402

# sample_timeline: never exceeds the cap, always keeps both ends.
check("sample 0 photos", images.sample_timeline([]) == [])
check("sample under cap unchanged", images.sample_timeline([1, 2, 3]) == [1, 2, 3])
_fifty = list(range(50))
_s = images.sample_timeline(_fifty)
check("sample caps at 9", len(_s) == 9, str(len(_s)))
check("sample keeps first and last", _s[0] == 0 and _s[-1] == 49, str(_s))
check("sample is ordered + unique", _s == sorted(set(_s)))
check("sample limit=1 takes latest", images.sample_timeline(_fifty, limit=1) == [49])

# Archive OFF (ARCHIVE_CHANNEL_ID unset): check-ins must be unaffected. The
# photo flows above already ran in this state — assert the switch explicitly.
check("archive disabled when unconfigured", app_module._archive_channel() is None)

photo_log: list[dict] = []
deactivated: list[tuple] = []
logged_rows: list[dict] = []
sheets.get_photo_log = lambda uid, active_only=True: list(photo_log)
sheets.append_photo_log = lambda **kw: logged_rows.append(kw)


def fake_deactivate(uid, taken_on):
    deactivated.append((uid, taken_on))
    return next((p for p in photo_log if p["taken_on"] == taken_on), None)


sheets.deactivate_photo_log_row = fake_deactivate
deleted: list[tuple] = []
discord_api.delete_message = lambda cid, mid: deleted.append((str(cid), str(mid))) or True
app_module.discord_api = discord_api

# Everything below needs the archive configured.
os.environ["ARCHIVE_CHANNEL_ID"] = "555444333"
check("archive enabled when configured", app_module._archive_channel() == "555444333")

# /collage with no photos → friendly nudge, nothing posted
photo_log.clear(); calls["post"].clear(); calls["edit"].clear()
client.post("/process", json={"kind": "collage", "token": "tok-col", "user": USER, "member_nick": None},
            headers={"X-Task-Secret": "s3cret"})
check("collage empty → nudge", "No progress photos yet" in calls["edit"][-1][1]["content"])
check("collage empty → nothing posted", len(calls["post"]) == 0)

# /collage with photos → renders a real PNG
photo_log.extend([
    {"taken_on": "2026-01-01", "archive_ref": "a1", "post_ref": "p1", "kind": "day1"},
    {"taken_on": "2026-02-01", "archive_ref": "a2", "post_ref": "p2", "kind": "progress"},
    {"taken_on": "2026-03-01", "archive_ref": "a3", "post_ref": "p3", "kind": "progress"},
])
calls["edit"].clear()
client.post("/process", json={"kind": "collage", "token": "tok-col2", "user": USER, "member_nick": "Joey"},
            headers={"X-Task-Secret": "s3cret"})
_col = calls["edit"][-1]
check("collage → real PNG", _col[2] is not None and _col[2].getvalue()[:8] == b"\x89PNG\r\n\x1a\n")
check("collage → embed titled", _col[1]["embeds"][0]["title"].startswith("🖼️ Progress Collage"))
check("collage → date range in description", "2026-01-01" in _col[1]["embeds"][0]["description"]
      and "2026-03-01" in _col[1]["embeds"][0]["description"])

# A single unreadable archive photo degrades that panel, not the whole collage.
_real_get = discord_api.get_message
discord_api.get_message = lambda cid, mid: (_ for _ in ()).throw(RuntimeError("gone")) if mid == "a2" \
    else _real_get(cid, mid)
calls["edit"].clear()
client.post("/process", json={"kind": "collage", "token": "tok-col3", "user": USER, "member_nick": None},
            headers={"X-Task-Secret": "s3cret"})
check("collage survives one bad photo", calls["edit"][-1][2] is not None)
check("collage counts only readable panels", "2 photos" in calls["edit"][-1][1]["embeds"][0]["description"])
discord_api.get_message = _real_get

# /photo-replace on a known date → deletes both old copies, posts + relogs
photo_state.update({"consent": True, "day1_ref": "old-ref", "pending_url": None})
calls["post"].clear(); calls["edit"].clear(); deleted.clear(); logged_rows.clear()
client.post("/process", json={"kind": "photo_replace", "token": "tok-r", "user": USER, "member_nick": None,
                              "username": "joe", "taken_on": "2026-02-01",
                              "photo_url": "https://cdn/new.png"}, headers={"X-Task-Secret": "s3cret"})
check("replace → deactivated the row", deactivated[-1] == ("42", "2026-02-01"))
check("replace → deleted archive + public copies",
      ("555444333", "a2") in deleted and ("999888777", "p2") in deleted, str(deleted))
_public = [c for c in calls["post"] if c[0] == "999888777"]
_arch = [c for c in calls["post"] if c[0] == "555444333"]
check("replace → one public post", len(_public) == 1
      and _public[0][1]["embeds"][0]["title"].startswith("🔄 Updated photo"))
check("replace → filename matches embed attachment", _public[0][3] == "progress.png")
check("replace → raw photo re-archived", len(_arch) == 1)
check("replace → new log row", logged_rows[-1]["taken_on"] == "2026-02-01"
      and logged_rows[-1]["kind"] == "progress")
check("replace → confirms with pretty date", "Feb 01, 2026" in calls["edit"][-1][1]["content"])

# Replacing the Day 1 photo must re-point the baseline, or before/after breaks.
calls["post"].clear(); calls["edit"].clear(); upserts.clear()
client.post("/process", json={"kind": "photo_replace", "token": "tok-r2", "user": USER, "member_nick": None,
                              "username": "joe", "taken_on": "2026-01-01",
                              "photo_url": "https://cdn/new2.png"}, headers={"X-Task-Secret": "s3cret"})
_public = [c for c in calls["post"] if c[0] == "999888777"]
check("replace day1 → uses day1 embed", _public[0][1]["embeds"][0]["title"].startswith("📸 Day 1"))
check("replace day1 → filename day1.png", _public[0][3] == "day1.png")
check("replace day1 → baseline re-pointed", any(f.get("day1_ref") == "stored-1" for f in upsert_fields()))

# Unknown date → clear error, nothing deleted or posted
calls["post"].clear(); calls["edit"].clear(); deleted.clear()
client.post("/process", json={"kind": "photo_replace", "token": "tok-r3", "user": USER, "member_nick": None,
                              "username": "joe", "taken_on": "1999-01-01",
                              "photo_url": "https://cdn/new.png"}, headers={"X-Task-Secret": "s3cret"})
check("replace unknown date → no deletes", deleted == [])
check("replace unknown date → no post", len(calls["post"]) == 0)
check("replace unknown date → explains", "No photo found" in calls["edit"][-1][1]["content"])

# Replace without consent → consent prompt, nothing destroyed
photo_state.update({"consent": False, "day1_ref": None, "pending_url": None})
calls["post"].clear(); calls["edit"].clear(); deleted.clear()
client.post("/process", json={"kind": "photo_replace", "token": "tok-r4", "user": USER, "member_nick": None,
                              "username": "joe", "taken_on": "2026-02-01",
                              "photo_url": "https://cdn/new.png"}, headers={"X-Task-Secret": "s3cret"})
check("replace unconsented → no deletes", deleted == [])
check("replace unconsented → consent button",
      calls["edit"][-1][1]["components"][0]["components"][0]["custom_id"] == "photo_consent:42")

# ── Day 1 image resolution ─────────────────────────────────────────────────────
# The 2026-07-27 outage: a Day 1 post keeps its photo in the embed, not in
# `attachments`, so reading attachments[0] raised IndexError and *every*
# before/after died. Cover each source the composite can come from.
check("resolves an embed image", app_module._message_image_url(
    {"attachments": [], "embeds": [{"image": {"url": "https://cdn/e.png"}}]}) == "https://cdn/e.png")
check("resolves a real attachment", app_module._message_image_url(
    {"attachments": [{"url": "https://cdn/a.png"}], "embeds": []}) == "https://cdn/a.png")
check("no image → None", app_module._message_image_url({"attachments": [], "embeds": [{}]}) is None)
check("missing keys → None", app_module._message_image_url({}) is None)

# (a) Archive ref preferred: the raw PNG beats the embed's re-encoded copy.
fetched: list = []
_photo_log_snapshot = list(photo_log)  # restored below; later checks rely on it
photo_state.update({"consent": True, "day1_ref": "day1-msg", "pending_url": None})
photo_log.clear()
photo_log.append({"taken_on": "2026-01-01", "archive_ref": "arch-1", "post_ref": "p1", "kind": "day1"})
_real_get = discord_api.get_message


def tracking_get(cid, mid):
    fetched.append(str(mid))
    return _real_get(cid, mid)


discord_api.get_message = tracking_get
app_module.discord_api = discord_api
calls["post"].clear(); calls["edit"].clear()
client.post("/process", json=checkin_photo_payload(), headers={"X-Task-Secret": "s3cret"})
check("before/after uses the archived Day 1", "arch-1" in fetched)
check("before/after posted from archive",
      any(p[1].get("embeds", [{}])[0].get("title", "").startswith("🔥 Before & After")
          for p in calls["post"]))

# (b) No Photo Log row (a baseline predating the archive) → falls back to the embed.
photo_log.clear()
calls["post"].clear(); calls["edit"].clear()
client.post("/process", json=checkin_photo_payload(), headers={"X-Task-Secret": "s3cret"})
check("before/after falls back to the embed image",
      any(p[1].get("embeds", [{}])[0].get("title", "").startswith("🔥 Before & After")
          for p in calls["post"]))
check("embed fallback confirms before & after",
      "before & after" in calls["edit"][-1][1]["content"].lower())

# (c) Baseline unreadable → becomes a new Day 1 instead of raising.
discord_api.get_message = lambda cid, mid: {
    "id": mid, "timestamp": "2026-01-01T00:00:00+00:00", "attachments": [], "embeds": [],
}
app_module.discord_api = discord_api
calls["post"].clear(); calls["edit"].clear(); upserts.clear()
resp = client.post("/process", json=checkin_photo_payload(), headers={"X-Task-Secret": "s3cret"})
check("unreadable baseline → still 200", resp.status_code == 200)
check("unreadable baseline → posts a fresh Day 1",
      any(p[1].get("embeds", [{}])[0].get("title", "").startswith("📸 Day 1") for p in calls["post"]))
check("unreadable baseline → day1_ref replaced",
      any(f.get("day1_ref") == "stored-1" for (_, _, f) in upserts))
check("unreadable baseline → user told it reset",
      "new **Day 1**" in calls["edit"][-1][1]["content"])

discord_api.get_message = _real_get
app_module.discord_api = discord_api

# ── Dead interaction token ─────────────────────────────────────────────────────
# Missing Discord's 3s ack permanently invalidates the token, so the reply — and
# any consent button riding on it — has to reach the user another way.
check("404/10015 is a dead token", discord_api._is_dead_token(404, {"code": 10015}))
check("401/50027 is a dead token", discord_api._is_dead_token(401, {"code": 50027}))
check("404/other is not a dead token", not discord_api._is_dead_token(404, {"code": 10008}))
check("429 is not a dead token", not discord_api._is_dead_token(429, {}))
check("no body is not a dead token", not discord_api._is_dead_token(404, None))

dms: list = []
_live_edit = discord_api.edit_original_response


def dead_edit(token, payload, file_buf=None, filename="progress.png"):
    raise discord_api.DeadInteractionToken("404/10015")


discord_api.edit_original_response = dead_edit
discord_api.send_dm = (
    lambda uid, payload, file_buf=None, filename="progress.png": dms.append((str(uid), payload))
)
app_module.discord_api = discord_api

# The exact 18:45 incident: /day1 on a cold start, unconsented.
photo_state.update({"consent": False, "day1_ref": None, "pending_url": None})
calls["post"].clear(); calls["edit"].clear(); upserts.clear(); dms.clear()
resp = client.post("/process", json={"kind": "set_baseline", "token": "tok-dead", "user": USER,
                                     "member_nick": None, "username": "joe",
                                     "photo_url": "https://cdn/att-1.png"},
                   headers={"X-Task-Secret": "s3cret"})
check("dead token → still 200", resp.status_code == 200)
check("dead token → pending photo still stashed",
      any(f.get("pending_url") == "https://cdn/att-1.png" for (_, _, f) in upserts))
check("dead token → consent prompt delivered by DM", len(dms) == 1 and dms[0][0] == "42")
check("dead token → DM keeps the working button",
      dms[0][1]["components"][0]["components"][0]["custom_id"] == "photo_consent:42")

# The text check-in must survive a dead token even though the photo reply can't.
photo_state.update({"consent": False, "day1_ref": None, "pending_url": None})
calls["post"].clear(); dms.clear()
client.post("/process", json=checkin_photo_payload(), headers={"X-Task-Secret": "s3cret"})
check("dead token → check-in embed still posts",
      any(p[1].get("embeds", [{}])[0].get("title", "").startswith("Weekly Check-in")
          for p in calls["post"]))
check("dead token → user still reached by DM", len(dms) == 1)

# DMs closed → a public nudge that reveals nothing about a photo.
def dm_blocked(uid, payload, file_buf=None, filename="progress.png"):
    raise RuntimeError("Cannot send messages to this user")


discord_api.send_dm = dm_blocked
app_module.discord_api = discord_api
photo_state.update({"consent": False, "day1_ref": None, "pending_url": None})
calls["post"].clear(); dms.clear()
client.post("/process", json={"kind": "set_baseline", "token": "tok-dead2", "user": USER,
                              "member_nick": None, "username": "joe",
                              "photo_url": "https://cdn/att-1.png"},
            headers={"X-Task-Secret": "s3cret"})
_nudges = [p for p in calls["post"] if "<@42>" in str(p[1].get("content", ""))]
check("DM blocked → public nudge sent", len(_nudges) == 1)
check("public nudge keeps the photo private",
      "photo" not in _nudges[0][1]["content"].lower() and "components" not in _nudges[0][1])

# A transient Discord 500 is NOT a dead token: no DM, no public nudge.
def flaky_edit(token, payload, file_buf=None, filename="progress.png"):
    raise RuntimeError("500 Server Error")


discord_api.edit_original_response = flaky_edit
discord_api.send_dm = (
    lambda uid, payload, file_buf=None, filename="progress.png": dms.append((str(uid), payload))
)
app_module.discord_api = discord_api
calls["post"].clear(); dms.clear()
client.post("/process", json={"kind": "summary", "token": "tok-flaky", "user": USER},
            headers={"X-Task-Secret": "s3cret"})
check("transient error → no DM", dms == [])
check("transient error → no public nudge",
      not any("<@42>" in str(p[1].get("content", "")) for p in calls["post"]))

discord_api.edit_original_response = _live_edit
app_module.discord_api = discord_api

# An unrecognised task kind must still answer. The interaction is already
# deferred by the time /process runs, so returning silently leaves the user
# staring at "thinking…" until Discord gives up.
calls["edit"].clear()
resp = client.post("/process", json={"kind": "no-such-kind", "token": "tok-unknown", "user": USER},
                   headers={"X-Task-Secret": "s3cret"})
check("unknown task kind → still 200", resp.status_code == 200)
check("unknown task kind → user gets an answer",
      len(calls["edit"]) == 1 and "went wrong" in calls["edit"][-1][1]["content"])

# ── Recap maths ────────────────────────────────────────────────────────────────
_wed = datetime(2026, 10, 7).date()  # a Wednesday → last week is Sep 28 – Oct 4
check("recap: last week bounds", recap.last_week_bounds(_wed) == (datetime(2026, 9, 28).date(), datetime(2026, 10, 4).date()))
check("recap: Monday's last week is the week just ended",
      recap.last_week_bounds(datetime(2026, 10, 5).date()) == (datetime(2026, 9, 28).date(), datetime(2026, 10, 4).date()))


def _row(uid, name, y, m, d, w, starting=None):
    return {"user_id": uid, "username": name, "weight": w, "starting": starting,
            "date": datetime(y, m, d, 18, tzinfo=timezone.utc)}


_s = recap.summarize_week([_row("1", "joe", 2026, 9, 22, 190), _row("1", "joe", 2026, 9, 29, 189), _row("1", "joe", 2026, 10, 3, 188)], _wed)
check("recap: two check-ins in one week → latest weight, one member",
      len(_s["members"]) == 1 and _s["members"][0]["change"] == -2 and _s["members"][0]["streak"] == 2)
check("recap: week label", _s["week_label"] == "Sep 28 – Oct 04")
_s = recap.summarize_week([_row("1", "joe", 2026, 9, 22, 190), _row("1", "joe", 2026, 9, 30, 192)], _wed)
check("recap: everyone gained → no biggest mover", _s["biggest_mover"] is None and _s["members"][0]["change"] == 2)
_s = recap.summarize_week([_row("1", "joe", 2026, 9, 30, 192)], _wed)
check("recap: first-ever check-in last week → checked in, no change",
      _s["members"][0]["checked_in"] and _s["members"][0]["change"] is None and _s["members"][0]["total"] == 0)
_s = recap.summarize_week([_row("1", "joe", 2026, 9, 1, 200, starting=210), _row("1", "joe", 2026, 9, 30, 195)], _wed)
check("recap: total uses the recorded starting weight", _s["members"][0]["total"] == -15 and _s["combined"] == -15)
_s = recap.summarize_week([_row("1", "joe", 2026, 9, 8, 200), _row("1", "joe", 2026, 9, 15, 199)], _wed)
check("recap: a member who missed last week reads streak 0",
      not _s["members"][0]["checked_in"] and _s["members"][0]["streak"] == 0)
check("recap: empty", recap.summarize_week([], _wed) == {"week_label": "Sep 28 – Oct 04", "members": [], "combined": 0, "biggest_mover": None})

# ── Goal maths ─────────────────────────────────────────────────────────────────
_gp = goals.progress(200, 185, 170)
check("goals: losing toward a lower goal", _gp["direction"] == -1 and _gp["remaining"] == 15 and _gp["pct"] == 50 and not _gp["reached"])
_gp = goals.progress(150, 160, 170)
check("goals: gaining toward a higher goal", _gp["direction"] == 1 and _gp["remaining"] == 10 and _gp["pct"] == 50)
check("goals: reached", goals.progress(200, 168, 170)["reached"] and goals.progress(200, 168, 170)["remaining"] == -2)
check("goals: pct clamps when moving away", goals.progress(200, 210, 170)["pct"] == 0)
check("goals: goal equal to start → None", goals.progress(200, 190, 200) is None)
_today = datetime(2026, 10, 6).date()
_pd, _pw = goals.projected_date(6.6, -1.1, -1, _today)
check("goals: projection at pace", _pd == datetime(2026, 11, 17).date() and abs(_pw - 6.0) < 1e-9, f"{_pd} {_pw}")
check("goals: no pace → None", goals.projected_date(10, None, -1, _today) is None)
check("goals: wrong-sign pace → None", goals.projected_date(10, 0.5, -1, _today) is None)
check("goals: already reached → None", goals.projected_date(0, -1.0, -1, _today) is None)
check("goals: beyond two years → None", goals.projected_date(120, -1.0, -1, _today) is None)
check("goals: describe", goals.describe(170, goals.progress(200, 185, 170)) == "170.0 lbs — 15.0 to go (50% there)")
check("goals: describe reached", goals.describe(170, goals.progress(200, 165, 170)) == "170.0 lbs — ✅ reached!")
check("goals: describe without progress", goals.describe(170, None) == "170.0 lbs")

# ── Streaks: consecutive local weeks ───────────────────────────────────────────
def _at(y, m, d, h=12):
    return {"date": datetime(y, m, d, h, tzinfo=timezone.utc), "weight": 1.0}


check("streaks: empty", streaks.compute_streaks([]) == {"current": 0, "longest": 0, "weeks": 0})
_five = [_at(2026, 1, 5 + 7 * k) for k in range(4)] + [_at(2026, 2, 2)]
check("streaks: five consecutive weeks", streaks.compute_streaks(_five) == {"current": 5, "longest": 5, "weeks": 5})
_gap = [_at(2026, 1, 5), _at(2026, 1, 12), _at(2026, 1, 19), _at(2026, 2, 2), _at(2026, 2, 9)]
check("streaks: a gap resets current but keeps longest",
      streaks.compute_streaks(_gap) == {"current": 2, "longest": 3, "weeks": 5})
check("streaks: two check-ins in one week count once",
      streaks.compute_streaks([_at(2026, 1, 5), _at(2026, 1, 8)])["weeks"] == 1)
check("streaks: Mon→Sun order within a week irrelevant",
      streaks.compute_streaks([_at(2026, 1, 11), _at(2026, 1, 12)]) == {"current": 2, "longest": 2, "weeks": 2})
check("streaks: year boundary is consecutive",
      streaks.compute_streaks([_at(2025, 12, 29), _at(2026, 1, 5)])["current"] == 2)
# Sunday 22:00 Pacific is Monday 06:00 UTC — it belongs to the Sunday's week.
check("streaks: late Sunday Pacific stays in its week",
      streaks.compute_streaks([_at(2026, 1, 5), _at(2026, 1, 12, 6)])["weeks"] == 1)
check("streaks: Monday 08:00 Pacific starts the next week",
      streaks.compute_streaks([_at(2026, 1, 5), _at(2026, 1, 12, 16)])["weeks"] == 2)
# as_of: the recap's view of "last week".
_run = [_at(2026, 1, 5), _at(2026, 1, 12), _at(2026, 1, 19)]
check("streaks: as_of in the latest week → unchanged",
      streaks.compute_streaks(_run, as_of=datetime(2026, 1, 22, tzinfo=timezone.utc))["current"] == 3)
check("streaks: as_of a week later → reset",
      streaks.compute_streaks(_run, as_of=datetime(2026, 1, 28, tzinfo=timezone.utc))["current"] == 0)
check("streaks: as_of ignores later check-ins",
      streaks.compute_streaks(_run, as_of=datetime(2026, 1, 14, tzinfo=timezone.utc)) == {"current": 2, "longest": 2, "weeks": 2})
check("streaks: naive datetimes are treated as UTC",
      streaks.week_index(datetime(2026, 1, 5, 12)) == streaks.week_index(datetime(2026, 1, 5, 12, tzinfo=timezone.utc)))
check("streaks: milestones", streaks.MILESTONES == (4, 8, 12, 26, 52))

# ── Sheet headers reconcile without moving data ────────────────────────────────
# The rollback guarantee lives here. If v2 adds a column and someone reverts to
# an older build, that build must NOT rewrite the header — doing so destroys the
# newer columns and pushes every record down a row.
class _FakeWS:
    def __init__(self, header, cols=None):
        self.header = list(header)
        self.row_count = 1 if header else 0
        self.col_count = cols if cols is not None else len(header)
        self.title = "Fake"
        self.inserted = []
        self.updated = []
        self.added_cols = 0

    def row_values(self, n):
        return list(self.header)

    def insert_row(self, values, index):
        self.inserted.append((list(values), index))

    def update_cell(self, row, col, value):
        self.updated.append((row, col, value))
        while len(self.header) < col:
            self.header.append("")
        self.header[col - 1] = value

    def add_cols(self, n):
        self.added_cols += n
        self.col_count += n


_H = ["A", "B", "C"]

_ws = _FakeWS(_H)
sheets._ensure_headers(_ws, _H)
check("headers identical → untouched", not _ws.inserted and not _ws.updated)

_ws = _FakeWS(["A", "B"], cols=2)
sheets._ensure_headers(_ws, _H)
check("sheet narrower → header widened in place, no row inserted",
      _ws.updated == [(1, 3, "C")] and not _ws.inserted)
check("sheet narrower → grid widened", _ws.added_cols == 1)

# The rollback case.
_ws = _FakeWS(["A", "B", "C", "D", "E"], cols=5)
sheets._ensure_headers(_ws, _H)
check("sheet wider (rolled-back build) → left completely alone",
      not _ws.inserted and not _ws.updated and _ws.header == ["A", "B", "C", "D", "E"])

_ws = _FakeWS(["totally", "different"])
sheets._ensure_headers(_ws, _H)
check("unrecognisable header → a correct one is inserted",
      _ws.inserted == [(_H, 1)])

_ws = _FakeWS([])
sheets._ensure_headers(_ws, _H)
check("empty sheet → header inserted", _ws.inserted == [(_H, 1)])


# ── Deferred consent honours the original intent ───────────────────────────────
# Consent granted later used to guess what to do from state alone, and guessed
# wrong: a parked /day1 posted a before/after instead of resetting the baseline,
# and a parked /photo-replace lost its date and posted as a photo for today.

# /day1 while unconsented records the intent, not just the URL.
photo_state.update({"consent": False, "day1_ref": None, "pending_url": None,
                    "pending_kind": None, "pending_date": None})
calls["post"].clear(); calls["edit"].clear(); upserts.clear()
client.post("/process", json={"kind": "set_baseline", "token": "tok-d1", "user": USER,
                              "member_nick": None, "username": "joe",
                              "photo_url": "https://cdn/new-baseline.png"},
            headers={"X-Task-Secret": "s3cret"})
check("/day1 unconsented → records kind=day1",
      any(f.get("pending_kind") == "day1" for (_, _, f) in upserts))

# Granting consent must reset the baseline — NOT post a before/after, even
# though a readable day1_ref exists.
photo_state.update({"consent": False, "day1_ref": "day1-msg",
                    "pending_url": "https://cdn/new-baseline.png",
                    "pending_kind": "day1", "pending_date": None})
calls["post"].clear(); calls["edit"].clear(); upserts.clear()
client.post("/process", json={"kind": "grant_consent", "token": "tok-c-d1", "user": USER,
                              "member_nick": None, "username": "joe"},
            headers={"X-Task-Secret": "s3cret"})
_titles = [(p[1].get("embeds") or [{}])[0].get("title", "") for p in calls["post"]]
check("consent on a parked /day1 → posts Day 1",
      any(t.startswith("📸 Day 1") for t in _titles), str(_titles))
check("consent on a parked /day1 → NOT a before/after",
      not any(t.startswith("🔥 Before & After") for t in _titles), str(_titles))
check("consent on a parked /day1 → baseline repointed",
      any(f.get("day1_ref") == "stored-1" for (_, _, f) in upserts))
check("consent on a parked /day1 → says Day 1 is set",
      "Day 1" in calls["edit"][-1][1]["content"])
check("consent on a parked /day1 → pending intent cleared",
      any(f.get("pending_kind") == "" for (_, _, f) in upserts))

# /photo-replace while unconsented keeps the target date with the photo.
photo_state.update({"consent": False, "day1_ref": None, "pending_url": None,
                    "pending_kind": None, "pending_date": None})
os.environ["ARCHIVE_CHANNEL_ID"] = "555444333"
upserts.clear()
client.post("/process", json={"kind": "photo_replace", "token": "tok-r-nc", "user": USER,
                              "member_nick": None, "username": "joe",
                              "taken_on": "2026-02-01", "photo_url": "https://cdn/new.png"},
            headers={"X-Task-Secret": "s3cret"})
check("/photo-replace unconsented → records kind=replace",
      any(f.get("pending_kind") == "replace" for (_, _, f) in upserts))
check("/photo-replace unconsented → keeps the date",
      any(f.get("pending_date") == "2026-02-01" for (_, _, f) in upserts))

# Granting consent must replace THAT date, not post a new photo dated today.
photo_log.clear()
photo_log.extend([
    {"taken_on": "2026-01-01", "archive_ref": "a1", "post_ref": "p1", "kind": "day1"},
    {"taken_on": "2026-02-01", "archive_ref": "a2", "post_ref": "p2", "kind": "progress"},
])
photo_state.update({"consent": False, "day1_ref": None,
                    "pending_url": "https://cdn/new.png",
                    "pending_kind": "replace", "pending_date": "2026-02-01"})
calls["post"].clear(); calls["edit"].clear(); deleted.clear()
client.post("/process", json={"kind": "grant_consent", "token": "tok-c-r", "user": USER,
                              "member_nick": None, "username": "joe"},
            headers={"X-Task-Secret": "s3cret"})
check("consent on a parked /photo-replace → old copies deleted",
      ("555444333", "a2") in deleted and ("999888777", "p2") in deleted, str(deleted))
check("consent on a parked /photo-replace → posts the replacement",
      any((p[1].get("embeds") or [{}])[0].get("title", "").startswith("🔄 Updated photo")
          for p in calls["post"]))
check("consent on a parked /photo-replace → names the original date",
      "Feb 01, 2026" in calls["edit"][-1][1]["content"], calls["edit"][-1][1]["content"])

# A row written before Pending Kind existed still behaves as a check-in photo.
photo_state.update({"consent": False, "day1_ref": None,
                    "pending_url": "https://cdn/att-1.png",
                    "pending_kind": None, "pending_date": None})
calls["post"].clear(); calls["edit"].clear()
client.post("/process", json={"kind": "grant_consent", "token": "tok-c-legacy", "user": USER,
                              "member_nick": None, "username": "joe"},
            headers={"X-Task-Secret": "s3cret"})
check("legacy pending row → still posts as Day 1",
      any((p[1].get("embeds") or [{}])[0].get("title", "").startswith("📸 Day 1")
          for p in calls["post"]))

os.environ["ARCHIVE_CHANNEL_ID"] = ""

# ── Expired pending photo on consent ───────────────────────────────────────────
# Consent must not be recorded while a dead URL stays parked in the sheet: the
# button is gone by then, so nothing would ever retry it.
_live_download = discord_api.download_image


def gone(url):
    raise RuntimeError("410 Gone")


discord_api.download_image = gone
app_module.discord_api = discord_api
photo_state.update({"consent": False, "day1_ref": None, "pending_url": "https://cdn/expired.png"})
calls["post"].clear(); calls["edit"].clear(); upserts.clear()
resp = client.post("/process", json={"kind": "grant_consent", "token": "tok-exp", "user": USER,
                                     "member_nick": None, "username": "joe"},
                   headers={"X-Task-Secret": "s3cret"})
check("expired pending → still 200", resp.status_code == 200)
check("expired pending → consent still recorded", any(f.get("consent") is True for (_, _, f) in upserts))
check("expired pending → stale URL cleared", any(f.get("pending_url") == "" for (_, _, f) in upserts))
check("expired pending → explains how to retry", "/day1" in calls["edit"][-1][1]["content"])
check("expired pending → nothing posted publicly", len(calls["post"]) == 0)

discord_api.download_image = _live_download
app_module.discord_api = discord_api

# ── Cloud Tasks client is built once per process, not per interaction ──────────
# ~2.5s of gRPC import + ADC + TLS on a cold process is most of Discord's 3s
# budget; paying it per request is what made cold /checkin time out.
_builds = []
tasks_queue._client = None
_orig_get_client = tasks_queue._get_client


def counting_get_client():
    if tasks_queue._client is None:
        _builds.append(1)
        tasks_queue._client = object()
    return tasks_queue._client


tasks_queue._get_client = counting_get_client
tasks_queue._get_client(); tasks_queue._get_client(); tasks_queue._get_client()
check("Cloud Tasks client built once", len(_builds) == 1)
tasks_queue._get_client = _orig_get_client
tasks_queue._client = None
check("warmup() is callable off the request path", callable(tasks_queue.warmup))

photo_log.clear(); photo_log.extend(_photo_log_snapshot)

# Autocomplete: type 8, newest first, filtered by what's typed, capped at 25.
def ac_interaction(typed: str) -> dict:
    return {"type": 4, "token": "tok-ac", "data": {
        "name": "photo-replace",
        "options": [{"name": "date", "value": typed, "focused": True}]},
        "member": {"user": USER, "nick": None}}


resp = signed_post(ac_interaction(""))
body = resp.get_json()
check("autocomplete → type 8", body["type"] == 8)
_names = [c["value"] for c in body["data"]["choices"]]
check("autocomplete newest first", _names == ["2026-03-01", "2026-02-01", "2026-01-01"], str(_names))
check("autocomplete labels day1", any("(Day 1)" in c["name"] for c in body["data"]["choices"]))
check("autocomplete filters on typed text",
      [c["value"] for c in signed_post(ac_interaction("02")).get_json()["data"]["choices"]] == ["2026-02-01"])

_many = [{"taken_on": f"2026-{m:02d}-{d:02d}", "archive_ref": "a", "post_ref": "p", "kind": "progress"}
         for m in range(1, 13) for d in (1, 8, 15)]  # 36 > Discord's 25 cap
photo_log.clear(); photo_log.extend(_many)
check("autocomplete caps at 25", len(signed_post(ac_interaction("")).get_json()["data"]["choices"]) == 25)

# A slow sheet must not blow the 3s deadline — degrade to no suggestions.
def slow_log(uid, active_only=True):
    time.sleep(3)
    return _many


sheets.get_photo_log = slow_log
_t0 = time.time()
body = signed_post(ac_interaction("")).get_json()
check("autocomplete degrades within budget",
      body["type"] == 8 and body["data"]["choices"] == [] and time.time() - _t0 < 2.0)
sheets.get_photo_log = lambda uid, active_only=True: list(photo_log)

# /howto renders and is pinnable-shaped (public when shared, ephemeral otherwise)
body = signed_post(cmd_interaction("howto")).get_json()
check("howto → ephemeral by default", body["data"]["flags"] == 64)
body = signed_post(cmd_interaction("howto", [{"name": "share", "value": True}])).get_json()
check("howto share → public", "flags" not in body["data"])
_fields = " ".join(f["value"] for f in body["data"]["embeds"][0]["fields"])
check("howto documents the real modal fields",
      "Current weight" in _fields and "Last week's weight" in _fields)
check("howto doesn't ask for starting weight", "starting weight" not in _fields.lower()
      or "remembered automatically" in _fields)
check("howto lists the new commands", "/collage" in _fields and "/photo-replace" in _fields)

# The weekly reminder must not advertise a field the modal no longer has.
_reminder = app_module._reminder_embed()["description"]
check("reminder drops starting weight", "Starting weight" not in _reminder)
check("reminder mentions optional photo", "progress photo" in _reminder.lower())


# ── Sheets handles are opened once per process ─────────────────────────────────
# Re-opening a tab on every call cost ~0.7s of auth + metadata round trips, so a
# warm /checkin's prefill read (budget ≤1.4s) still timed out and opened the
# modal with Last Week's Weight blank.
class _CountingSpreadsheet:
    def __init__(self, opened):
        self.opened = opened

    def worksheet(self, name):
        self.opened.append(name)
        return _FakeWS(sheets.HEADERS)


class _CountingClient:
    def __init__(self):
        self.opened = []

    def open_by_key(self, key):
        return _CountingSpreadsheet(self.opened)


_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Check-ins")
_auths = []
_cc = _CountingClient()
_orig_sheets_client = sheets._get_client
sheets._get_client = lambda: _auths.append(1) or _cc
sheets._client = None
sheets._tabs.clear()
_ws1 = sheets._get_sheet()
_ws2 = sheets._get_sheet()
sheets._get_photos_sheet()
check("check-ins tab opened once and reused", _ws1 is _ws2 and _cc.opened.count(_TAB) == 1)
check("one Sheets client for every tab", len(_auths) == 1 and len(_cc.opened) == 2)

sheets._tabs.clear()
REAL_SHEETS_WARMUP()
check("warmup() opens the check-ins tab", _TAB in sheets._tabs)
sheets._get_client = _orig_sheets_client
sheets._client = None
sheets._tabs.clear()

print(f"\nAll {PASS} checks passed ✅")

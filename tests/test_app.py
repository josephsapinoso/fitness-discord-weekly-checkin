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
        "TASK_SECRET": "s3cret",
        "PREFILL_TIMEOUT_S": "0.5",
        "SELF_URL": "https://example.run.app",
    }
)

import app as app_module  # noqa: E402
import discord_api  # noqa: E402
import sheets  # noqa: E402
import tasks_queue  # noqa: E402

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
sheets.get_user_prefill = lambda uid: ("200 lbs", "190 lbs")
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
    return ("x", "y")

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

# Restore a fast stub: the checkin_submit task now calls get_user_prefill to
# recover Starting Weight, so leaving slow_prefill bound would add 3s per task.
sheets.get_user_prefill = lambda uid: ("200 lbs", "190 lbs")


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
    return ("200 lbs", "190 lbs")


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
sheets.get_user_prefill = lambda uid: ("200 lbs", "190 lbs")

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
check("checkin ephemeral confirmed", calls["edit"][0][1]["content"].startswith("✅"))

# First-ever check-in: nothing in the sheet to recover, so today's weight IS the
# starting weight. And a Sheets outage must not lose the check-in entirely.
submit_body = {
    "kind": "checkin_submit", "token": "t", "user": USER, "username": "joe",
    "values": {"current_weight": "185 lbs", "last_week_weight": "", "proud_of": "x", "can_work_on": "y"},
}
for label, stub, expected in [
    ("first check-in", lambda uid: (None, None), "185 lbs"),
    ("prefill raises", _raise_sheets, "185 lbs"),
]:
    logged.clear()
    sheets.get_user_prefill = stub
    resp = client.post("/process", json=submit_body, headers={"X-Task-Secret": "s3cret"})
    check(f"{label} → 200", resp.status_code == 200)
    check(f"{label} → starting falls back to current", logged[0]["starting_weight"] == expected)
sheets.get_user_prefill = lambda uid: ("200 lbs", "190 lbs")

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

# ── 7. /reminder ───────────────────────────────────────────────────────────────
resp = client.post("/reminder", headers={"X-Reminder-Secret": "nope"})
check("reminder wrong secret → 403", resp.status_code == 403)

calls["post"].clear()
resp = client.post("/reminder", headers={"X-Reminder-Secret": "s3cret"})
check("reminder → 200", resp.status_code == 200)
cid, payload, _ = calls["post"][0]
check("reminder embed", payload["embeds"][0]["title"].startswith("🏋️") and cid == "999888777")

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
    "8 commands registered",
    sorted(by_name) == sorted(
        ["checkin", "summary", "progress", "history", "day1",
         "collage", "howto", "photo-replace"]
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

# Drift guard: a command Discord knows about but app.py can't answer produces
# "the application did not respond" in the channel, which is invisible here
# unless we check the two lists agree.
handled = set(re.findall(r'if name == "([a-z0-9-]+)"', open(
    os.path.join(ROOT, "app.py"), encoding="utf-8").read()))
check("every registered command is handled in app.py",
      set(by_name) <= handled, str(set(by_name) - handled))
check("every handled command is registered",
      handled <= set(by_name), str(handled - set(by_name)))


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
photo_state = {"consent": False, "day1_ref": None, "pending_url": None}


def fake_get_photo_state(uid):
    s = dict(photo_state)
    s["day1_ref"] = s["day1_ref"] or None
    s["pending_url"] = s["pending_url"] or None
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
discord_api.get_message = lambda cid, mid: {
    "id": mid, "timestamp": "2026-01-01T00:00:00+00:00",
    "attachments": [{"url": "https://cdn/day1-fresh.png"}],
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
check("consented photo confirm", "photo" in calls["edit"][-1][1]["content"])

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

print(f"\nAll {PASS} checks passed ✅")

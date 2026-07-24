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
check("modal has 5 text inputs + photo upload", len(rows) == 6)
inputs = {r["components"][0]["custom_id"]: r["components"][0] for r in rows if "components" in r}
check("prefill last week", inputs["last_week_weight"].get("value") == "190 lbs")
check("prefill starting", inputs["starting_weight"].get("value") == "200 lbs")
check("current not prefilled", "value" not in inputs["current_weight"])
check("paragraph styles", inputs["proud_of"]["style"] == 2 and inputs["can_work_on"]["style"] == 2)
photo_row = next(r for r in rows if r.get("type") == 18)
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
    r["components"][0]["custom_id"]: r["components"][0]
    for r in modal["data"]["components"]
    if "components" in r
}
check("slow prefill → modal within budget", modal["type"] == 9 and elapsed < 2.0, f"{elapsed:.2f}s")
check("slow prefill → no values", "value" not in rows["last_week_weight"])

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
            "starting_weight": "200",
            "proud_of": "Ran 3x",
            "can_work_on": "Sleep",
        },
    },
    headers={"X-Task-Secret": "s3cret"},
)
check("checkin task → 200", resp.status_code == 200)
check("checkin row logged", logged[0]["current_weight"] == "185 lbs" and logged[0]["user_id"] == "42")
cid, embed_payload, _ = calls["post"][0]
embed = embed_payload["embeds"][0]
check("checkin embed → right channel", cid == "999888777")
check("checkin embed title uses nick", embed["title"] == "Weekly Check-in — Joey")
weight_field = embed["fields"][0]
check("weight change computed", "📉 -1.2" in weight_field["value"], weight_field["value"])
check("checkin ephemeral confirmed", calls["edit"][0][1]["content"].startswith("✅"))

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

names = [c["name"] for c in register_commands.COMMANDS]
check("5 commands registered", names == ["checkin", "summary", "progress", "history", "day1"])
share_opt = register_commands.COMMANDS[2]["options"][0]
check("share option is boolean+optional", share_opt["type"] == 5 and share_opt["required"] is False)
day1_opt = register_commands.COMMANDS[4]["options"][0]
check("day1 photo option is attachment+required", day1_opt["type"] == 11 and day1_opt["required"] is True)


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

print(f"\nAll {PASS} checks passed ✅")

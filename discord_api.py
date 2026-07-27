"""
Thin Discord REST helpers (no gateway, no discord.py).

Used by the Cloud Run interactions server to:
  - edit the original (deferred) interaction response, optionally with a file
  - post messages to the check-in channel via the bot token
"""

import io
import json
import logging
import os

import requests

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"

# Discord's classic embed colors (matching discord.py's Color helpers)
COLOR_GREEN = 0x2ECC71
COLOR_BLUE = 0x3498DB
COLOR_GOLD = 0xF1C40F
COLOR_ORANGE = 0xE67E22


def _bot_headers() -> dict:
    return {"Authorization": f"Bot {os.environ['DISCORD_TOKEN']}"}


def _app_id() -> str:
    return os.environ["DISCORD_APPLICATION_ID"]


def avatar_url(user: dict) -> str:
    """CDN avatar URL for a user object (with default-avatar fallback)."""
    uid = user["id"]
    avatar_hash = user.get("avatar")
    if avatar_hash:
        return f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png"
    index = (int(uid) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"


def display_name(user: dict, member: dict | None = None) -> str:
    """Best display name: guild nick > global name > username."""
    if member and member.get("nick"):
        return member["nick"]
    return user.get("global_name") or user.get("username", "Unknown")


def _multipart(payload: dict, file_buf: io.BytesIO | None, filename: str):
    """Build requests kwargs for a JSON or multipart (file) Discord call."""
    if file_buf is None:
        return {"json": payload}
    payload = dict(payload)
    payload["attachments"] = [{"id": 0, "filename": filename}]
    return {
        "files": {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": (filename, file_buf.getvalue(), "image/png"),
        }
    }


class DeadInteractionToken(Exception):
    """The interaction webhook is gone — its 3s ack was missed, or it expired.

    Distinguished from a generic HTTP error because it is not retryable and not
    the user's fault: the reply has to be delivered some other way.
    """


# Unknown Webhook (never acked / expired) and Invalid Webhook Token.
_DEAD_TOKEN_CODES = (10015, 50027)


def _is_dead_token(status: int, body: dict | None) -> bool:
    """Whether a failed interaction-webhook call means the token is unusable."""
    if status not in (401, 404):
        return False
    return (body or {}).get("code") in _DEAD_TOKEN_CODES


def edit_original_response(
    interaction_token: str,
    payload: dict,
    file_buf: io.BytesIO | None = None,
    filename: str = "progress.png",
) -> None:
    """PATCH the original response of a deferred interaction.

    Interaction webhook endpoints are authorized by the token in the URL,
    so no bot Authorization header is needed.
    """
    url = f"{API_BASE}/webhooks/{_app_id()}/{interaction_token}/messages/@original"
    resp = requests.patch(url, timeout=30, **_multipart(payload, file_buf, filename))
    if not resp.ok:
        log.error("edit_original_response failed (%s): %s", resp.status_code, resp.text)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if _is_dead_token(resp.status_code, body):
            raise DeadInteractionToken(f"{resp.status_code}/{(body or {}).get('code')}")
    resp.raise_for_status()


def send_dm(
    user_id: int | str,
    payload: dict,
    file_buf: io.BytesIO | None = None,
    filename: str = "progress.png",
) -> dict:
    """Open (or reuse) the user's DM channel and post there via the bot token.

    Used when a deferred reply can no longer reach its interaction. Buttons keep
    working in DMs, so a consent prompt delivered this way is still actionable.
    """
    resp = requests.post(
        f"{API_BASE}/users/@me/channels",
        headers=_bot_headers(),
        json={"recipient_id": str(user_id)},
        timeout=30,
    )
    if not resp.ok:
        log.error("open DM failed (%s): %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return post_channel_message(resp.json()["id"], payload, file_buf, filename)


def post_channel_message(
    channel_id: int | str,
    payload: dict,
    file_buf: io.BytesIO | None = None,
    filename: str = "progress.png",
) -> dict:
    """POST a message to a channel using the bot token; return the new message.

    The returned dict includes the message `id`, which callers use to keep a
    durable reference to an uploaded photo (Discord CDN URLs expire, but the
    message can be re-fetched later for a fresh signed URL — see get_message).
    """
    url = f"{API_BASE}/channels/{channel_id}/messages"
    resp = requests.post(
        url, headers=_bot_headers(), timeout=30, **_multipart(payload, file_buf, filename)
    )
    if not resp.ok:
        log.error("post_channel_message failed (%s): %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


def get_message(channel_id: int | str, message_id: int | str) -> dict:
    """GET a channel message via the bot token.

    Re-fetching a message returns fresh, non-expired signed attachment URLs,
    which is how we durably re-read a previously uploaded photo.
    """
    url = f"{API_BASE}/channels/{channel_id}/messages/{message_id}"
    resp = requests.get(url, headers=_bot_headers(), timeout=30)
    if not resp.ok:
        log.error("get_message failed (%s): %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


def delete_message(channel_id: int | str, message_id: int | str) -> bool:
    """DELETE a channel message. Returns True if it's gone.

    Only ever called on the bot's OWN messages, which needs no Manage Messages
    permission. A 404 means someone already removed it by hand — that's the
    desired end state, so it counts as success rather than an error.
    """
    url = f"{API_BASE}/channels/{channel_id}/messages/{message_id}"
    resp = requests.delete(url, headers=_bot_headers(), timeout=30)
    if resp.status_code == 404:
        return True
    if not resp.ok:
        log.error("delete_message failed (%s): %s", resp.status_code, resp.text)
        return False
    return True


# Reject anything larger than this before decoding (guards memory / decode bombs).
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", 10 * 1024 * 1024))
_ALLOWED_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")


def download_image(url: str) -> bytes:
    """Download an image URL, enforcing a content-type allowlist and size cap."""
    resp = requests.get(url, timeout=30, stream=True)
    resp.raise_for_status()

    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type and content_type not in _ALLOWED_IMAGE_TYPES:
        raise ValueError(f"unsupported image content-type: {content_type!r}")

    declared = resp.headers.get("Content-Length")
    if declared and int(declared) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds size limit")

    chunks = bytearray()
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        chunks.extend(chunk)
        if len(chunks) > MAX_IMAGE_BYTES:
            raise ValueError("image exceeds size limit")
    return bytes(chunks)

"""
One-time registration of the bot's slash commands with Discord.

The old gateway bot already registered these same commands via tree.sync(),
so running this is mostly a safety net — it bulk-overwrites the global
command list so it exactly matches what app.py handles.

Usage:
    DISCORD_TOKEN=... DISCORD_APPLICATION_ID=... python register_commands.py
    (or put both in .env)
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

COMMANDS = [
    {"name": "checkin", "description": "Submit your weekly fitness check-in"},
    {"name": "summary", "description": "Show the latest check-ins for the group"},
    {
        "name": "progress",
        "description": "Your personal weight progress chart",
        "options": [
            {
                "type": 5,  # boolean
                "name": "share",
                "description": "Post publicly in the channel instead of just to you",
                "required": False,
            }
        ],
    },
    {"name": "history", "description": "Link to the full check-in history spreadsheet"},
    {
        "name": "day1",
        "description": "Set your Day 1 baseline photo for before/after comparisons",
        "options": [
            {
                "type": 11,  # attachment
                "name": "photo",
                "description": "Your starting progress photo",
                "required": True,
            }
        ],
    },
]


def main() -> None:
    app_id = os.environ["DISCORD_APPLICATION_ID"]
    url = f"https://discord.com/api/v10/applications/{app_id}/commands"
    resp = requests.put(
        url,
        headers={"Authorization": f"Bot {os.environ['DISCORD_TOKEN']}"},
        json=COMMANDS,
        timeout=30,
    )
    resp.raise_for_status()
    names = ", ".join(f"/{c['name']}" for c in resp.json())
    print(f"Registered {len(resp.json())} global commands: {names}")


if __name__ == "__main__":
    main()

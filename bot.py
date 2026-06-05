"""
Fitness Discord Bot — weekly check-in facilitator.

Commands:
  /checkin   — opens a modal to submit this week's check-in
  /summary   — posts the latest check-ins for the group
  /history   — link to the full Google Sheet

Scheduling:
  Every week on the configured day/time the bot posts a reminder prompt.
"""

import os
import logging
from datetime import datetime, time, timezone

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import sheets

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config from environment ────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHECKIN_CHANNEL_ID = int(os.environ["CHECKIN_CHANNEL_ID"])

# Day of week (0=Mon … 6=Sun) and UTC hour for the weekly reminder
REMINDER_WEEKDAY = int(os.environ.get("REMINDER_WEEKDAY", "0"))   # Monday
REMINDER_HOUR    = int(os.environ.get("REMINDER_HOUR", "9"))      # 09:00 UTC
REMINDER_MINUTE  = int(os.environ.get("REMINDER_MINUTE", "0"))


# ── Modal ──────────────────────────────────────────────────────────────────────
class CheckinModal(discord.ui.Modal, title="Weekly Fitness Check-in 💪"):
    current_weight = discord.ui.TextInput(
        label="Current Weight",
        placeholder="e.g. 185 lbs",
        required=True,
        max_length=50,
    )
    last_week_weight = discord.ui.TextInput(
        label="Last Week's Weight",
        placeholder="e.g. 187 lbs",
        required=True,
        max_length=50,
    )
    starting_weight = discord.ui.TextInput(
        label="Starting Weight",
        placeholder="e.g. 200 lbs (auto-filled after first check-in)",
        required=True,
        max_length=50,
    )

    def __init__(self, known_starting_weight: str | None = None) -> None:
        super().__init__()
        if known_starting_weight:
            self.starting_weight.default = known_starting_weight
    proud_of = discord.ui.TextInput(
        label="Proud of 🌟",
        placeholder="Something you accomplished this week",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )
    can_work_on = discord.ui.TextInput(
        label="Can Work On 🎯",
        placeholder="Something to improve next week",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        user = interaction.user
        try:
            sheets.log_checkin(
                user_id=user.id,
                username=str(user),
                current_weight=self.current_weight.value,
                last_week_weight=self.last_week_weight.value,
                starting_weight=self.starting_weight.value,
                proud_of=self.proud_of.value,
                can_work_on=self.can_work_on.value,
            )
        except Exception as e:
            log.error("Failed to save check-in: %s", e)
            await interaction.followup.send(
                "⚠️ There was an error saving your check-in. Please try again.",
                ephemeral=True,
            )
            return

        # Build a formatted embed to post publicly in the channel
        embed = _build_checkin_embed(
            user=user,
            current=self.current_weight.value,
            last_week=self.last_week_weight.value,
            starting=self.starting_weight.value,
            proud_of=self.proud_of.value,
            can_work_on=self.can_work_on.value,
        )

        channel = interaction.guild.get_channel(CHECKIN_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed)

        await interaction.followup.send("✅ Check-in submitted!", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Modal error: %s", error)
        await interaction.response.send_message(
            "Something went wrong. Please try again.", ephemeral=True
        )


# ── Embed builder ──────────────────────────────────────────────────────────────
def _build_checkin_embed(
    user: discord.User | discord.Member,
    current: str,
    last_week: str,
    starting: str,
    proud_of: str,
    can_work_on: str,
) -> discord.Embed:
    # Calculate weight change vs last week (best-effort)
    change_str = ""
    try:
        cur = float("".join(c for c in current if c.isdigit() or c == "."))
        lw  = float("".join(c for c in last_week if c.isdigit() or c == "."))
        diff = cur - lw
        arrow = "📉" if diff < 0 else ("📈" if diff > 0 else "➡️")
        change_str = f"  {arrow} {diff:+.1f}"
    except ValueError:
        pass

    week_str = datetime.now(timezone.utc).strftime("Week of %B %d, %Y")
    embed = discord.Embed(
        title=f"Weekly Check-in — {user.display_name}",
        description=week_str,
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="⚖️ Current Weight", value=f"{current}{change_str}", inline=True)
    embed.add_field(name="📅 Last Week",       value=last_week,               inline=True)
    embed.add_field(name="🚀 Starting Weight", value=starting,                inline=True)
    embed.add_field(name="🌟 Proud of",        value=proud_of,                inline=False)
    embed.add_field(name="🎯 Can Work On",     value=can_work_on,             inline=False)
    embed.set_footer(text="Keep it up! 💪")
    return embed


# ── Bot setup ──────────────────────────────────────────────────────────────────
class FitnessBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        weekly_reminder.start()
        log.info("Commands synced, reminder task started.")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)


bot = FitnessBot()


# ── Slash commands ─────────────────────────────────────────────────────────────
@bot.tree.command(name="checkin", description="Submit your weekly fitness check-in")
async def checkin(interaction: discord.Interaction) -> None:
    # Look up this user's starting weight from their most recent check-in
    try:
        known_starting = sheets.get_starting_weight(interaction.user.id)
    except Exception:
        known_starting = None
    await interaction.response.send_modal(CheckinModal(known_starting_weight=known_starting))


@bot.tree.command(name="summary", description="Show the latest check-ins for the group")
async def summary(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=False)
    try:
        records = sheets.get_latest_checkins(limit=10)
    except Exception as e:
        log.error("Failed to fetch summary: %s", e)
        await interaction.followup.send("⚠️ Could not load check-ins right now.")
        return

    if not records:
        await interaction.followup.send("No check-ins logged yet. Be the first with `/checkin`!")
        return

    embed = discord.Embed(
        title="📊 Latest Check-ins",
        color=discord.Color.blue(),
        description=f"{len(records)} most recent entries",
    )
    for r in records[-5:]:  # show last 5 in the embed
        username = r.get("Username", "Unknown")
        cur  = r.get("Current Weight", "—")
        lw   = r.get("Last Week Weight", "—")
        diff_str = ""
        try:
            diff = float("".join(c for c in cur if c.isdigit() or c == ".")) - \
                   float("".join(c for c in lw  if c.isdigit() or c == "."))
            diff_str = f" ({diff:+.1f})"
        except ValueError:
            pass
        embed.add_field(
            name=username,
            value=(
                f"**Weight:** {cur}{diff_str}\n"
                f"**🌟** {r.get('Proud Of', '—')[:80]}\n"
                f"**🎯** {r.get('Can Work On', '—')[:80]}"
            ),
            inline=False,
        )

    try:
        url = sheets.get_sheet_url()
        embed.add_field(name="📄 Full history", value=f"[Open Google Sheet]({url})", inline=False)
    except Exception:
        pass

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="history", description="Link to the full check-in history spreadsheet")
async def history(interaction: discord.Interaction) -> None:
    try:
        url = sheets.get_sheet_url()
        await interaction.response.send_message(
            f"📄 [Full check-in history in Google Sheets]({url})", ephemeral=True
        )
    except Exception as e:
        log.error("Failed to get sheet URL: %s", e)
        await interaction.response.send_message(
            "⚠️ Could not retrieve the sheet link.", ephemeral=True
        )


# ── Weekly reminder ────────────────────────────────────────────────────────────
@tasks.loop(hours=1)
async def weekly_reminder() -> None:
    """Check once per hour whether it's time to post the weekly prompt."""
    now = datetime.now(timezone.utc)
    if now.weekday() != REMINDER_WEEKDAY:
        return
    if now.hour != REMINDER_HOUR or now.minute > REMINDER_MINUTE + 5:
        return  # only fire in the 5-min window after the target time

    channel = bot.get_channel(CHECKIN_CHANNEL_ID)
    if channel is None:
        log.warning("Reminder: channel %s not found.", CHECKIN_CHANNEL_ID)
        return

    embed = discord.Embed(
        title="🏋️ Weekly Check-in Time!",
        description=(
            "It's time for your weekly fitness check-in! "
            "Use `/checkin` to log your progress.\n\n"
            "**This week, share:**\n"
            "⚖️ Current weight\n"
            "📅 Last week's weight\n"
            "🚀 Starting weight\n"
            "🌟 Something you're proud of\n"
            "🎯 Something to work on"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Consistency is key 💪")
    await channel.send(embed=embed)
    log.info("Weekly reminder posted to channel %s.", CHECKIN_CHANNEL_ID)


@weekly_reminder.before_loop
async def before_reminder() -> None:
    await bot.wait_until_ready()


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

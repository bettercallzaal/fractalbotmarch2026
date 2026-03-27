"""
Event scheduling cog for the ZAO Fractal Discord bot.

Allows admins to create recurring weekly events (e.g. fractal meetings) and
automatically sends @everyone reminders at 24h, 6h, and 1h before each event.

Commands:
    /schedule       -- (admin) Create a recurring weekly event.
    /events         -- List all scheduled events with countdowns.
    /cancel_event   -- (admin) Remove a scheduled event.
    /edit_event     -- (admin) Edit an existing event's schedule.

Background task:
    A once-per-minute loop checks each event and sends reminders at the
    configured intervals, tracking which reminders have already been sent
    to avoid duplicate pings.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cogs.base import BaseCog
from config.config import FRACTAL_BOT_CHANNEL_ID
from utils.safe_json import atomic_save

# ---------------------------------------------------------------------------
# Data file path
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
EVENTS_FILE = os.path.join(DATA_DIR, 'events.json')

# ---------------------------------------------------------------------------
# Day-of-week choices for the slash command dropdown
# ---------------------------------------------------------------------------
DAY_CHOICES = [
    app_commands.Choice(name="Monday", value="monday"),
    app_commands.Choice(name="Tuesday", value="tuesday"),
    app_commands.Choice(name="Wednesday", value="wednesday"),
    app_commands.Choice(name="Thursday", value="thursday"),
    app_commands.Choice(name="Friday", value="friday"),
    app_commands.Choice(name="Saturday", value="saturday"),
    app_commands.Choice(name="Sunday", value="sunday"),
]

# Map day names to Python weekday integers (Monday=0 .. Sunday=6)
DAY_TO_WEEKDAY = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _load_events() -> dict:
    """Load the events data file, returning a default structure if missing."""
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, 'r') as f:
            return json.load(f)
    return {"events": {}}


def _save_events(data: dict):
    """Persist events data atomically."""
    atomic_save(EVENTS_FILE, data)


def _next_occurrence(day: str, time_str: str, tz_name: str) -> datetime:
    """Calculate the next occurrence of a weekly event.

    Returns a timezone-aware datetime in UTC for the next (or current) weekday
    at the specified local time.  If the event time has already passed today,
    the next week's occurrence is returned.
    """
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    target_weekday = DAY_TO_WEEKDAY[day.lower()]
    hour, minute = map(int, time_str.split(":"))

    # Build today's candidate in the event's local timezone
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Calculate days until the target weekday
    days_ahead = (target_weekday - now_local.weekday()) % 7
    candidate += timedelta(days=days_ahead)

    # If the candidate is in the past (same day but time already passed), jump a week
    if candidate <= now_local:
        candidate += timedelta(weeks=1)

    return candidate.astimezone(timezone.utc)


def _format_countdown(td: timedelta) -> str:
    """Format a timedelta as a human-readable countdown string."""
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        return "now"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


class EventsCog(BaseCog):
    """Manages recurring event scheduling and automated reminders."""

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self.data = _load_events()

    async def cog_load(self):
        """Start the reminder loop when the cog is loaded."""
        self.reminder_check.start()

    async def cog_unload(self):
        """Stop the reminder loop when the cog is unloaded."""
        self.reminder_check.cancel()

    # ------------------------------------------------------------------
    # Autocomplete helper -- supplies existing event names
    # ------------------------------------------------------------------
    async def event_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return matching event names for autocomplete fields."""
        names = list(self.data["events"].keys())
        return [
            app_commands.Choice(name=self.data["events"][n]["name"], value=n)
            for n in names
            if current.lower() in n.lower()
        ][:25]

    # ------------------------------------------------------------------
    # /schedule
    # ------------------------------------------------------------------
    @app_commands.command(name="schedule", description="Create a recurring weekly event with automatic reminders")
    @app_commands.describe(
        name="Event name (e.g. Weekly Fractal)",
        day="Day of the week",
        time="Time in 24h format (e.g. 18:00)",
        tz="Timezone (e.g. UTC, America/New_York)",
        channel="Channel to post reminders in (defaults to fractal-bot channel)",
    )
    @app_commands.choices(day=DAY_CHOICES)
    async def schedule_event(
        self,
        interaction: discord.Interaction,
        name: str,
        day: app_commands.Choice[str],
        time: str,
        tz: str = "UTC",
        channel: discord.TextChannel = None,
    ):
        # Admin check
        if not self.is_supreme_admin(interaction.user):
            await interaction.response.send_message(
                "\u274c You need the Supreme Admin role to schedule events.", ephemeral=True
            )
            return

        # Validate time format
        try:
            hour, minute = map(int, time.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            await interaction.response.send_message(
                "\u274c Invalid time format. Use 24h format like `18:00`.", ephemeral=True
            )
            return

        # Validate timezone
        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            await interaction.response.send_message(
                f"\u274c Unknown timezone `{tz}`. Use IANA names like `UTC`, `America/New_York`, `Europe/London`.",
                ephemeral=True,
            )
            return

        # Build a slug key from the name
        key = name.lower().replace(" ", "-")
        if key in self.data["events"]:
            await interaction.response.send_message(
                f"\u274c An event with key `{key}` already exists. Use `/edit_event` or `/cancel_event` first.",
                ephemeral=True,
            )
            return

        channel_id = channel.id if channel else FRACTAL_BOT_CHANNEL_ID

        event = {
            "name": name,
            "day": day.value,
            "time": time,
            "timezone": tz,
            "channel_id": str(channel_id),
            "created_by": str(interaction.user.id),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_reminded_24h": None,
            "last_reminded_6h": None,
            "last_reminded_1h": None,
        }
        self.data["events"][key] = event
        _save_events(self.data)

        next_time = _next_occurrence(day.value, time, tz)
        countdown = _format_countdown(next_time - datetime.now(timezone.utc))

        embed = discord.Embed(
            title="\U0001f4c5 Event Scheduled",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Event", value=name, inline=True)
        embed.add_field(name="Day", value=day.name, inline=True)
        embed.add_field(name="Time", value=f"{time} ({tz})", inline=True)
        embed.add_field(name="Reminders In", value=f"<#{channel_id}>", inline=True)
        embed.add_field(name="Next Occurrence", value=f"<t:{int(next_time.timestamp())}:F>", inline=True)
        embed.add_field(name="Countdown", value=countdown, inline=True)
        embed.set_footer(text=f"Scheduled by {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /events
    # ------------------------------------------------------------------
    @app_commands.command(name="events", description="List all scheduled recurring events")
    async def list_events(self, interaction: discord.Interaction):
        events = self.data.get("events", {})
        if not events:
            await interaction.response.send_message(
                "No events scheduled yet. Use `/schedule` to create one.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="\U0001f4c6 Scheduled Events",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        now = datetime.now(timezone.utc)
        for key, ev in events.items():
            next_time = _next_occurrence(ev["day"], ev["time"], ev["timezone"])
            countdown = _format_countdown(next_time - now)
            embed.add_field(
                name=f"{ev['name']}",
                value=(
                    f"**Day:** {ev['day'].capitalize()}\n"
                    f"**Time:** {ev['time']} ({ev['timezone']})\n"
                    f"**Next:** <t:{int(next_time.timestamp())}:F>\n"
                    f"**Countdown:** {countdown}\n"
                    f"**Channel:** <#{ev['channel_id']}>"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /cancel_event
    # ------------------------------------------------------------------
    @app_commands.command(name="cancel_event", description="Remove a scheduled event")
    @app_commands.describe(name="Name of the event to cancel")
    @app_commands.autocomplete(name=event_name_autocomplete)
    async def cancel_event(self, interaction: discord.Interaction, name: str):
        if not self.is_supreme_admin(interaction.user):
            await interaction.response.send_message(
                "\u274c You need the Supreme Admin role to cancel events.", ephemeral=True
            )
            return

        if name not in self.data["events"]:
            await interaction.response.send_message(
                f"\u274c No event found with key `{name}`.", ephemeral=True
            )
            return

        event_name = self.data["events"][name]["name"]
        del self.data["events"][name]
        _save_events(self.data)

        embed = discord.Embed(
            title="\U0001f5d1\ufe0f Event Cancelled",
            description=f"**{event_name}** has been removed from the schedule.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /edit_event
    # ------------------------------------------------------------------
    @app_commands.command(name="edit_event", description="Edit an existing scheduled event")
    @app_commands.describe(
        name="Name of the event to edit",
        day="New day of the week",
        time="New time in 24h format (e.g. 18:00)",
        tz="New timezone",
        channel="New reminder channel",
    )
    @app_commands.choices(day=DAY_CHOICES)
    @app_commands.autocomplete(name=event_name_autocomplete)
    async def edit_event(
        self,
        interaction: discord.Interaction,
        name: str,
        day: app_commands.Choice[str] = None,
        time: str = None,
        tz: str = None,
        channel: discord.TextChannel = None,
    ):
        if not self.is_supreme_admin(interaction.user):
            await interaction.response.send_message(
                "\u274c You need the Supreme Admin role to edit events.", ephemeral=True
            )
            return

        if name not in self.data["events"]:
            await interaction.response.send_message(
                f"\u274c No event found with key `{name}`.", ephemeral=True
            )
            return

        ev = self.data["events"][name]
        updated = []

        if day is not None:
            ev["day"] = day.value
            updated.append(f"Day -> {day.name}")

        if time is not None:
            try:
                hour, minute = map(int, time.split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except (ValueError, AttributeError):
                await interaction.response.send_message(
                    "\u274c Invalid time format. Use 24h format like `18:00`.", ephemeral=True
                )
                return
            ev["time"] = time
            updated.append(f"Time -> {time}")

        if tz is not None:
            try:
                ZoneInfo(tz)
            except (KeyError, Exception):
                await interaction.response.send_message(
                    f"\u274c Unknown timezone `{tz}`.", ephemeral=True
                )
                return
            ev["timezone"] = tz
            updated.append(f"Timezone -> {tz}")

        if channel is not None:
            ev["channel_id"] = str(channel.id)
            updated.append(f"Channel -> #{channel.name}")

        if not updated:
            await interaction.response.send_message(
                "\u274c No changes provided. Specify at least one field to update.", ephemeral=True
            )
            return

        # Reset reminder tracking since the schedule changed
        ev["last_reminded_24h"] = None
        ev["last_reminded_6h"] = None
        ev["last_reminded_1h"] = None

        _save_events(self.data)

        next_time = _next_occurrence(ev["day"], ev["time"], ev["timezone"])
        countdown = _format_countdown(next_time - datetime.now(timezone.utc))

        embed = discord.Embed(
            title="\u270f\ufe0f Event Updated",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Event", value=ev["name"], inline=False)
        embed.add_field(name="Changes", value="\n".join(f"\u2022 {c}" for c in updated), inline=False)
        embed.add_field(name="Next Occurrence", value=f"<t:{int(next_time.timestamp())}:F>", inline=True)
        embed.add_field(name="Countdown", value=countdown, inline=True)
        embed.set_footer(text=f"Edited by {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # Background reminder loop
    # ------------------------------------------------------------------
    @tasks.loop(minutes=1)
    async def reminder_check(self):
        """Check all events and send reminders at 24h, 6h, and 1h before."""
        now = datetime.now(timezone.utc)
        changed = False

        for key, ev in self.data.get("events", {}).items():
            next_time = _next_occurrence(ev["day"], ev["time"], ev["timezone"])
            time_until = next_time - now
            total_seconds = time_until.total_seconds()

            channel = self.bot.get_channel(int(ev["channel_id"]))
            if channel is None:
                self.logger.warning(f"[EVENTS] Cannot find channel {ev['channel_id']} for event {key}")
                continue

            # Unique identifier for this specific occurrence (the target timestamp)
            occurrence_key = next_time.isoformat()

            # Define reminder thresholds: (field, threshold_seconds, label, color)
            reminders = [
                ("last_reminded_24h", 86400, "tomorrow", discord.Color.gold()),
                ("last_reminded_6h", 21600, "today", discord.Color.orange()),
                ("last_reminded_1h", 3600, "in 1 hour", discord.Color.red()),
            ]

            for field, threshold, label, color in reminders:
                if total_seconds <= threshold and ev.get(field) != occurrence_key:
                    # Send this reminder
                    tz = ZoneInfo(ev["timezone"])
                    local_time = next_time.astimezone(tz)
                    time_display = local_time.strftime("%H:%M %Z")

                    embed = discord.Embed(
                        title=f"\u23f0 {ev['name']} — {label.capitalize()}!",
                        color=color,
                        timestamp=now,
                    )
                    embed.add_field(name="Event", value=ev["name"], inline=True)
                    embed.add_field(name="Day", value=ev["day"].capitalize(), inline=True)
                    embed.add_field(name="Time", value=time_display, inline=True)
                    embed.add_field(
                        name="Countdown",
                        value=_format_countdown(time_until) if total_seconds > 0 else "Starting now!",
                        inline=False,
                    )

                    await channel.send(
                        content=f"@everyone Fractal meeting {label} at **{time_display}**!",
                        embed=embed,
                    )
                    self.logger.info(f"[EVENTS] Sent {field} reminder for {key} (next: {occurrence_key})")

                    ev[field] = occurrence_key
                    changed = True

        if changed:
            _save_events(self.data)

    @reminder_check.before_loop
    async def before_reminder_check(self):
        """Wait until the bot is ready before starting the reminder loop."""
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    """discord.py extension entry point -- registers EventsCog with the bot."""
    await bot.add_cog(EventsCog(bot))

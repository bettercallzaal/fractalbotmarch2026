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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cogs.base import BaseCog
from config.config import FRACTAL_BOT_CHANNEL_ID
from utils.supabase_client import get_supabase

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
        self.supabase = get_supabase()

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
        result = self.supabase.table("discord_events").select("slug, name").execute()
        rows = result.data or []
        return [
            app_commands.Choice(name=row["name"], value=row["slug"])
            for row in rows
            if current.lower() in row["slug"].lower()
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
        existing = self.supabase.table("discord_events").select("id").eq("slug", key).execute()
        if existing.data:
            await interaction.response.send_message(
                f"\u274c An event with key `{key}` already exists. Use `/edit_event` or `/cancel_event` first.",
                ephemeral=True,
            )
            return

        channel_id = channel.id if channel else FRACTAL_BOT_CHANNEL_ID

        event = {
            "slug": key,
            "name": name,
            "day_of_week": day.value,
            "event_time": time,
            "timezone": tz,
            "channel_id": str(channel_id),
            "created_by": str(interaction.user.id),
            "last_reminded_24h": None,
            "last_reminded_6h": None,
            "last_reminded_1h": None,
        }
        self.supabase.table("discord_events").insert(event).execute()

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
        result = self.supabase.table("discord_events").select("*").execute()
        rows = result.data or []
        if not rows:
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
        for ev in rows:
            next_time = _next_occurrence(ev["day_of_week"], ev["event_time"], ev["timezone"])
            countdown = _format_countdown(next_time - now)
            embed.add_field(
                name=f"{ev['name']}",
                value=(
                    f"**Day:** {ev['day_of_week'].capitalize()}\n"
                    f"**Time:** {ev['event_time']} ({ev['timezone']})\n"
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

        result = self.supabase.table("discord_events").select("name").eq("slug", name).execute()
        if not result.data:
            await interaction.response.send_message(
                f"\u274c No event found with key `{name}`.", ephemeral=True
            )
            return

        event_name = result.data[0]["name"]
        self.supabase.table("discord_events").delete().eq("slug", name).execute()

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

        result = self.supabase.table("discord_events").select("*").eq("slug", name).execute()
        if not result.data:
            await interaction.response.send_message(
                f"\u274c No event found with key `{name}`.", ephemeral=True
            )
            return

        ev = result.data[0]
        updates = {}
        updated = []

        if day is not None:
            updates["day_of_week"] = day.value
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
            updates["event_time"] = time
            updated.append(f"Time -> {time}")

        if tz is not None:
            try:
                ZoneInfo(tz)
            except (KeyError, Exception):
                await interaction.response.send_message(
                    f"\u274c Unknown timezone `{tz}`.", ephemeral=True
                )
                return
            updates["timezone"] = tz
            updated.append(f"Timezone -> {tz}")

        if channel is not None:
            updates["channel_id"] = str(channel.id)
            updated.append(f"Channel -> #{channel.name}")

        if not updated:
            await interaction.response.send_message(
                "\u274c No changes provided. Specify at least one field to update.", ephemeral=True
            )
            return

        # Reset reminder tracking since the schedule changed
        updates["last_reminded_24h"] = None
        updates["last_reminded_6h"] = None
        updates["last_reminded_1h"] = None

        self.supabase.table("discord_events").update(updates).eq("slug", name).execute()

        # Merge updates into ev for the response embed
        ev.update(updates)

        next_time = _next_occurrence(ev["day_of_week"], ev["event_time"], ev["timezone"])
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

        result = self.supabase.table("discord_events").select("*").execute()
        rows = result.data or []

        for ev in rows:
            next_time = _next_occurrence(ev["day_of_week"], ev["event_time"], ev["timezone"])
            time_until = next_time - now
            total_seconds = time_until.total_seconds()

            channel = self.bot.get_channel(int(ev["channel_id"]))
            if channel is None:
                self.logger.warning(f"[EVENTS] Cannot find channel {ev['channel_id']} for event {ev['slug']}")
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
                    embed.add_field(name="Day", value=ev["day_of_week"].capitalize(), inline=True)
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
                    self.logger.info(f"[EVENTS] Sent {field} reminder for {ev['slug']} (next: {occurrence_key})")

                    # Update just this reminder field in Supabase
                    self.supabase.table("discord_events").update(
                        {field: occurrence_key}
                    ).eq("slug", ev["slug"]).execute()

    @reminder_check.before_loop
    async def before_reminder_check(self):
        """Wait until the bot is ready before starting the reminder loop."""
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    """discord.py extension entry point -- registers EventsCog with the bot."""
    await bot.add_cog(EventsCog(bot))

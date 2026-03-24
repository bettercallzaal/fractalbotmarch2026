"""Presentation timer system for ZAO Fractal meetings.

This module implements a timed speaking queue used during fractal group
presentations. Before a fractal group votes on contributions, each member
presents their work. The timer enforces equal speaking time, provides
countdown warnings, and offers interactive controls (reactions, hand-raising,
skip/defer) via Discord UI components.

Architecture:
    - PresentationTimer: Core state machine that tracks the speaker queue,
      countdown, reactions, and raised hands. One instance per active session.
    - TimerControlView: Discord UI view (buttons + select menu) that delegates
      user interactions back to the PresentationTimer instance.
    - TimerCog: The discord.py cog that registers slash commands (/timer,
      /timer_add) and manages active timer instances per channel.

Flow:
    1. A user in a voice channel invokes /timer.
    2. The cog pulls voice channel members, optionally shuffles them, and
       creates a PresentationTimer.
    3. An intro preview embed is sent (pulled from the IntroCog cache),
       followed by the live timer embed with interactive buttons.
    4. The countdown loop sleeps efficiently (jumping between warning
       milestones rather than polling every second) and auto-advances
       speakers when time expires.
    5. When all speakers finish, the timer announces readiness for voting.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import asyncio
import time
from cogs.base import BaseCog
from config.config import INTROS_CHANNEL_ID


class PresentationTimer:
    """Core state machine for a single presentation timer session.

    Manages an ordered queue of speakers, each given a fixed time window.
    Speakers can be skipped, deferred (come back later), or reordered
    mid-session. The timer edits a single Discord embed message in-place
    to reflect the current state, avoiding message spam.

    Attributes:
        channel: The text channel where the timer embed lives.
        speakers: Ordered list of members who will present.
        minutes: Duration each speaker gets.
        facilitator: The member who started the timer.
        current_index: Position in the speakers list (0-based).
        paused: Whether the countdown is currently paused.
        stopped: Whether the session was manually stopped.
        message: The live-updating Discord embed message.
        skipped: Members who deferred their turn to present later.
        reactions: Stackable emoji counters (audience feedback).
        raised_hands: Members who signalled they have a question.
    """

    def __init__(self, channel: discord.abc.Messageable, speakers: list[discord.Member],
                 minutes: int, facilitator: discord.Member):
        self.channel = channel
        self.speakers = speakers
        self.minutes = minutes
        self.facilitator = facilitator
        self.current_index = 0
        self.paused = False
        self.stopped = False
        self.message: discord.Message | None = None
        # Unix timestamp when the current speaker's time expires
        self.end_timestamp: int = 0
        self.skipped: list[discord.Member] = []  # Members deferred to come back later
        self._countdown_task: asyncio.Task | None = None  # Track active countdown
        self.logger = logging.getLogger('bot')

        # Interactive reactions — unlike Discord's native reactions, these are
        # simple counters that stack: every button click increments the count,
        # so one user can contribute multiple times.
        self.reactions: dict[str, int] = {
            "\U0001f525": 0,   # fire
            "\U0001f44f": 0,   # clap
            "\U0001f4a1": 0,   # lightbulb
            "\u2753": 0,       # question mark
        }
        self.raised_hands: list[discord.Member] = []
        # Warning flags prevent duplicate warning messages within a single turn
        self._warned_60 = False
        self._warned_30 = False
        self._in_overtime = False
        self.overtime_seconds = 45  # Grace period for picking next speaker

    @property
    def current_speaker(self) -> discord.Member | None:
        """Return the member currently presenting, or None if index is out of bounds."""
        if 0 <= self.current_index < len(self.speakers):
            return self.speakers[self.current_index]
        return None

    @property
    def is_done(self) -> bool:
        """True when all speakers (including deferred ones) have finished, or timer was stopped."""
        return (self.current_index >= len(self.speakers) and not self.skipped) or self.stopped

    def _reset_round_state(self):
        """Reset per-speaker state when advancing to the next speaker.

        Clears reaction counts, raised hands, and warning flags so each
        speaker starts with a clean slate.
        """
        for key in self.reactions:
            self.reactions[key] = 0
        self.raised_hands = []
        self._warned_60 = False
        self._warned_30 = False
        self._in_overtime = False

    def _reaction_bar(self) -> str:
        """Build a compact string summarizing current reaction counts.

        Returns something like "fire 3  clap 5" or "No reactions yet" if all zero.
        """
        parts = []
        for emoji, count in self.reactions.items():
            if count > 0:
                parts.append(f"{emoji} {count}")
        return "  ".join(parts) if parts else "No reactions yet"

    def _build_embed(self, status: str = "speaking") -> discord.Embed:
        """Construct the Discord embed reflecting current timer state.

        Args:
            status: One of "speaking", "paused", "warning", or "done".
                    Controls the embed's title, color, and displayed fields.

        Returns:
            A discord.Embed ready to be sent or used in a message edit.
        """
        speaker = self.current_speaker

        # Terminal state: all speakers finished
        if status == "done":
            embed = discord.Embed(
                title="\u2705 Presentations Complete",
                description="All members have presented. Ready to start voting!",
                color=0x57F287
            )
            lines = []
            for i, s in enumerate(self.speakers):
                lines.append(f"\u2705 {s.mention}")
            embed.add_field(name="Speakers", value="\n".join(lines), inline=False)
            embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")
            return embed

        if status == "overtime":
            remaining = max(0, self.end_timestamp - int(time.time()))
            mins, secs = divmod(remaining, 60)
            embed = discord.Embed(
                title=f"\u23f0 OVERTIME — Pick Next Speaker!",
                description=f"{speaker.mention}'s time is up! Use the dropdown or buttons to pick who goes next.",
                color=0xED4245  # Red — urgency
            )
            embed.add_field(
                name="Overtime",
                value=f"**{mins}:{secs:02d}** remaining to pick",
                inline=True
            )
        elif status == "paused":
            embed = discord.Embed(
                title=f"\u23f8\ufe0f Presentations Paused",
                description=f"Timer paused during {speaker.mention}'s turn.",
                color=0xFEE75C  # Yellow
            )
        elif status == "warning":
            remaining = max(0, self.end_timestamp - int(time.time()))
            mins, secs = divmod(remaining, 60)
            embed = discord.Embed(
                title=f"\u23f0 {speaker.display_name} - Time Running Out!",
                description=f"{speaker.mention} has the floor.",
                color=0xED4245  # Red — urgency color
            )
            embed.add_field(
                name="Time Remaining",
                value=f"**{mins}:{secs:02d}**  (ends <t:{self.end_timestamp}:R>)",
                inline=True
            )
        else:
            # Normal "speaking" state
            remaining = max(0, self.end_timestamp - int(time.time()))
            mins, secs = divmod(remaining, 60)
            embed = discord.Embed(
                title=f"\U0001f399\ufe0f Now Presenting: {speaker.display_name}",
                description=f"{speaker.mention}, you're up! You have **{self.minutes} minutes**.",
                color=0x5865F2  # Blue — default brand color
            )
            embed.add_field(
                name="Time Remaining",
                value=f"**{mins}:{secs:02d}**  (ends <t:{self.end_timestamp}:R>)",
                inline=True
            )

        # Speaker progress indicator (includes deferred speakers in total count)
        embed.add_field(
            name="Speaker",
            value=f"{self.current_index + 1} of {len(self.speakers) + len(self.skipped)}",
            inline=True
        )
        embed.add_field(
            name="Duration",
            value=f"{self.minutes} min each",
            inline=True
        )

        # Live reactions bar
        embed.add_field(
            name="Reactions",
            value=self._reaction_bar(),
            inline=False
        )

        # Raised hands — shown only when someone has raised their hand
        if self.raised_hands:
            hand_names = ", ".join(f"**{m.display_name}**" for m in self.raised_hands)
            embed.add_field(
                name="\u270b Raised Hands",
                value=hand_names,
                inline=False
            )

        # Speaker queue with visual status indicators:
        #   checkmark = already presented, speech bubble = current, hourglass = upcoming,
        #   arrows = deferred (will come back)
        queue_lines = []
        for i, s in enumerate(self.speakers):
            if i < self.current_index:
                queue_lines.append(f"\u2705 ~~{s.display_name}~~")
            elif i == self.current_index:
                queue_lines.append(f"\U0001f4ac **{s.display_name}** \u2190 now")
            else:
                queue_lines.append(f"\u23f3 {s.display_name}")
        if self.skipped:
            for s in self.skipped:
                queue_lines.append(f"\U0001f504 {s.display_name} (come back)")
        embed.add_field(name="Queue", value="\n".join(queue_lines), inline=False)

        embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")
        return embed

    async def _update_message(self, status: str = "speaking", content: str = None):
        """Edit the live timer message with the current state.

        If the original message was deleted (NotFound), re-sends a new one
        to keep the timer visible.
        """
        embed = self._build_embed(status)
        view = TimerControlView(self)
        if self.message:
            try:
                await self.message.edit(embed=embed, view=view, content=content)
            except discord.NotFound:
                # Message was deleted — recover by sending a fresh one
                self.message = await self.channel.send(
                    content=content, embed=embed, view=view
                )

    def _start_countdown(self):
        """Cancel any existing countdown task and start a fresh one.

        This prevents overlapping countdowns when resuming or advancing
        speakers — each speaker gets exactly one active countdown task.
        """
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()
        self._countdown_task = asyncio.create_task(self._countdown())

    async def start(self):
        """Begin the presentation session by sending the initial timer embed.

        This is called once when /timer is invoked. It sends the first
        embed message and kicks off the countdown for the first speaker.
        """
        self.end_timestamp = int(time.time()) + (self.minutes * 60)
        embed = self._build_embed("speaking")
        view = TimerControlView(self)
        self.message = await self.channel.send(embed=embed, view=view)
        self._start_countdown()

    async def _countdown(self):
        """Background loop that waits for timer expiry and posts warnings.

        Updates the embed every 10 seconds for a live countdown on the left side.
        When time expires, enters a 45-second overtime period for picking the
        next speaker before auto-advancing.
        """
        while not self.is_done:
            remaining = self.end_timestamp - int(time.time())

            # While paused, just idle — end_timestamp is stale but will be
            # recalculated on resume()
            if self.paused:
                await asyncio.sleep(1)
                continue

            # Overtime expired — auto-advance
            if remaining <= 0 and self._in_overtime:
                await self.advance()
                return

            # Main time expired — enter overtime
            if remaining <= 0 and not self._in_overtime:
                self._in_overtime = True
                self.end_timestamp = int(time.time()) + self.overtime_seconds
                await self._update_message("overtime")
                warn = await self.channel.send(
                    f"\u23f0 **{self.current_speaker.mention}'s time is up!** "
                    f"You have **{self.overtime_seconds} seconds** to pick who goes next!"
                )
                asyncio.create_task(self._delete_after(warn, 15))
                # Tight loop during overtime for live updates
                while not self.is_done:
                    ot_remaining = self.end_timestamp - int(time.time())
                    if ot_remaining <= 0:
                        await self.advance()
                        return
                    await self._update_message("overtime")
                    await asyncio.sleep(5)
                return

            # Time warnings — update the embed color/title and send ephemeral alerts
            if remaining <= 60 and not self._warned_60:
                self._warned_60 = True
                await self._update_message("warning")
                warn = await self.channel.send(
                    f"\u23f0 {self.current_speaker.mention}, **1 minute remaining!**"
                )
                # Auto-delete warning to reduce clutter
                asyncio.create_task(self._delete_after(warn, 10))

            if remaining <= 30 and not self._warned_30:
                self._warned_30 = True
                await self._update_message("warning")
                warn = await self.channel.send(
                    f"\U0001f6a8 {self.current_speaker.mention}, **30 seconds!** Wrap it up!"
                )
                asyncio.create_task(self._delete_after(warn, 10))

            # Update embed every 10 seconds for live countdown on the left side
            status = "warning" if self._warned_60 and remaining <= 60 else "speaking"
            await self._update_message(status)

            # Sleep in shorter intervals for live updates, but still use
            # milestones to avoid unnecessary work at the start
            if remaining > 70:
                await asyncio.sleep(min(remaining - 60, 10))
            elif remaining > 35:
                await asyncio.sleep(min(remaining - 30, 10))
            else:
                await asyncio.sleep(min(remaining, 5))

    async def _delete_after(self, message: discord.Message, seconds: int):
        """Delete a transient notification message after a delay.

        Silently ignores NotFound errors (message already deleted).
        """
        await asyncio.sleep(seconds)
        try:
            await message.delete()
        except discord.NotFound:
            pass

    async def add_reaction(self, emoji: str, user_id: int):
        """Increment a reaction counter and refresh the embed.

        Unlike Discord's native reactions, these stack: every click adds +1,
        allowing users to express enthusiasm by clicking multiple times.
        """
        if emoji in self.reactions:
            self.reactions[emoji] += 1
            # Preserve the warning-state embed color if we're in the final minute
            remaining = self.end_timestamp - int(time.time())
            status = "warning" if self._warned_60 and remaining <= 60 else "speaking"
            await self._update_message(status)

    async def raise_hand(self, member: discord.Member):
        """Toggle hand-raise for a member and notify the current speaker.

        Raising a hand adds the member to the list and sends a brief
        notification. Clicking again lowers the hand (removes from list).
        """
        if member in self.raised_hands:
            self.raised_hands.remove(member)
            await self._update_message()
        else:
            self.raised_hands.append(member)
            await self._update_message()
            # Notify the speaker so they see it even if not watching the embed
            notify = await self.channel.send(
                f"\u270b **{member.display_name}** has a question for {self.current_speaker.mention}!"
            )
            asyncio.create_task(self._delete_after(notify, 8))

    async def im_done(self, member: discord.Member):
        """Allow the current speaker to end their turn early.

        Returns False if someone other than the current speaker tries to
        use this, allowing the caller to show an error. Returns True on
        success, None if the timer is already done.
        """
        if self.is_done:
            return
        if member.id != self.current_speaker.id:
            return False
        done_msg = await self.channel.send(
            f"\u2705 **{member.display_name}** finished early! Moving on..."
        )
        asyncio.create_task(self._delete_after(done_msg, 5))
        await self.advance()
        return True

    async def add_time(self, minutes: int = 1):
        """Extend the current speaker's remaining time.

        Also resets warning flags if the new remaining time is above the
        warning thresholds, so warnings re-trigger at the correct moment.
        """
        self.end_timestamp += minutes * 60
        # Reset warning flags if added time pushed us back above thresholds,
        # so the warnings fire again at the correct remaining time
        remaining = self.end_timestamp - int(time.time())
        if remaining > 60:
            self._warned_60 = False
            self._warned_30 = False
        elif remaining > 30:
            self._warned_30 = False
        await self._update_message()

    async def skip_come_back(self):
        """Defer the current speaker to present later.

        Moves them to the skipped list. They'll be re-inserted into the
        speakers list after all originally-queued speakers finish.
        """
        if self.is_done:
            return
        speaker = self.current_speaker
        if speaker:
            self.skipped.append(speaker)
        await self.advance()

    async def pick_next(self, member: discord.Member):
        """Reorder the queue so a specific member presents next.

        Handles two cases:
        - Member is in the skipped list: pull them out and insert at next position.
        - Member is in the upcoming queue: move them to the next position.
        In both cases, immediately advances to start their turn.
        """
        if self.is_done:
            return

        # Check if member is in the skipped (deferred) list first
        if member in self.skipped:
            self.skipped.remove(member)
            # Insert right after current speaker so advance() picks them up
            self.speakers.insert(self.current_index + 1, member)
            await self.advance()
            return

        # Check if member is in the upcoming (not yet presented) speakers
        upcoming_indices = [i for i, s in enumerate(self.speakers) if i > self.current_index and s.id == member.id]
        if upcoming_indices:
            idx = upcoming_indices[0]
            speaker = self.speakers.pop(idx)
            self.speakers.insert(self.current_index + 1, speaker)
            await self.advance()

    async def advance(self):
        """Move to the next speaker in the queue.

        When the main queue is exhausted, pulls deferred speakers back in.
        When everyone has presented, posts a completion message and stops.
        """
        if self.stopped:
            return

        self._reset_round_state()
        self.current_index += 1

        # If we've gone past the end of the speakers list, check for deferred speakers
        if self.current_index >= len(self.speakers):
            if self.skipped:
                # Re-append the first deferred speaker so the index is valid again
                comeback = self.skipped.pop(0)
                self.speakers.append(comeback)
            else:
                # All speakers done — show completion embed and stop
                embed = self._build_embed("done")
                view = discord.ui.View()  # Empty view removes all buttons
                if self.message:
                    try:
                        await self.message.edit(embed=embed, view=view, content=None)
                    except discord.NotFound:
                        pass
                await self.channel.send("\u2705 **All presentations complete!** Ready to begin voting.")
                self.stopped = True
                return

        # Start next speaker's timer
        self.end_timestamp = int(time.time()) + (self.minutes * 60)
        self.paused = False

        # Ping the next speaker with a content message (appears as notification)
        content = f"\U0001f399\ufe0f {self.current_speaker.mention}, you're up! You have **{self.minutes} minutes**."
        await self._update_message("speaking", content)

        self._start_countdown()

    async def pause(self):
        """Pause the countdown, freezing the remaining time.

        Captures how much time was left so resume() can set a fresh
        end_timestamp relative to the current moment.
        """
        if self.paused or self.is_done:
            return
        self.paused = True
        # Store remaining seconds so we can restore them on resume
        self._remaining_when_paused = max(0, self.end_timestamp - int(time.time()))
        await self._update_message("paused")

    async def resume(self):
        """Resume a paused countdown from where it left off.

        Recalculates end_timestamp based on the time that was remaining
        when pause() was called, then restarts the countdown task.
        """
        if not self.paused or self.is_done:
            return
        self.paused = False
        # Set a fresh deadline based on stored remaining time
        self.end_timestamp = int(time.time()) + self._remaining_when_paused
        await self._update_message("speaking",
            content=f"\U0001f399\ufe0f {self.current_speaker.mention} has the floor.")
        self._start_countdown()

    async def skip(self):
        """Skip the current speaker without deferring them (they don't come back)."""
        await self.advance()

    async def stop(self):
        """Terminate the entire timer session immediately.

        Cancels the countdown task and replaces the embed with a
        stopped-state summary showing how many speakers presented.
        """
        self.stopped = True
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()
        embed = discord.Embed(
            title="\u23f9\ufe0f Presentations Stopped",
            description=f"Timer stopped. {self.current_index} of {len(self.speakers)} speakers presented.",
            color=0xED4245
        )
        embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")
        if self.message:
            try:
                await self.message.edit(embed=embed, view=None, content=None)
            except discord.NotFound:
                pass


class TimerControlView(discord.ui.View):
    """Interactive button/select UI for controlling the presentation timer.

    Any participant can use these controls (not just the facilitator).
    Discord limits views to 5 rows of components, laid out as:

        Row 0: I'm Done | Skip | Skip & Come Back | +1 Min | Raise Hand
        Row 1: Pause/Resume | Stop | Fire | Clap | Big Brain
        Row 2: Pick who goes next... (select menu, built dynamically)

    The Pause and Resume buttons are mutually exclusive — only one is
    shown depending on the timer's paused state.
    """

    def __init__(self, timer: PresentationTimer):
        # timeout=None keeps the view alive indefinitely (outlives the bot's
        # default 180-second view timeout)
        super().__init__(timeout=None)
        self.timer = timer

        # Show only the relevant button: Pause when running, Resume when paused
        if timer.paused:
            self.remove_item(self.pause_btn)
        else:
            self.remove_item(self.resume_btn)

        # Dynamically build a select menu with upcoming + deferred speakers.
        # This lets the facilitator reorder who goes next on the fly.
        upcoming = [s for i, s in enumerate(timer.speakers) if i > timer.current_index]
        pickable = upcoming + timer.skipped
        if pickable:
            options = []
            seen = set()  # Deduplicate in case a member appears in both lists
            for s in pickable:
                if s.id not in seen:
                    label = s.display_name
                    if s in timer.skipped:
                        label += " (skipped)"
                    options.append(discord.SelectOption(label=label, value=str(s.id)))
                    seen.add(s.id)
            if options:
                select = discord.ui.Select(
                    placeholder="Pick who goes next...",
                    options=options[:25],  # Discord caps select menus at 25 options
                    custom_id="pick_next_select",
                    row=2
                )
                select.callback = self._pick_next_callback
                self.add_item(select)

    async def _pick_next_callback(self, interaction: discord.Interaction):
        """Handle selection from the 'pick next' dropdown menu."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        member_id = int(interaction.data['values'][0])
        # Resolve the member object from either the speakers or skipped list
        member = None
        for s in self.timer.speakers + self.timer.skipped:
            if s.id == member_id:
                member = s
                break
        if member:
            await self.timer.pick_next(member)

    # --- Row 0: Speaker controls ---

    @discord.ui.button(label="I'm Done", style=discord.ButtonStyle.success, emoji="\u2705", row=0)
    async def im_done_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Let the current speaker voluntarily end their turn early."""
        result = await self.timer.im_done(interaction.user)
        if result is False:
            # Only the active speaker can use this button
            try:
                await interaction.response.send_message(
                    "Only the current speaker can end their turn.", ephemeral=True
                )
            except (discord.NotFound, discord.HTTPException):
                pass
        else:
            try:
                await interaction.response.defer()
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, emoji="\u23ed\ufe0f", row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skip the current speaker permanently (they don't come back)."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.skip()

    @discord.ui.button(label="Come Back", style=discord.ButtonStyle.secondary, emoji="\U0001f504", row=0)
    async def skip_come_back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Defer the current speaker to present after everyone else."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.skip_come_back()

    @discord.ui.button(label="+1 Min", style=discord.ButtonStyle.secondary, emoji="\u23f0", row=0)
    async def add_time_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Add one extra minute to the current speaker's time."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.add_time(1)

    @discord.ui.button(label="Hand", style=discord.ButtonStyle.primary, emoji="\u270b", row=0)
    async def raise_hand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggle hand-raise to signal a question for the speaker."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.raise_hand(interaction.user)

    # --- Row 1: Timer controls + audience reactions ---

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="\u23f8\ufe0f", row=1)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pause the countdown timer."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.pause()

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.success, emoji="\u25b6\ufe0f", row=1)
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Resume the countdown timer from where it was paused."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.resume()

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="\u23f9\ufe0f", row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Stop the entire presentation session."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.stop()

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="\U0001f525", row=1)
    async def react_fire(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Add a fire reaction to the current speaker's tally."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.add_reaction("\U0001f525", interaction.user.id)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="\U0001f44f", row=1)
    async def react_clap(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Add a clap reaction to the current speaker's tally."""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.add_reaction("\U0001f44f", interaction.user.id)


class TimerCog(BaseCog):
    """Discord cog that registers slash commands for the presentation timer.

    Manages active timer instances per channel. Only one timer can run in
    a channel at a time. The /timer command also integrates with IntroCog
    to display member introductions before presentations begin.
    """

    def __init__(self, bot):
        super().__init__(bot)
        # Map of channel_id -> PresentationTimer for active sessions
        self.active_timers = {}

    @app_commands.command(
        name="timer",
        description="Start a presentation timer for everyone in your voice channel"
    )
    @app_commands.describe(
        minutes="Minutes per speaker (default: 4)",
        shuffle="Randomize speaker order (default: no)"
    )
    async def timer(self, interaction: discord.Interaction, minutes: int = 4,
                    shuffle: bool = False):
        """Start a presentation timer for all members in the invoker's voice channel.

        Validates the user is in a voice channel, checks no timer is already
        running, then builds an intro preview embed and starts the timer.
        Uses a defer pattern: fast validation is done with direct responses,
        slow work (intro lookup, embed building) happens after deferring.
        """
        self.logger.info(f"[TIMER] handler called: interaction={interaction.id} user={interaction.user}")
        if self.is_duplicate_interaction(interaction):
            self.logger.warning(f"[TIMER] BLOCKED by BaseCog dedup: {interaction.id}")
            return

        # --- Fast validation (direct response, no defer needed) ---
        voice_check = await self.check_voice_state(interaction.user)
        if not voice_check['success']:
            await interaction.response.send_message(voice_check['message'], ephemeral=True)
            return

        channel = interaction.channel
        if channel.id in self.active_timers and not self.active_timers[channel.id].is_done:
            await interaction.response.send_message(
                "A timer is already running in this channel. Stop it first with the Stop button.",
                ephemeral=True
            )
            return

        if minutes < 1 or minutes > 30:
            await interaction.response.send_message(
                "Timer must be between 1 and 30 minutes.", ephemeral=True
            )
            return

        # --- Slow path: defer first, then do potentially slow I/O ---
        await interaction.response.defer(ephemeral=True)

        members = voice_check['members']
        if shuffle:
            import random
            random.shuffle(members)

        timer = PresentationTimer(
            channel=channel,
            speakers=members,
            minutes=minutes,
            facilitator=interaction.user
        )
        self.active_timers[channel.id] = timer

        # Fetch and display intro previews from IntroCog's cache.
        # This gives participants context about each speaker before presentations.
        intro_cog = self.bot.get_cog('IntroCog')
        if intro_cog:
            try:
                intro_cache = intro_cog.intro_cache
                intro_lines = []
                no_intro = []
                guild_id = interaction.guild_id
                for member in members:
                    intro_data = intro_cache.get(member.id)
                    if not intro_data:
                        # Cache miss — fall back to scanning the intros channel history.
                        # This is slow but only happens once per member; subsequent
                        # lookups will hit the cache.
                        intros_channel = self.bot.get_channel(INTROS_CHANNEL_ID)
                        if intros_channel:
                            async for message in intros_channel.history(limit=None, oldest_first=True):
                                if message.author.id == member.id and message.content.strip():
                                    intro_cache.set(
                                        member.id, message.content,
                                        message.id, message.created_at.isoformat()
                                    )
                                    intro_data = intro_cache.get(member.id)
                                    break
                    if not intro_data:
                        no_intro.append(member)
                        continue
                    # Truncate to a 1-2 line preview with a link to the full intro
                    text_lines = intro_data['text'].strip().split('\n')
                    preview = ' '.join(text_lines[:2]).strip()
                    if len(preview) > 150:
                        preview = preview[:147] + "..."
                    msg_id = intro_data.get('message_id')
                    if msg_id:
                        link = f"https://discord.com/channels/{guild_id}/{INTROS_CHANNEL_ID}/{msg_id}"
                        intro_lines.append(f"\u2022 **{member.display_name}** \u2014 {preview} [read more]({link})")
                    else:
                        intro_lines.append(f"\u2022 **{member.display_name}** \u2014 {preview}")

                # Send intros as a public embed before the timer starts
                if intro_lines or no_intro:
                    intro_embed = discord.Embed(
                        title="\U0001f44b Meet Your Group",
                        color=0x5865F2
                    )
                    if intro_lines:
                        intro_embed.description = "\n\n".join(intro_lines)
                    if no_intro:
                        mentions = " ".join(m.mention for m in no_intro)
                        intro_embed.add_field(
                            name="\U0001f4ad Missing Intros",
                            value=f"{mentions}\nPost your intro in <#{INTROS_CHANNEL_ID}> so your group can get to know you!",
                            inline=False
                        )
                    intro_embed.set_footer(text="ZAO Fractal \u2022 zao.frapps.xyz")
                    await channel.send(embed=intro_embed)
            except Exception as e:
                self.logger.error(f"Error loading intros: {e}", exc_info=True)

        # Acknowledge the slash command ephemerally, then send the public timer embed
        await interaction.followup.send("\U0001f399\ufe0f Timer started!", ephemeral=True)
        await timer.start()

    @app_commands.command(
        name="timer_add",
        description="Add extra time to the current speaker"
    )
    @app_commands.describe(minutes="Extra minutes to add (default: 1)")
    async def timer_add(self, interaction: discord.Interaction, minutes: int = 1):
        """Add extra minutes to the current speaker's remaining time.

        This is a standalone slash command (separate from the button) so
        facilitators can add arbitrary amounts of time, not just 1 minute.
        """
        await interaction.response.defer(ephemeral=True)

        channel = interaction.channel
        timer = self.active_timers.get(channel.id)

        if not timer or timer.is_done:
            await interaction.followup.send("No active timer in this channel.", ephemeral=True)
            return

        await timer.add_time(minutes)

        await interaction.followup.send(
            f"Added **{minutes} min** to {timer.current_speaker.display_name}'s turn.",
            ephemeral=True
        )


async def setup(bot):
    """Entry point for discord.py's cog loading system."""
    await bot.add_cog(TimerCog(bot))

import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import asyncio
import time
from cogs.base import BaseCog
from config.config import INTROS_CHANNEL_ID


class PresentationTimer:
    """Manages a speaking queue with countdown timer for a channel"""

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
        self.end_timestamp: int = 0
        self.skipped: list[discord.Member] = []  # Members deferred to come back later
        self._countdown_task: asyncio.Task | None = None  # Track active countdown
        self.logger = logging.getLogger('bot')

        # Interactive features - reactions are counters, users can stack
        self.reactions: dict[str, int] = {
            "\U0001f525": 0,   # fire
            "\U0001f44f": 0,   # clap
            "\U0001f4a1": 0,   # lightbulb
            "\u2753": 0,       # question mark
        }
        self.raised_hands: list[discord.Member] = []
        self._warned_60 = False
        self._warned_30 = False

    @property
    def current_speaker(self) -> discord.Member | None:
        if 0 <= self.current_index < len(self.speakers):
            return self.speakers[self.current_index]
        return None

    @property
    def is_done(self) -> bool:
        return (self.current_index >= len(self.speakers) and not self.skipped) or self.stopped

    def _reset_round_state(self):
        """Reset per-speaker state when advancing"""
        for key in self.reactions:
            self.reactions[key] = 0
        self.raised_hands = []
        self._warned_60 = False
        self._warned_30 = False

    def _reaction_bar(self) -> str:
        """Build a compact reaction summary string"""
        parts = []
        for emoji, count in self.reactions.items():
            if count > 0:
                parts.append(f"{emoji} {count}")
        return "  ".join(parts) if parts else "No reactions yet"

    def _build_embed(self, status: str = "speaking") -> discord.Embed:
        speaker = self.current_speaker

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

        if status == "paused":
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
                color=0xED4245  # Red
            )
            embed.add_field(
                name="Time Remaining",
                value=f"**{mins}:{secs:02d}**  (ends <t:{self.end_timestamp}:R>)",
                inline=True
            )
        else:
            remaining = max(0, self.end_timestamp - int(time.time()))
            mins, secs = divmod(remaining, 60)
            embed = discord.Embed(
                title=f"\U0001f399\ufe0f Now Presenting: {speaker.display_name}",
                description=f"{speaker.mention}, you're up! You have **{self.minutes} minutes**.",
                color=0x5865F2  # Blue
            )
            embed.add_field(
                name="Time Remaining",
                value=f"**{mins}:{secs:02d}**  (ends <t:{self.end_timestamp}:R>)",
                inline=True
            )

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

        # Raised hands
        if self.raised_hands:
            hand_names = ", ".join(f"**{m.display_name}**" for m in self.raised_hands)
            embed.add_field(
                name="\u270b Raised Hands",
                value=hand_names,
                inline=False
            )

        # Queue
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
        """Helper to update the timer message with current state"""
        embed = self._build_embed(status)
        view = TimerControlView(self)
        if self.message:
            try:
                await self.message.edit(embed=embed, view=view, content=content)
            except discord.NotFound:
                self.message = await self.channel.send(
                    content=content, embed=embed, view=view
                )

    def _start_countdown(self):
        """Cancel any existing countdown and start a new one"""
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()
        self._countdown_task = asyncio.create_task(self._countdown())

    async def start(self):
        """Start the presentation timer from the first speaker — sends one message"""
        self.end_timestamp = int(time.time()) + (self.minutes * 60)
        embed = self._build_embed("speaking")
        view = TimerControlView(self)
        self.message = await self.channel.send(embed=embed, view=view)
        self._start_countdown()

    async def _countdown(self):
        """Wait for the timer to expire, then advance. Posts time warnings."""
        while not self.is_done:
            remaining = self.end_timestamp - int(time.time())

            if self.paused:
                await asyncio.sleep(1)
                continue

            if remaining <= 0:
                await self.advance()
                return

            # Time warnings — only update embed at these milestones
            if remaining <= 60 and not self._warned_60:
                self._warned_60 = True
                await self._update_message("warning")
                warn = await self.channel.send(
                    f"\u23f0 {self.current_speaker.mention}, **1 minute remaining!**"
                )
                asyncio.create_task(self._delete_after(warn, 10))

            if remaining <= 30 and not self._warned_30:
                self._warned_30 = True
                await self._update_message("warning")
                warn = await self.channel.send(
                    f"\U0001f6a8 {self.current_speaker.mention}, **30 seconds!** Wrap it up!"
                )
                asyncio.create_task(self._delete_after(warn, 10))

            # Sleep until next milestone or expiry — no periodic tick updates
            if remaining > 60:
                await asyncio.sleep(remaining - 60)
            elif remaining > 30:
                await asyncio.sleep(remaining - 30)
            else:
                await asyncio.sleep(min(remaining, 5))

    async def _delete_after(self, message: discord.Message, seconds: int):
        """Delete a message after a delay"""
        await asyncio.sleep(seconds)
        try:
            await message.delete()
        except discord.NotFound:
            pass

    async def add_reaction(self, emoji: str, user_id: int):
        """Stack a reaction - every click adds one more"""
        if emoji in self.reactions:
            self.reactions[emoji] += 1
            remaining = self.end_timestamp - int(time.time())
            status = "warning" if self._warned_60 and remaining <= 60 else "speaking"
            await self._update_message(status)

    async def raise_hand(self, member: discord.Member):
        """Toggle raise hand for a member"""
        if member in self.raised_hands:
            self.raised_hands.remove(member)
            await self._update_message()
        else:
            self.raised_hands.append(member)
            await self._update_message()
            # Notify the speaker
            notify = await self.channel.send(
                f"\u270b **{member.display_name}** has a question for {self.current_speaker.mention}!"
            )
            asyncio.create_task(self._delete_after(notify, 8))

    async def im_done(self, member: discord.Member):
        """Current speaker ends their turn early"""
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
        """Add time to current speaker"""
        self.end_timestamp += minutes * 60
        # Reset warning flags if we went back above thresholds
        remaining = self.end_timestamp - int(time.time())
        if remaining > 60:
            self._warned_60 = False
            self._warned_30 = False
        elif remaining > 30:
            self._warned_30 = False
        await self._update_message()

    async def skip_come_back(self):
        """Skip current speaker and add them to the end to come back later"""
        if self.is_done:
            return
        speaker = self.current_speaker
        if speaker:
            self.skipped.append(speaker)
        await self.advance()

    async def pick_next(self, member: discord.Member):
        """Jump to a specific member next, reordering the queue"""
        if self.is_done:
            return

        # Check if member is in the skipped list
        if member in self.skipped:
            self.skipped.remove(member)
            self.speakers.insert(self.current_index + 1, member)
            await self.advance()
            return

        # Check if member is in the upcoming speakers
        upcoming_indices = [i for i, s in enumerate(self.speakers) if i > self.current_index and s.id == member.id]
        if upcoming_indices:
            idx = upcoming_indices[0]
            speaker = self.speakers.pop(idx)
            self.speakers.insert(self.current_index + 1, speaker)
            await self.advance()

    async def advance(self):
        """Move to the next speaker"""
        if self.stopped:
            return

        self._reset_round_state()
        self.current_index += 1

        if self.current_index >= len(self.speakers):
            if self.skipped:
                comeback = self.skipped.pop(0)
                self.speakers.append(comeback)
            else:
                embed = self._build_embed("done")
                view = discord.ui.View()
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

        content = f"\U0001f399\ufe0f {self.current_speaker.mention}, you're up! You have **{self.minutes} minutes**."
        await self._update_message("speaking", content)

        self._start_countdown()

    async def pause(self):
        """Pause the timer"""
        if self.paused or self.is_done:
            return
        self.paused = True
        self._remaining_when_paused = max(0, self.end_timestamp - int(time.time()))
        await self._update_message("paused")

    async def resume(self):
        """Resume the timer"""
        if not self.paused or self.is_done:
            return
        self.paused = False
        self.end_timestamp = int(time.time()) + self._remaining_when_paused
        await self._update_message("speaking",
            content=f"\U0001f399\ufe0f {self.current_speaker.mention} has the floor.")
        self._start_countdown()

    async def skip(self):
        """Skip to next speaker"""
        await self.advance()

    async def stop(self):
        """Stop the timer entirely"""
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
    """Interactive buttons for controlling the presentation timer - anyone can use.

    Layout (5 rows max in Discord):
      Row 0: I'm Done | Skip | Skip & Come Back | +1 Min | Raise Hand
      Row 1: Pause/Resume | Stop | Fire | Clap | Big Brain
      Row 2: Pick who goes next... (select menu)
    """

    def __init__(self, timer: PresentationTimer):
        super().__init__(timeout=None)
        self.timer = timer

        # Swap pause/resume based on state
        if timer.paused:
            self.remove_item(self.pause_btn)
        else:
            self.remove_item(self.resume_btn)

        # Add "Pick Next" select menu with upcoming speakers + skipped (row 2)
        upcoming = [s for i, s in enumerate(timer.speakers) if i > timer.current_index]
        pickable = upcoming + timer.skipped
        if pickable:
            options = []
            seen = set()
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
                    options=options[:25],
                    custom_id="pick_next_select",
                    row=2
                )
                select.callback = self._pick_next_callback
                self.add_item(select)

    async def _pick_next_callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        member_id = int(interaction.data['values'][0])
        member = None
        for s in self.timer.speakers + self.timer.skipped:
            if s.id == member_id:
                member = s
                break
        if member:
            await self.timer.pick_next(member)

    # Row 0: Speaker controls
    @discord.ui.button(label="I'm Done", style=discord.ButtonStyle.success, emoji="\u2705", row=0)
    async def im_done_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        result = await self.timer.im_done(interaction.user)
        if result is False:
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
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.skip()

    @discord.ui.button(label="Come Back", style=discord.ButtonStyle.secondary, emoji="\U0001f504", row=0)
    async def skip_come_back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.skip_come_back()

    @discord.ui.button(label="+1 Min", style=discord.ButtonStyle.secondary, emoji="\u23f0", row=0)
    async def add_time_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.add_time(1)

    @discord.ui.button(label="Hand", style=discord.ButtonStyle.primary, emoji="\u270b", row=0)
    async def raise_hand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.raise_hand(interaction.user)

    # Row 1: Timer controls + reactions
    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="\u23f8\ufe0f", row=1)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.pause()

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.success, emoji="\u25b6\ufe0f", row=1)
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.resume()

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="\u23f9\ufe0f", row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.stop()

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="\U0001f525", row=1)
    async def react_fire(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.add_reaction("\U0001f525", interaction.user.id)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="\U0001f44f", row=1)
    async def react_clap(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        await self.timer.add_reaction("\U0001f44f", interaction.user.id)


class TimerCog(BaseCog):
    """Cog for managing presentation timers before fractal voting"""

    def __init__(self, bot):
        super().__init__(bot)
        self.active_timers = {}  # channel_id -> PresentationTimer

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
        """Start a presentation timer"""
        self.logger.info(f"[TIMER] handler called: interaction={interaction.id} user={interaction.user}")
        if self.is_duplicate_interaction(interaction):
            self.logger.warning(f"[TIMER] BLOCKED by BaseCog dedup: {interaction.id}")
            return

        # --- Fast validation (direct response, no webhook) ---
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

        # --- Slow path: defer, then do work ---
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

        # Build and send intro previews as a separate public message
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
                    # 1-2 line preview
                    text_lines = intro_data['text'].strip().split('\n')
                    preview = ' '.join(text_lines[:2]).strip()
                    if len(preview) > 150:
                        preview = preview[:147] + "..."
                    # Link to full intro message
                    msg_id = intro_data.get('message_id')
                    if msg_id:
                        link = f"https://discord.com/channels/{guild_id}/{INTROS_CHANNEL_ID}/{msg_id}"
                        intro_lines.append(f"\u2022 **{member.display_name}** \u2014 {preview} [read more]({link})")
                    else:
                        intro_lines.append(f"\u2022 **{member.display_name}** \u2014 {preview}")

                # Send intros as a public embed in the channel
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

        # Send one public timer embed, ack the interaction ephemerally
        await interaction.followup.send("\U0001f399\ufe0f Timer started!", ephemeral=True)
        await timer.start()

    @app_commands.command(
        name="timer_add",
        description="Add extra time to the current speaker"
    )
    @app_commands.describe(minutes="Extra minutes to add (default: 1)")
    async def timer_add(self, interaction: discord.Interaction, minutes: int = 1):
        """Add time to the current speaker"""
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
    await bot.add_cog(TimerCog(bot))

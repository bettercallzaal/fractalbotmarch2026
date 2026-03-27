"""
Fractal group management for the ZAO Fractal voting bot.

This module implements the core fractal democracy process used by ZAO (a DAO).
A "fractal" is a structured group deliberation where members rank each other
across six levels (6 down to 1) through successive voting rounds. Each round,
members vote for who they believe contributed most; the winner of each round
is assigned the current level and removed from the candidate pool. The process
repeats until all levels are filled or only one candidate remains.

Winners earn "Respect" points on a Fibonacci-like scale (highest level = most
Respect). Once all rounds are complete, the final rankings are posted to the
server's general channel and a URL is generated so participants can submit the
results onchain via the ZAO web app (zao.frapps.xyz/submitBreakout).

Key concepts:
    - Level: Ranges from 6 (highest) to 1 (lowest). Voting starts at level 6.
    - Vote threshold: A candidate wins a round when they receive votes from
      at least ceil(total_members / 2) members (simple majority).
    - Facilitator: The member who created the fractal session; has admin powers.
    - Active candidates: Members still eligible to win a level (shrinks each round).
    - Wallet registry: Maps Discord members to Ethereum addresses for onchain submission.
"""

import discord
import logging
import asyncio
import random
import os
from typing import Optional, List, Dict
from urllib.parse import urlencode
from utils.web_integration import web_integration

# Resolve the project-root assets/ directory (three levels up from this file: cogs/fractal/group.py)
ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'assets')
# Default notification sound used as a fallback when a level-specific sound is missing
PING_SOUND = os.path.join(ASSETS_DIR, 'ping.mp3')

# Different sounds per level (ascending pitch as levels get more intense)
LEVEL_SOUNDS = {
    6: os.path.join(ASSETS_DIR, 'level6.mp3'),
    5: os.path.join(ASSETS_DIR, 'level5.mp3'),
    4: os.path.join(ASSETS_DIR, 'level4.mp3'),
    3: os.path.join(ASSETS_DIR, 'level3.mp3'),
    2: os.path.join(ASSETS_DIR, 'level2.mp3'),
    1: os.path.join(ASSETS_DIR, 'level1.mp3'),
}

# How long the bot stays in voice before auto-disconnecting (seconds)
VOICE_STAY_DURATION = 300  # 5 minutes

class FractalGroup:
    """Core class for managing a single fractal voting session.

    A FractalGroup is created when a facilitator starts a new fractal. It owns
    the Discord thread, tracks all members, manages the voting state machine
    (level 6 down to 1), and handles voice-channel notifications.

    Lifecycle:
        1. ``__init__`` -- set up state.
        2. ``start_fractal`` -- post welcome message and begin round 1 (level 6).
        3. ``start_new_round`` / ``process_vote`` / ``check_for_winner`` -- loop
           until all levels are assigned or one candidate remains.
        4. ``end_fractal`` -- post results, generate onchain link, record history.

    The group is removed from ``cog.active_groups`` once ``end_fractal`` completes.
    """

    def __init__(self, thread: discord.Thread, members: List[discord.Member], facilitator: discord.Member, cog):
        """Initialize a new fractal group.

        Args:
            thread: The Discord thread where voting messages and announcements are posted.
            members: All participants in this fractal session. This list is used to
                     calculate the vote threshold and never shrinks during the session.
            facilitator: The member who initiated the fractal; can pause/resume voting.
            cog: Reference to the parent Cog instance, used to access bot-level
                 resources (wallet registry, fractal history, active_groups map).
        """
        self.thread = thread
        self.facilitator = facilitator
        # members is the full roster and stays constant -- used for threshold math
        self.members = members
        # active_candidates is a shrinking copy: winners are removed each round
        self.active_candidates = members.copy()
        # votes maps voter member ID -> candidate member ID for the current round only
        self.votes = {}
        # winners maps level number (6..1) -> the Member who won that level
        self.winners = {}
        self.current_level = 6  # Voting proceeds from level 6 down to 1
        self.current_voting_message = None  # Reference to the latest voting embed message
        self.cog = cog
        self.voice_channel = None  # Set later by FractalNameModal after group creation
        self.logger = logging.getLogger('bot')
        self._voice_disconnect_task = None  # asyncio.Task for the auto-disconnect timer

        self.logger.info(f"Created fractal group '{thread.name}' with facilitator {facilitator.display_name} and {len(members)} members")

    async def start_fractal(self):
        """Start the fractal voting process.

        Sends a welcome message to the thread, notifies the web dashboard,
        and kicks off the first voting round (level 6).
        """
        self.logger.info(f"Starting fractal process for '{self.thread.name}' with {len(self.members)} members")

        # Send welcome message
        welcome_msg = (
            f"# 🎊 **Welcome to {self.thread.name}!** 🎊\n\n"
            f"**Facilitator:** {self.facilitator.mention}\n"
            f"**Members:** {', '.join([m.mention for m in self.members])}\n\n"
            f"🗳️ **Starting fractal voting process...**\n"
            f"We'll vote through levels 6→1 until we have a winner!\n\n"
        )
        await self.thread.send(welcome_msg)

        # Notify web app that fractal started
        await web_integration.notify_fractal_started(self)

        # Start first round
        self.logger.info(f"Starting first round for '{self.thread.name}'")
        await self.start_new_round()

    async def start_new_round(self, winner: Optional[discord.Member] = None):
        """Start a new voting round, optionally recording a previous winner.

        This is the main round-lifecycle method. When called with a winner, it:
        1. Records the winner at the current level.
        2. Removes them from the active candidate pool.
        3. Decrements the level counter (e.g. 6 -> 5).
        4. Checks termination conditions (level < 1 or <= 1 candidate left).
        5. Resets the per-round vote map and posts a new voting UI.

        Args:
            winner: The member who won the previous round, or None for the
                    very first round of the session.
        """
        # Process previous winner if exists
        if winner:
            self.winners[self.current_level] = winner
            self.active_candidates.remove(winner)  # Remove winner from future rounds
            self.current_level -= 1  # Descend to the next level

            # Announce the winner. Note: current_level was already decremented
            # above, so we add 1 back to display the level they actually won.
            await self.thread.send(
                f"🎊 **LEVEL {self.current_level + 1} WINNER: {winner.mention}!** 🎊\n\n"
                f"Moving to Level {self.current_level}..."
            )

        # Termination: no more levels to assign, or only one candidate remains
        # (that last candidate will be assigned the lowest remaining level in end_fractal)
        if self.current_level < 1 or len(self.active_candidates) <= 1:
            await self.end_fractal()
            return

        # Reset votes for new round
        self.votes = {}

        # Log active candidates
        candidate_names = ", ".join([c.display_name for c in self.active_candidates])
        self.logger.info(f"Starting level {self.current_level} with {len(self.active_candidates)} candidates: {candidate_names}")

        try:
            # Import here to avoid circular import
            from .views import ZAOFractalVotingView

            # Create voting view with buttons
            view = ZAOFractalVotingView(self)

            # Create beautiful voting message like the second image
            votes_needed = self.get_vote_threshold()
            candidates_list = ", ".join([c.mention for c in self.active_candidates])

            voting_message = (
                f"🗳️ **Voting for Level {self.current_level}**\n\n"
                f"**Candidates:** {candidates_list}\n"
                f"**Votes Needed to Win:** {votes_needed} ({votes_needed}/{len(self.members)} members)\n\n"
                f"Click a button below to vote. Your vote will be announced publicly.\n"
                f"You can change your vote at any time by clicking a different button."
            )

            message = await self.thread.send(voting_message, view=view)
            self.current_voting_message = message

            # Notify voice channel with link + audio ping
            await self.notify_voice_channel()

        except Exception as e:
            self.logger.error(f"Error creating voting UI: {e}", exc_info=True)
            await self.thread.send("❌ Error setting up voting buttons. Please try again.")

    def get_vote_threshold(self):
        """Calculate votes needed to win (simple majority: ceil(n/2)).

        Uses the full member count (not active candidates) so the bar stays
        consistent across rounds. The formula ``n // 2 + n % 2`` is equivalent
        to ``math.ceil(n / 2)`` but avoids a float conversion.

        Returns:
            int: The minimum number of votes a candidate needs to win the round.
                 Always at least 1 even if the group is empty (defensive).
        """
        return max(1, len(self.members) // 2 + len(self.members) % 2)  # Ceiling division

    async def notify_voice_channel(self):
        """Send a link to the voting thread in the voice channel text chat and play a level-specific sound.

        This method performs two independent actions:
        1. Posts a text message in the voice channel's companion text chat so
           users in voice see a clickable link to the voting thread.
        2. Connects to (or reuses) the voice channel and plays an audio chime.
           Each level has its own sound file (ascending pitch for higher levels);
           if the file is missing, it falls back to the generic ping.mp3.

        After playing the sound, a 5-minute auto-disconnect timer is started
        (or reset if one already exists) so the bot does not linger in voice
        indefinitely.
        """
        if not self.voice_channel:
            return

        # Send thread link to voice channel text chat
        try:
            await self.voice_channel.send(
                f"🗳️ **Level {self.current_level} voting is live!** "
                f"Vote here → {self.thread.mention}"
            )
        except Exception as e:
            self.logger.error(f"Failed to send voice channel notification: {e}")

        # Pick the sound for this level; two-stage fallback:
        # 1. Try the level-specific file (e.g. level4.mp3)
        # 2. Fall back to the generic ping.mp3
        sound_file = LEVEL_SOUNDS.get(self.current_level, PING_SOUND)
        if not os.path.exists(sound_file):
            sound_file = PING_SOUND
        if not os.path.exists(sound_file):
            self.logger.warning(f"No sound file found for level {self.current_level}")
            return

        # Play audio ping in voice channel
        try:
            guild = self.thread.guild
            voice_client = guild.voice_client

            # Clean up a stale voice client that lost its connection
            if voice_client and not voice_client.is_connected():
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    pass
                voice_client = None

            # Connect if not already connected (self_deaf=True since we only play audio, never listen)
            if not voice_client:
                voice_client = await self.voice_channel.connect(
                    timeout=30.0, reconnect=True, self_deaf=True
                )
            elif voice_client.channel != self.voice_channel:
                # Bot is in a different voice channel -- move it
                await voice_client.move_to(self.voice_channel)

            # Brief delay to allow the WebSocket handshake to finish
            await asyncio.sleep(1.0)

            if not voice_client.is_connected():
                self.logger.warning("Voice client not connected after wait")
                return

            # Stop any currently playing audio before starting the new sound
            if voice_client.is_playing():
                voice_client.stop()

            source = discord.FFmpegPCMAudio(sound_file)
            voice_client.play(source)

            # Reset the 5-minute auto-disconnect timer so inactivity is measured
            # from the most recent round notification, not the first one
            self._schedule_voice_disconnect(voice_client)

        except Exception as e:
            self.logger.error(f"Failed to play audio ping: {e}", exc_info=True)
            # Best-effort cleanup: force-disconnect to avoid orphaned voice state
            try:
                vc = guild.voice_client
                if vc:
                    await vc.disconnect(force=True)
            except Exception:
                pass

    def _schedule_voice_disconnect(self, voice_client):
        """Start or restart the idle-disconnect timer for the voice connection.

        Each call cancels any previously running timer so the bot stays
        connected as long as new rounds keep firing within the window.

        Args:
            voice_client: The active discord.VoiceClient to disconnect later.
        """
        # Cancel the prior timer so inactivity is measured from *this* round
        if self._voice_disconnect_task and not self._voice_disconnect_task.done():
            self._voice_disconnect_task.cancel()

        self._voice_disconnect_task = asyncio.create_task(
            self._auto_disconnect(voice_client)
        )

    async def _auto_disconnect(self, voice_client):
        """Sleep for VOICE_STAY_DURATION then leave the voice channel.

        Runs as an asyncio.Task created by ``_schedule_voice_disconnect``.
        If the task is cancelled (because a new round reset the timer),
        the CancelledError is silently swallowed.
        """
        try:
            await asyncio.sleep(VOICE_STAY_DURATION)
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect()
                self.logger.info(f"Auto-disconnected from voice after {VOICE_STAY_DURATION}s")
        except asyncio.CancelledError:
            pass  # Timer was reset by a new round starting
        except Exception:
            pass  # Swallow errors silently; voice disconnect is best-effort

    async def process_vote(self, voter: discord.Member, candidate: discord.Member):
        """Process a vote from a member and announce it publicly.

        Votes are publicly visible (not secret ballot). A member can change
        their vote at any time during the round by clicking a different
        candidate button; the old vote is replaced and a "vote changed"
        message is posted.

        After recording the vote, ``check_for_winner`` is called to see if
        any candidate has crossed the majority threshold.

        Args:
            voter: The member casting the vote.
            candidate: The member being voted for.
        """
        # Block votes while fractal is paused. The 'paused' attribute is not
        # set in __init__; it is dynamically added by the facilitator's pause
        # command, so we use getattr with a False default for safety.
        if getattr(self, 'paused', False):
            await self.thread.send(f"⏸️ Voting is paused. {voter.mention}, please wait for the facilitator to resume.")
            return

        previous_vote = self.votes.get(voter.id)
        previous_candidate = None

        if previous_vote:
            # Look up the previous candidate's Member object. They could be in
            # active_candidates OR among past winners (already removed from the
            # pool), so we search both lists to resolve the display name.
            previous_candidate = discord.utils.get(self.active_candidates + [m for m in self.members if m.id in [w.id for w in self.winners.values()]], id=previous_vote)

        # Record or overwrite the voter's choice for this round. Each voter
        # can only have one active vote at a time; re-voting replaces the old one.
        self.votes[voter.id] = candidate.id

        # Push real-time vote data to the web dashboard for live spectators
        await web_integration.notify_vote_cast(self, voter, candidate)

        # Announce the vote publicly so all members have full transparency
        if previous_candidate:
            await self.thread.send(
                f"🔄 **Vote Changed:** {voter.mention} changed vote from {previous_candidate.mention} to {candidate.mention}"
            )
        else:
            await self.thread.send(
                f"✅ **New Vote:** {voter.mention} voted for {candidate.mention}"
            )

        # Check if this vote caused a winner
        await self.check_for_winner()

    async def check_for_winner(self):
        """Check if any candidate has reached the vote threshold and advance the round.

        Tallies all votes for the current round and compares against the
        majority threshold (ceil(n/2)). If a candidate meets or exceeds
        the threshold, they win. In the unlikely event of a tie at the
        maximum vote count, one of the tied candidates is chosen at random.

        This method is called after every vote, so it runs frequently but
        only triggers a round transition when the threshold is actually met.
        """
        vote_counts = {}

        # Tally: count how many votes each candidate received this round
        for candidate_id in self.votes.values():
            vote_counts[candidate_id] = vote_counts.get(candidate_id, 0) + 1

        threshold = self.get_vote_threshold()

        # Determine the highest vote count any single candidate has
        max_votes = max(vote_counts.values()) if vote_counts else 0

        if max_votes >= threshold:
            # Collect all candidates sharing the top vote count (usually just one)
            winners_with_max_votes = [
                candidate_id for candidate_id, count in vote_counts.items()
                if count == max_votes
            ]

            # Handle ties: if multiple candidates share the max, pick one at random
            if len(winners_with_max_votes) > 1:
                await self.thread.send(
                    f"🎲 **Tie detected!** {len(winners_with_max_votes)} candidates tied with {max_votes} votes. Selecting randomly..."
                )
                winner_id = random.choice(winners_with_max_votes)
            else:
                winner_id = winners_with_max_votes[0]

            # Resolve the winner's Member object from the active candidate list.
            # This should always succeed unless the member left the server mid-vote.
            winner = discord.utils.get(self.active_candidates, id=winner_id)
            if winner:
                # Log winner info
                self.logger.info(f"Winner for level {self.current_level}: {winner.display_name} with {max_votes}/{len(self.members)} votes")

                # Notify web app of round completion
                await web_integration.notify_round_complete(self, winner)

                await self.start_new_round(winner)
                return

    async def end_fractal(self):
        """End the fractal process, post final results, and clean up.

        This method handles all post-fractal work:
        1. Assigns the last remaining candidate to the lowest open level.
        2. Builds the final ranking (sorted highest level first).
        3. Posts results in the fractal thread.
        4. Generates the onchain submission link (submitBreakout URL).
        5. Notifies the web dashboard.
        6. Records the session to persistent fractal history.
        7. Posts a summary embed to the server's general/main channel.
        8. Removes this group from the cog's active_groups map.
        """
        # If exactly one candidate remains, they get the current (lowest) level
        if len(self.active_candidates) == 1:
            self.winners[self.current_level] = self.active_candidates[0]

        # Build the final ranking list ordered from highest level (6) to lowest (1)
        final_ranking = []
        for level in sorted(self.winners.keys(), reverse=True):
            final_ranking.append(self.winners[level])

        # Show results in fractal thread with medal emojis for the top 3,
        # plain numbering for ranks 4+. enumerate starts at 1 so i matches
        # the human-readable rank (1st, 2nd, 3rd...).
        results_text = "# 🏆 **FRACTAL COMPLETE!** 🏆\n\n**Final Rankings:**\n"
        for i, winner in enumerate(final_ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            results_text += f"{medal} {winner.mention}\n"

        await self.thread.send(results_text)

        # Generate onchain submit breakout link
        await self._post_submit_breakout(final_ranking)

        # Notify web app that fractal is complete
        await web_integration.notify_fractal_complete(self)

        # Persist the session to the bot's fractal history store so it can be
        # queried later (e.g. leaderboard, analytics). Each member gets a
        # Respect score based on their ranking position (index into RESPECT_POINTS).
        try:
            history = getattr(self.cog.bot, 'fractal_history', None)
            if history:
                from config.config import RESPECT_POINTS
                rankings_data = []
                for i, member in enumerate(final_ranking):
                    # RESPECT_POINTS is a Fibonacci-like list: index 0 = highest rank
                    respect = RESPECT_POINTS[i] if i < len(RESPECT_POINTS) else 0
                    rankings_data.append({
                        'user_id': str(member.id),
                        'display_name': member.display_name,
                        'level': 6 - i,  # Convert ranking index back to level number
                        'respect': respect
                    })
                history.record(
                    group_name=self.thread.name,
                    facilitator_id=self.facilitator.id,
                    facilitator_name=self.facilitator.display_name,
                    # fractal_number and group_number are set externally by
                    # FractalNameModal; default to empty string if unset.
                    fractal_number=getattr(self, 'fractal_number', ''),
                    group_number=getattr(self, 'group_number', ''),
                    guild_id=self.thread.guild.id,
                    thread_id=self.thread.id,
                    rankings=rankings_data
                )
                self.logger.info(f"Recorded fractal '{self.thread.name}' to history")
        except Exception as e:
            self.logger.error(f"Failed to record fractal history: {e}")

        # Post a summary embed to the server's general channel so all members
        # (not just those in the fractal thread) can see the results and vote onchain.
        try:
            # Heuristic channel lookup: prefer channels named general/main/chat/lobby
            general_channel = None
            for channel in self.thread.guild.channels:
                if isinstance(channel, discord.TextChannel) and (
                    'general' in channel.name.lower() or
                    'main' in channel.name.lower() or
                    channel.name.lower() in ['chat', 'lobby']
                ):
                    general_channel = channel
                    break

            if not general_channel:
                # Last resort: use the first text channel the bot can see
                general_channel = next(
                    (ch for ch in self.thread.guild.channels if isinstance(ch, discord.TextChannel)),
                    None
                )

            if general_channel:
                from config.config import RESPECT_POINTS
                fibonacci = RESPECT_POINTS  # Fibonacci-like Respect distribution

                # Build human-readable rankings with medal emojis and Respect earned.
                # Note: this loop is 0-indexed (unlike the thread results which use
                # enumerate(..., 1)), so medal checks compare against 0, 1, 2.
                rankings_lines = []
                for i, winner in enumerate(final_ranking):
                    medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"**{i+1}.**"
                    respect = fibonacci[i] if i < len(fibonacci) else 0
                    rankings_lines.append(f"{medal} {winner.mention}  —  **+{respect} Respect**")

                # Build the submitBreakout URL so members can confirm results onchain.
                # URL format: /submitBreakout?groupnumber=N&vote1=WALLET&vote2=WALLET&...
                # where vote1 = highest ranked member's wallet, vote2 = second, etc.
                submit_url = None
                registry = getattr(self.cog.bot, 'wallet_registry', None)
                if registry:
                    params = {'groupnumber': getattr(self, 'group_number', '1')}
                    for i, member in enumerate(final_ranking):
                        wallet = registry.lookup(member)
                        # Empty string for members who haven't registered a wallet
                        params[f'vote{i+1}'] = wallet if wallet else ''
                    submit_url = f"https://zao.frapps.xyz/submitBreakout?{urlencode(params)}"

                # 0x57F287 is Discord's "green" brand color, used for success embeds
                embed = discord.Embed(
                    title=f"🏆 {self.thread.name} — Results",
                    description=(
                        "**Final Rankings:**\n\n"
                        + "\n".join(rankings_lines)
                    ),
                    color=0x57F287
                )

                # Only add the onchain submission field if wallet lookups succeeded
                # (registry was available and at least one wallet was found)
                if submit_url:
                    embed.add_field(
                        name="🗳️ Submit Results Onchain",
                        value=f"**[Click here to vote and confirm results]({submit_url})**",
                        inline=False
                    )

                # Brand footer shown at the bottom of every results embed
                embed.set_footer(text="ZAO Fractal • zao.frapps.xyz")

                # Post the embed to the general channel with @mentions for all
                # fractal members so they get a notification outside the thread
                mentions = " ".join([m.mention for m in self.members])
                await general_channel.send(
                    content=f"🏆 **Fractal complete!** {mentions} — go vote to submit results onchain! 👇",
                    embed=embed
                )

        except Exception as e:
            self.logger.error(f"Failed to post results to general channel: {e}")

        # Clean up: remove this group from the cog's tracking dict so new
        # fractals can be started in the same thread if needed
        if hasattr(self.cog, 'active_groups') and self.thread.id in self.cog.active_groups:
            del self.cog.active_groups[self.thread.id]

        self.logger.info(f"Fractal group '{self.thread.name}' completed")

    async def _post_submit_breakout(self, final_ranking):
        """Build and post the onchain submission link to the fractal thread.

        Constructs a URL to ``zao.frapps.xyz/submitBreakout`` with query
        parameters mapping each rank position (vote1..voteN) to the
        member's registered Ethereum wallet address.  Members without a
        linked wallet are included as empty params and flagged in the embed.

        Args:
            final_ranking: Ordered list of discord.Member from highest to
                           lowest level.
        """
        try:
            # Get wallet registry from the bot
            registry = getattr(self.cog.bot, 'wallet_registry', None)
            if not registry:
                self.logger.warning("No wallet registry available - skipping submitBreakout link")
                return

            # Look up Ethereum wallet addresses for each ranked member.
            # wallet_params accumulates raw key=value pairs (legacy, kept for logging).
            # ranked_wallets pairs each (member, wallet_or_None) for URL + display.
            # missing collects display names of members without a linked wallet.
            wallet_params = []
            missing = []
            ranked_wallets = []
            for i, member in enumerate(final_ranking):
                wallet = registry.lookup(member)  # Returns hex address string or None
                if wallet:
                    wallet_params.append(f"vote{i+1}={wallet}")
                    ranked_wallets.append((member, wallet))
                else:
                    missing.append(member.display_name)
                    wallet_params.append(f"vote{i+1}=")  # Placeholder empty param
                    ranked_wallets.append((member, None))

            # group_number is set by the FractalNameModal when the facilitator
            # creates the session. Falls back to '1' if unset (single-group fractal).
            group_number = getattr(self, 'group_number', '1')

            # Build the URL using urlencode for proper percent-encoding of wallet addresses.
            # Final URL looks like: /submitBreakout?groupnumber=1&vote1=0xABC...&vote2=0xDEF...
            base_url = "https://zao.frapps.xyz/submitBreakout"
            url_params = {'groupnumber': group_number}
            for i, (member, wallet) in enumerate(ranked_wallets):
                url_params[f'vote{i+1}'] = wallet if wallet else ''
            submit_url = f"{base_url}?{urlencode(url_params)}"

            # Build rankings text showing truncated wallet addresses next to each member
            from config.config import RESPECT_POINTS
            fibonacci = RESPECT_POINTS  # Fibonacci-like Respect point distribution
            rankings_lines = []
            for i, (member, wallet) in enumerate(ranked_wallets):
                respect = fibonacci[i] if i < len(fibonacci) else 0
                # Show first 6 and last 4 chars of the wallet for readability (e.g. 0x1234...abcd)
                short = f"`{wallet[:6]}...{wallet[-4:]}`" if wallet else "⚠️ missing"
                rankings_lines.append(f"**{i+1}.** {member.mention} → {short} (+{respect} Respect)")

            # Post the embed to the fractal thread. The embed URL is set to the
            # submitBreakout link so clicking the embed title opens the submission page.
            # Color 0x57F287 is Discord's green for success/positive outcomes.
            embed = discord.Embed(
                title="🗳️ Submit Results Onchain",
                description=(
                    "**Fractal complete! Now submit these rankings onchain to earn Respect.**\n\n"
                    + "\n".join(rankings_lines)
                ),
                color=0x57F287,
                url=submit_url
            )

            # If any members have not linked a wallet via /register, add a warning
            # field so they know they need to register before results can be submitted.
            if missing:
                embed.add_field(
                    name="⚠️ Missing Wallets",
                    value=f"{', '.join(missing)} — use `/register 0xYourAddress` to link",
                    inline=False
                )

            embed.set_footer(text="ZAO Fractal • zao.frapps.xyz")

            await self.thread.send(embed=embed)

            # Post a plain-text message with the raw URL and @mentions. This
            # ensures every member gets a notification ping and the link is
            # visible even for users whose clients do not render embeds.
            mentions = " ".join([m.mention for m in self.members])
            await self.thread.send(
                f"🔗 **Go vote here to submit results onchain:**\n"
                f"{submit_url}\n\n"
                f"{mentions} — click the link above to confirm the breakout results!"
            )

        except Exception as e:
            self.logger.error(f"Error generating submitBreakout link: {e}", exc_info=True)

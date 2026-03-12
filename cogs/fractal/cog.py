import discord
from discord import app_commands
from discord.ext import commands
import logging
import random
import math
from datetime import datetime
from ..base import BaseCog
from .views import MemberConfirmationView
from .group import FractalGroup
from config.config import INTROS_CHANNEL_ID

class FractalCog(BaseCog):
    """Cog for handling ZAO Fractal voting commands and logic"""

    def __init__(self, bot):
        super().__init__(bot)
        self.bot = bot
        self.logger = logging.getLogger('bot')
        self.active_groups = {}  # Dict mapping thread_id to FractalGroup
        self.daily_counters = {}  # Dict mapping guild_id -> {date: counter}

        # Create admin command group
        self.admin_group = app_commands.Group(name="admin", description="Admin commands for fractal management")

    def _get_next_group_name(self, guild_id: int) -> str:
        """Generate auto-incremented group name for the day"""
        today = datetime.now().strftime("%b %d, %Y")

        if guild_id not in self.daily_counters:
            self.daily_counters[guild_id] = {}

        if today not in self.daily_counters[guild_id]:
            self.daily_counters[guild_id][today] = 0

        self.daily_counters[guild_id][today] += 1
        counter = self.daily_counters[guild_id][today]

        return f"Fractal Group {counter} - {today}"

    @app_commands.command(
        name="zaofractal",
        description="Create a new ZAO fractal voting group from your current voice channel"
    )
    @app_commands.describe(name="Custom name for this fractal group (optional)")
    async def zaofractal(self, interaction: discord.Interaction, name: str = None):
        """Create a new ZAO fractal voting group from voice channel members"""
        # Check if interaction has already been responded to
        if interaction.response.is_done():
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        except discord.InteractionResponded:
            pass

        # Check user's voice state
        voice_check = await self.check_voice_state(interaction.user)
        if not voice_check['success']:
            try:
                await interaction.followup.send(voice_check['message'], ephemeral=True)
            except:
                await interaction.channel.send(f"{interaction.user.mention} {voice_check['message']}")
            return

        members = voice_check['members']

        # Store custom name for use in confirmation view
        custom_name = name

        # Check wallet and intro status for each member
        registry = getattr(self.bot, 'wallet_registry', None)
        self.logger.info(f"Wallet registry available: {registry is not None}")

        # Get intro cache from IntroCog
        intro_cog = self.bot.get_cog('IntroCog')
        intro_cache = intro_cog.intro_cache if intro_cog else None

        member_status = []
        without_wallet = []
        without_intro = []
        for member in members:
            wallet = registry.lookup(member) if registry else None
            has_intro = bool(intro_cache.get(member.id)) if intro_cache else None
            self.logger.info(f"Wallet lookup for {member.display_name} (id={member.id}, name={member.name}, global={member.global_name}): {wallet}")
            if not wallet:
                without_wallet.append(member)
            if not has_intro:
                without_intro.append(member)
            member_status.append((member, wallet, has_intro))

        # Build confirmation message with wallet + intro status
        lines = [f"**Start fractal{f' ({custom_name})' if custom_name else ''}?**\n"]
        for member, wallet, has_intro in member_status:
            wallet_icon = "✅" if wallet else "❌"
            intro_icon = "📝" if has_intro else "📭"
            issues = []
            if not wallet:
                issues.append("no wallet")
            if not has_intro:
                issues.append("no intro")
            suffix = f" — {', '.join(issues)}" if issues else ""
            lines.append(f"{wallet_icon}{intro_icon} {member.mention}{suffix}")

        warnings = []
        if without_wallet:
            warnings.append(f"⚠️ **{len(without_wallet)}** member(s) have no wallet. They should `/register` before results are submitted onchain.")
        if without_intro:
            warnings.append(f"📭 **{len(without_intro)}** member(s) have no intro. They should post in <#{INTROS_CHANNEL_ID}>.")
        if warnings:
            lines.append("")
            lines.extend(warnings)

        confirm_msg = "\n".join(lines)

        # Send member confirmation
        view = MemberConfirmationView(self, members, interaction.user, custom_name=custom_name)
        try:
            await interaction.followup.send(confirm_msg, view=view, ephemeral=True)
        except:
            await interaction.channel.send(
                f"{interaction.user.mention} {confirm_msg}",
                view=view
            )

    @app_commands.command(
        name="randomize",
        description="Split members from Fractal Waiting Room into fractal rooms (max 6 per room)"
    )
    @app_commands.describe(
        facilitator_1="Facilitator for fractal-1",
        facilitator_2="Facilitator for fractal-2",
        facilitator_3="Facilitator for fractal-3",
        facilitator_4="Facilitator for fractal-4",
        facilitator_5="Facilitator for fractal-5",
        facilitator_6="Facilitator for fractal-6",
    )
    async def randomize(
        self,
        interaction: discord.Interaction,
        facilitator_1: discord.Member = None,
        facilitator_2: discord.Member = None,
        facilitator_3: discord.Member = None,
        facilitator_4: discord.Member = None,
        facilitator_5: discord.Member = None,
        facilitator_6: discord.Member = None,
    ):
        """Randomly split waiting room members into fractal-1, fractal-2, etc."""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        guild = interaction.guild

        # Collect facilitator assignments (index -> member)
        facilitator_params = [facilitator_1, facilitator_2, facilitator_3, facilitator_4, facilitator_5, facilitator_6]
        facilitators = {}  # room index (0-based) -> member
        for i, fac in enumerate(facilitator_params):
            if fac is not None:
                facilitators[i] = fac

        # Find the Fractal Waiting Room voice channel (case-insensitive)
        waiting_room = None
        for channel in guild.voice_channels:
            if "fractal waiting room" in channel.name.lower():
                waiting_room = channel
                break

        if not waiting_room:
            await interaction.followup.send("❌ Could not find a **Fractal Waiting Room** voice channel.", ephemeral=True)
            return

        # Get non-bot members in waiting room
        all_members = [m for m in waiting_room.members if not m.bot]

        # Separate facilitators from the random pool
        facilitator_ids = {fac.id for fac in facilitators.values()}
        random_pool = [m for m in all_members if m.id not in facilitator_ids]

        # Check if any assigned facilitators aren't in the waiting room
        waiting_ids = {m.id for m in all_members}
        not_in_room = [fac for fac in facilitators.values() if fac.id not in waiting_ids]
        if not_in_room:
            names = ", ".join(f.display_name for f in not_in_room)
            await interaction.followup.send(
                f"❌ These facilitators are not in the Fractal Waiting Room: **{names}**",
                ephemeral=True
            )
            return

        total_people = len(all_members)
        if total_people < 2:
            await interaction.followup.send("❌ Need at least 2 members in the Fractal Waiting Room to randomize.", ephemeral=True)
            return

        # Determine number of groups: at least enough for all people, and at least enough for the highest facilitator slot
        min_groups_for_people = math.ceil(total_people / 6)
        min_groups_for_facilitators = (max(facilitators.keys()) + 1) if facilitators else 0
        num_groups = max(min_groups_for_people, min_groups_for_facilitators)

        # Find or validate fractal room channels (fractal-1, fractal-2, etc.)
        fractal_rooms = []
        missing_rooms = []
        for i in range(1, num_groups + 1):
            room = None
            for channel in guild.voice_channels:
                if channel.name.lower() == f"fractal-{i}":
                    room = channel
                    break
            if room:
                fractal_rooms.append(room)
            else:
                missing_rooms.append(f"fractal-{i}")

        if missing_rooms:
            await interaction.followup.send(
                f"❌ Missing voice channels: **{', '.join(missing_rooms)}**\n"
                f"Please create these voice channels first.",
                ephemeral=True
            )
            return

        # Pre-assign facilitators to their rooms
        groups = [[] for _ in range(num_groups)]
        group_facilitators = {}  # room index -> facilitator member
        for room_idx, fac in facilitators.items():
            groups[room_idx].append(fac)
            group_facilitators[room_idx] = fac

        # Shuffle remaining members randomly
        random.shuffle(random_pool)

        # Distribute remaining members evenly via round-robin, respecting max 6
        for member in random_pool:
            # Find the group with the fewest members (that isn't full)
            min_idx = min(range(num_groups), key=lambda idx: len(groups[idx]))
            groups[min_idx].append(member)

        # Move members to their assigned rooms
        move_results = []
        failed_moves = []
        for group_idx, group_members in enumerate(groups):
            room = fractal_rooms[group_idx]
            fac = group_facilitators.get(group_idx)
            for member in group_members:
                try:
                    await member.move_to(room)
                except discord.HTTPException as e:
                    failed_moves.append((member, str(e)))

            member_names = ", ".join(m.display_name for m in group_members)
            fac_label = f" | Facilitator: **{fac.display_name}**" if fac else ""
            move_results.append(f"**{room.name}** ({len(group_members)}){fac_label}\n> {member_names}")

        # Build result message
        msg = f"# Randomized {total_people} members into {num_groups} groups\n\n"
        msg += "\n\n".join(move_results)

        if failed_moves:
            msg += f"\n\n⚠️ Failed to move {len(failed_moves)} member(s):"
            for member, error in failed_moves:
                msg += f"\n• {member.display_name}: {error}"

        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(
        name="endgroup",
        description="End an active fractal group (facilitator only)"
    )
    async def end_group(self, interaction: discord.Interaction):
        """End an active fractal group"""
        await interaction.response.defer(ephemeral=True)

        # Check if in a fractal thread
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.followup.send("❌ This command can only be used in a fractal group thread.", ephemeral=True)
            return

        # Check if this is an active fractal group
        group = self.active_groups.get(interaction.channel.id)
        if not group:
            await interaction.followup.send("❌ This thread is not an active fractal group.", ephemeral=True)
            return

        # Check if user is facilitator
        if interaction.user.id != group.facilitator.id:
            await interaction.followup.send("❌ Only the group facilitator can end the fractal group.", ephemeral=True)
            return

        # End the fractal group (end_fractal() removes from active_groups itself)
        await group.end_fractal()
        # Guard against double-delete since end_fractal() already removes it
        self.active_groups.pop(interaction.channel.id, None)

        await interaction.followup.send("✅ Fractal group ended successfully.", ephemeral=True)

    @app_commands.command(
        name="status",
        description="Show the current status of an active fractal group"
    )
    async def status(self, interaction: discord.Interaction):
        """Show the status of an active fractal group"""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        except discord.InteractionResponded:
            pass

        # Check if in a fractal thread
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.followup.send("❌ This command can only be used in a fractal group thread.", ephemeral=True)
            return

        # Check if this is an active fractal group
        group = self.active_groups.get(interaction.channel.id)
        if not group:
            await interaction.followup.send("❌ This thread is not an active fractal group.", ephemeral=True)
            return

        # Build status message
        status = f"# ZAO Fractal Status\n\n"
        status += f"**Group:** {interaction.channel.name}\n"
        status += f"**Facilitator:** {group.facilitator.mention}\n"
        status += f"**Current Level:** {group.current_level}\n"
        status += f"**Members:** {len(group.members)}\n"
        status += f"**Active Candidates:** {len(group.active_candidates)}\n"
        status += f"**Votes Cast:** {len(group.votes)}/{len(group.members)}\n\n"

        # Winners so far
        if group.winners:
            status += "**Winners:**\n"
            for level, winner in sorted(group.winners.items(), reverse=True):
                status += f"Level {level}: {winner.mention}\n"

        await interaction.followup.send(status, ephemeral=True)

    @app_commands.command(
        name="groupwallets",
        description="Show wallet addresses for all members in the current fractal group"
    )
    async def groupwallets(self, interaction: discord.Interaction):
        """List wallet addresses for all members in the active fractal group"""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        except discord.InteractionResponded:
            pass

        # Check if in a fractal thread
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.followup.send("❌ Use this in a fractal group thread.", ephemeral=True)
            return

        group = self.active_groups.get(interaction.channel.id)
        if not group:
            await interaction.followup.send("❌ This thread is not an active fractal group.", ephemeral=True)
            return

        registry = getattr(self.bot, 'wallet_registry', None)
        if not registry:
            await interaction.followup.send("❌ Wallet registry not available.", ephemeral=True)
            return

        msg = f"# 🔗 Group Wallets\n\n"
        missing = []
        for member in group.members:
            wallet = registry.lookup(member)
            if wallet:
                short = f"{wallet[:6]}...{wallet[-4:]}"
                msg += f"✅ **{member.display_name}** → `{wallet}`\n"
            else:
                msg += f"❌ **{member.display_name}** → No wallet registered\n"
                missing.append(member.display_name)

        if missing:
            msg += f"\n⚠️ **{len(missing)} member(s) missing wallets.** They can use `/register 0xAddress` to link."
        else:
            msg += f"\n✅ All {len(group.members)} members have wallets linked!"

        await interaction.followup.send(msg, ephemeral=True)

    # Admin Commands
    @app_commands.command(
        name="admin_end_fractal",
        description="[ADMIN] Force end any active fractal group"
    )
    @app_commands.describe(thread_id="ID of the thread to end (optional)")
    async def admin_end_fractal(self, interaction: discord.Interaction, thread_id: str = None):
        """Admin command to force end fractals"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        if thread_id:
            # End specific fractal
            try:
                thread_id_int = int(thread_id)
                if thread_id_int in self.active_groups:
                    group = self.active_groups[thread_id_int]
                    await group.end_fractal()
                    await interaction.followup.send(f"✅ Ended fractal in {group.thread.mention}", ephemeral=True)
                else:
                    await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
            except ValueError:
                await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        else:
            # Show list of active fractals to choose from
            if not self.active_groups:
                await interaction.followup.send("❌ No active fractals to end.", ephemeral=True)
                return

            status = "**Active Fractals:**\n"
            for thread_id, group in self.active_groups.items():
                status += f"• {group.thread.mention} (ID: {thread_id}) - Level {group.current_level}\n"
            status += "\nUse `/admin_end_fractal thread_id:<ID>` to end a specific one."

            await interaction.followup.send(status, ephemeral=True)

    @app_commands.command(
        name="admin_list_fractals",
        description="[ADMIN] List all active fractal groups"
    )
    async def admin_list_fractals(self, interaction: discord.Interaction):
        """Admin command to list all active fractals"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        if not self.active_groups:
            await interaction.followup.send("✅ No active fractal groups.", ephemeral=True)
            return

        status = f"**Active Fractal Groups ({len(self.active_groups)}):**\n\n"
        for thread_id, group in self.active_groups.items():
            status += f"**{group.thread.name}**\n"
            status += f"• Thread: {group.thread.mention}\n"
            status += f"• Facilitator: {group.facilitator.mention}\n"
            status += f"• Current Level: {group.current_level}\n"
            status += f"• Members: {len(group.members)}\n"
            status += f"• Active Candidates: {len(group.active_candidates)}\n"
            status += f"• Votes Cast: {len(group.votes)}\n\n"

        await interaction.followup.send(status, ephemeral=True)

    @app_commands.command(
        name="admin_cleanup",
        description="[ADMIN] Clean up old/stuck fractal groups"
    )
    async def admin_cleanup(self, interaction: discord.Interaction):
        """Admin command to cleanup stuck fractals"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        cleaned_count = 0
        to_remove = []

        for thread_id, group in self.active_groups.items():
            try:
                # Check if thread still exists and is accessible
                thread = self.bot.get_channel(thread_id)
                if not thread or thread.archived:
                    to_remove.append(thread_id)
                    cleaned_count += 1
            except:
                to_remove.append(thread_id)
                cleaned_count += 1

        # Remove invalid groups
        for thread_id in to_remove:
            del self.active_groups[thread_id]

        await interaction.followup.send(
            f"✅ Cleanup complete. Removed {cleaned_count} inactive fractal groups.",
            ephemeral=True
        )

    # Force Round Progression Commands
    @app_commands.command(
        name="admin_force_round",
        description="[ADMIN] Skip current voting and move to next level"
    )
    @app_commands.describe(thread_id="ID of the fractal thread")
    async def admin_force_round(self, interaction: discord.Interaction, thread_id: str):
        """Admin command to force move to next round"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]

            # Find candidate with most votes or pick randomly if tie
            vote_counts = {}
            for candidate_id in group.votes.values():
                vote_counts[candidate_id] = vote_counts.get(candidate_id, 0) + 1

            if vote_counts:
                max_votes = max(vote_counts.values())
                winners = [cid for cid, count in vote_counts.items() if count == max_votes]
                winner_id = winners[0] if len(winners) == 1 else random.choice(winners)
                winner = discord.utils.get(group.active_candidates, id=winner_id)
            else:
                # No votes cast, pick random candidate
                winner = random.choice(group.active_candidates)

            await group.thread.send(f"⚡ **ADMIN OVERRIDE:** Forcing round completion. Winner: {winner.mention}")
            await group.start_new_round(winner)

            await interaction.followup.send(f"✅ Forced round completion in {group.thread.mention}. Winner: {winner.mention}", ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error forcing round: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="admin_reset_votes",
        description="[ADMIN] Clear all votes in current round"
    )
    @app_commands.describe(thread_id="ID of the fractal thread")
    async def admin_reset_votes(self, interaction: discord.Interaction, thread_id: str):
        """Admin command to reset votes in current round"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]
            old_vote_count = len(group.votes)
            group.votes = {}

            await group.thread.send(f"⚡ **ADMIN RESET:** All votes cleared. Voting restarted for Level {group.current_level}.")

            await interaction.followup.send(f"✅ Reset {old_vote_count} votes in {group.thread.mention}", ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error resetting votes: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="admin_declare_winner",
        description="[ADMIN] Manually declare a round winner"
    )
    @app_commands.describe(thread_id="ID of the fractal thread", user="User to declare as winner")
    async def admin_declare_winner(self, interaction: discord.Interaction, thread_id: str, user: discord.Member):
        """Admin command to manually declare a winner"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]

            if user not in group.active_candidates:
                await interaction.followup.send(f"❌ {user.mention} is not an active candidate in this fractal.", ephemeral=True)
                return

            await group.thread.send(f"⚡ **ADMIN DECLARATION:** {user.mention} declared winner of Level {group.current_level}!")
            await group.start_new_round(user)

            await interaction.followup.send(f"✅ Declared {user.mention} as winner in {group.thread.mention}", ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error declaring winner: {str(e)}", ephemeral=True)

    # Member Management Commands
    @app_commands.command(
        name="admin_add_member",
        description="[ADMIN] Add someone to an active fractal"
    )
    @app_commands.describe(thread_id="ID of the fractal thread", user="User to add to the fractal")
    async def admin_add_member(self, interaction: discord.Interaction, thread_id: str, user: discord.Member):
        """Admin command to add member to active fractal"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]

            if user in group.members:
                await interaction.followup.send(f"❌ {user.mention} is already in this fractal.", ephemeral=True)
                return

            # Add to members and active candidates
            group.members.append(user)
            group.active_candidates.append(user)

            # Add to thread
            try:
                await group.thread.add_user(user)
            except discord.HTTPException:
                pass

            await group.thread.send(f"⚡ **ADMIN ADD:** {user.mention} has been added to the fractal!")

            await interaction.followup.send(f"✅ Added {user.mention} to {group.thread.mention}", ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error adding member: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="admin_remove_member",
        description="[ADMIN] Remove someone from active fractal"
    )
    @app_commands.describe(thread_id="ID of the fractal thread", user="User to remove from the fractal")
    async def admin_remove_member(self, interaction: discord.Interaction, thread_id: str, user: discord.Member):
        """Admin command to remove member from active fractal"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]

            if user not in group.members:
                await interaction.followup.send(f"❌ {user.mention} is not in this fractal.", ephemeral=True)
                return

            # Remove from members and active candidates
            if user in group.members:
                group.members.remove(user)
            if user in group.active_candidates:
                group.active_candidates.remove(user)

            # Remove their vote if they had one
            if user.id in group.votes:
                del group.votes[user.id]

            await group.thread.send(f"⚡ **ADMIN REMOVE:** {user.mention} has been removed from the fractal.")

            await interaction.followup.send(f"✅ Removed {user.mention} from {group.thread.mention}", ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error removing member: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="admin_change_facilitator",
        description="[ADMIN] Transfer facilitator role to another member"
    )
    @app_commands.describe(thread_id="ID of the fractal thread", user="New facilitator")
    async def admin_change_facilitator(self, interaction: discord.Interaction, thread_id: str, user: discord.Member):
        """Admin command to change facilitator"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]
            old_facilitator = group.facilitator

            if user not in group.members:
                await interaction.followup.send(f"❌ {user.mention} must be a member of the fractal to become facilitator.", ephemeral=True)
                return

            group.facilitator = user

            await group.thread.send(f"⚡ **FACILITATOR CHANGE:** {old_facilitator.mention} → {user.mention}")

            await interaction.followup.send(f"✅ Changed facilitator from {old_facilitator.mention} to {user.mention} in {group.thread.mention}", ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error changing facilitator: {str(e)}", ephemeral=True)

    # Group Control Commands
    @app_commands.command(
        name="admin_pause_fractal",
        description="[ADMIN] Temporarily pause voting in a fractal"
    )
    @app_commands.describe(thread_id="ID of the fractal thread")
    async def admin_pause_fractal(self, interaction: discord.Interaction, thread_id: str):
        """Admin command to pause fractal voting"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]

            # Add paused flag to group
            if not hasattr(group, 'paused'):
                group.paused = False

            if group.paused:
                await interaction.followup.send("❌ Fractal is already paused.", ephemeral=True)
                return

            group.paused = True

            await group.thread.send("⏸️ **FRACTAL PAUSED** by admin. Voting is temporarily suspended.")

            await interaction.followup.send(f"✅ Paused fractal in {group.thread.mention}", ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error pausing fractal: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="admin_resume_fractal",
        description="[ADMIN] Resume paused fractal voting"
    )
    @app_commands.describe(thread_id="ID of the fractal thread")
    async def admin_resume_fractal(self, interaction: discord.Interaction, thread_id: str):
        """Admin command to resume paused fractal"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]

            if not hasattr(group, 'paused') or not group.paused:
                await interaction.followup.send("❌ Fractal is not paused.", ephemeral=True)
                return

            group.paused = False

            await group.thread.send("▶️ **FRACTAL RESUMED** by admin. Voting continues!")

            await interaction.followup.send(f"✅ Resumed fractal in {group.thread.mention}", ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error resuming fractal: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="admin_restart_fractal",
        description="[ADMIN] Restart fractal from beginning with same members"
    )
    @app_commands.describe(thread_id="ID of the fractal thread")
    async def admin_restart_fractal(self, interaction: discord.Interaction, thread_id: str):
        """Admin command to restart fractal from beginning"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]

            # Reset fractal state
            group.current_level = 6
            group.votes = {}
            group.winners = {}
            group.active_candidates = group.members.copy()
            if hasattr(group, 'paused'):
                group.paused = False

            await group.thread.send("🔄 **FRACTAL RESTARTED** by admin. Starting fresh from Level 6!")

            # Start new round
            await group.start_new_round()

            await interaction.followup.send(f"✅ Restarted fractal in {group.thread.mention}", ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error restarting fractal: {str(e)}", ephemeral=True)

    # Advanced Monitoring Commands
    @app_commands.command(
        name="admin_fractal_stats",
        description="[ADMIN] Detailed stats for a specific fractal group"
    )
    @app_commands.describe(thread_id="ID of the fractal thread")
    async def admin_fractal_stats(self, interaction: discord.Interaction, thread_id: str):
        """Admin command to get detailed fractal stats"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            thread_id_int = int(thread_id)
            if thread_id_int not in self.active_groups:
                await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                return

            group = self.active_groups[thread_id_int]

            # Calculate detailed stats
            total_members = len(group.members)
            active_candidates = len(group.active_candidates)
            votes_cast = len(group.votes)
            vote_percentage = (votes_cast / total_members * 100) if total_members > 0 else 0

            # Vote distribution
            vote_counts = {}
            for candidate_id in group.votes.values():
                candidate = discord.utils.get(group.active_candidates, id=candidate_id)
                if candidate:
                    vote_counts[candidate.display_name] = vote_counts.get(candidate.display_name, 0) + 1

            stats = f"# 📊 **Detailed Fractal Stats**\n\n"
            stats += f"**Thread:** {group.thread.mention}\n"
            stats += f"**Facilitator:** {group.facilitator.mention}\n"
            stats += f"**Current Level:** {group.current_level}\n"
            stats += f"**Status:** {'⏸️ Paused' if hasattr(group, 'paused') and group.paused else '▶️ Active'}\n\n"

            stats += f"**Members:** {total_members}\n"
            stats += f"**Active Candidates:** {active_candidates}\n"
            stats += f"**Votes Cast:** {votes_cast}/{total_members} ({vote_percentage:.1f}%)\n"
            stats += f"**Votes Needed to Win:** {group.get_vote_threshold()}\n\n"

            if vote_counts:
                stats += "**Current Vote Distribution:**\n"
                for candidate, count in sorted(vote_counts.items(), key=lambda x: x[1], reverse=True):
                    stats += f"• {candidate}: {count} votes\n"
                stats += "\n"

            if group.winners:
                stats += "**Winners So Far:**\n"
                for level in sorted(group.winners.keys(), reverse=True):
                    winner = group.winners[level]
                    stats += f"• Level {level}: {winner.display_name}\n"

            await interaction.followup.send(stats, ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error getting fractal stats: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="admin_server_stats",
        description="[ADMIN] Overall server fractal statistics"
    )
    async def admin_server_stats(self, interaction: discord.Interaction):
        """Admin command to get server-wide fractal stats"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            guild_id = interaction.guild.id

            # Count active fractals for this server
            server_fractals = [group for group in self.active_groups.values() if group.thread.guild.id == guild_id]

            total_active = len(server_fractals)
            total_participants = sum(len(group.members) for group in server_fractals)
            total_votes_cast = sum(len(group.votes) for group in server_fractals)

            # Daily counter stats
            today = datetime.now().strftime("%b %d, %Y")
            daily_count = 0
            if guild_id in self.daily_counters and today in self.daily_counters[guild_id]:
                daily_count = self.daily_counters[guild_id][today]

            stats = f"# 📈 **Server Fractal Statistics**\n\n"
            stats += f"**Server:** {interaction.guild.name}\n"
            stats += f"**Active Fractals:** {total_active}\n"
            stats += f"**Total Participants:** {total_participants}\n"
            stats += f"**Total Votes Cast:** {total_votes_cast}\n"
            stats += f"**Groups Created Today:** {daily_count}\n\n"

            if server_fractals:
                stats += "**Active Groups:**\n"
                for group in server_fractals:
                    status = "⏸️ Paused" if hasattr(group, 'paused') and group.paused else "▶️ Active"
                    stats += f"• {group.thread.name} - Level {group.current_level} ({status})\n"
            else:
                stats += "No active fractals currently running.\n"

            await interaction.followup.send(stats, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error getting server stats: {str(e)}", ephemeral=True)

    @app_commands.command(
        name="admin_export_data",
        description="[ADMIN] Export fractal data for analysis"
    )
    @app_commands.describe(thread_id="ID of the fractal thread (optional - exports all if not specified)")
    async def admin_export_data(self, interaction: discord.Interaction, thread_id: str = None):
        """Admin command to export fractal data"""
        await interaction.response.defer(ephemeral=True)

        if not self.is_supreme_admin(interaction.user):
            await interaction.followup.send("❌ You need the **Supreme Admin** role to use this command.", ephemeral=True)
            return

        try:
            import json
            from datetime import datetime

            export_data = {
                "export_timestamp": datetime.now().isoformat(),
                "server_id": interaction.guild.id,
                "server_name": interaction.guild.name,
                "fractals": []
            }

            if thread_id:
                # Export specific fractal
                thread_id_int = int(thread_id)
                if thread_id_int not in self.active_groups:
                    await interaction.followup.send("❌ No active fractal found with that thread ID.", ephemeral=True)
                    return
                groups_to_export = [self.active_groups[thread_id_int]]
            else:
                # Export all fractals for this server
                groups_to_export = [group for group in self.active_groups.values() if group.thread.guild.id == interaction.guild.id]

            for group in groups_to_export:
                fractal_data = {
                    "thread_id": group.thread.id,
                    "thread_name": group.thread.name,
                    "facilitator": {
                        "id": group.facilitator.id,
                        "name": group.facilitator.display_name
                    },
                    "current_level": group.current_level,
                    "paused": hasattr(group, 'paused') and group.paused,
                    "members": [{"id": m.id, "name": m.display_name} for m in group.members],
                    "active_candidates": [{"id": m.id, "name": m.display_name} for m in group.active_candidates],
                    "votes": {str(voter_id): candidate_id for voter_id, candidate_id in group.votes.items()},
                    "winners": {str(level): {"id": winner.id, "name": winner.display_name} for level, winner in group.winners.items()}
                }
                export_data["fractals"].append(fractal_data)

            # Create JSON file content
            json_content = json.dumps(export_data, indent=2)

            # Create file and send
            import io
            file_buffer = io.StringIO(json_content)
            file = discord.File(file_buffer, filename=f"fractal_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")

            await interaction.followup.send(
                f"📁 **Data Export Complete**\n"
                f"Exported {len(export_data['fractals'])} fractal(s) from {interaction.guild.name}",
                file=file,
                ephemeral=True
            )

        except ValueError:
            await interaction.followup.send("❌ Invalid thread ID format.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error exporting data: {str(e)}", ephemeral=True)

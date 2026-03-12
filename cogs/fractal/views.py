"""
Discord UI views and modals for the ZAO Fractal voting system.

This module defines the interactive UI components used during fractal group
sessions. It contains three main components:

- ZAOFractalVotingView: A persistent view with dynamically generated buttons
  that allows group members to cast votes for candidates during fractal rounds.
- FractalNameModal: A modal dialog that collects fractal and group numbers from
  the facilitator, then creates a Discord thread and initializes the fractal session.
- MemberConfirmationView: A confirmation view shown before a fractal starts,
  allowing the facilitator to review the member list, modify it, or proceed
  to start the fractal.

These views are attached to messages sent by the bot and handle user interactions
via Discord's component-based interaction model (buttons and modals).
"""

import discord
import logging
from typing import Callable, Dict, List
from .group import FractalGroup
from config.config import FRACTAL_BOT_CHANNEL_ID

class ZAOFractalVotingView(discord.ui.View):
    """A persistent Discord UI view that renders voting buttons for fractal rounds.

    Each active candidate in the fractal group gets a colored button. When a member
    clicks a button, their vote is recorded via the FractalGroup's process_vote method.
    The view has no timeout so it remains interactive for the duration of the round.

    Attributes:
        fractal_group (FractalGroup): The fractal group instance this view belongs to.
        logger (logging.Logger): Logger for recording voting events and errors.
    """

    def __init__(self, fractal_group):
        """Initialize the voting view for a fractal group.

        Args:
            fractal_group (FractalGroup): The fractal group whose active candidates
                will be displayed as voting buttons.
        """
        super().__init__(timeout=None)  # No timeout for persistent buttons
        self.fractal_group = fractal_group
        self.logger = logging.getLogger('bot')

        # Create voting buttons
        self.create_voting_buttons()

    def create_voting_buttons(self):
        """Create a button for each active candidate in the fractal group.

        Clears any previously rendered buttons and rebuilds the button list from
        the current set of active candidates. Each button is assigned a rotating
        color style (blue, green, red) to visually distinguish adjacent candidates.
        A unique custom_id based on the candidate's Discord user ID ensures that
        button interactions can be correctly routed.
        """
        # Clear any existing buttons to allow re-creation (e.g., when candidates change)
        self.clear_items()

        # Rotate through three visually distinct styles; grey is excluded because
        # it is too subtle and can imply a disabled state.
        styles = [
            discord.ButtonStyle.primary,    # Blue
            discord.ButtonStyle.success,    # Green
            discord.ButtonStyle.danger,     # Red
        ]

        # Create a button for each candidate
        for i, candidate in enumerate(self.fractal_group.active_candidates):
            # Use modulo to cycle back to the first style after exhausting the list
            style = styles[i % len(styles)]

            # custom_id must be unique across all active views in the bot;
            # prefixing with "vote_" and using the Discord user ID ensures this.
            button = discord.ui.Button(
                style=style,
                label=candidate.display_name,
                custom_id=f"vote_{candidate.id}"
            )

            # Each button gets its own closure-based callback bound to a specific candidate
            button.callback = self.create_vote_callback(candidate)
            self.add_item(button)

        self.logger.info(f"Created {len(self.fractal_group.active_candidates)} voting buttons")

    def create_vote_callback(self, candidate):
        """Create a closure-based callback function for a specific candidate's vote button.

        This factory method is needed because each button requires its own async callback
        bound to a particular candidate. A simple lambda would not work here since the
        callback is async and needs access to both the interaction and the candidate.

        Args:
            candidate (discord.Member): The candidate this button represents. The returned
                callback will record a vote for this candidate when invoked.

        Returns:
            Callable: An async function that handles the button interaction by deferring
                the response, processing the vote, and sending a private confirmation
                or error message to the voter.
        """
        async def vote_callback(interaction):
            # Defer immediately to prevent Discord's 3-second interaction timeout.
            # ephemeral=True means only the voter sees the "thinking" indicator.
            await interaction.response.defer(ephemeral=True)

            try:
                # Delegate vote recording and public announcement to FractalGroup
                await self.fractal_group.process_vote(interaction.user, candidate)

                # Send a private ephemeral confirmation only the voter can see
                await interaction.followup.send(
                    f"You voted for {candidate.display_name}",
                    ephemeral=True
                )

            except Exception as e:
                self.logger.error(f"Error processing vote: {e}", exc_info=True)
                await interaction.followup.send(
                    "❌ Error recording your vote. Please try again.",
                    ephemeral=True
                )

        return vote_callback


class FractalNameModal(discord.ui.Modal, title="Name Your Fractal"):
    """A modal dialog that prompts the facilitator for fractal and group numbers.

    When submitted, this modal creates a new public Discord thread named
    "Fractal {n} - Group {m}", adds all confirmed members to the thread,
    initializes a FractalGroup instance, and starts the fractal session.

    The modal is presented when the facilitator clicks the "Start Fractal" button
    on the MemberConfirmationView.

    Attributes:
        fractal_number (discord.ui.TextInput): Text input for the fractal number.
        group_number (discord.ui.TextInput): Text input for the group number.
        confirmation_view (MemberConfirmationView): The parent confirmation view
            that holds the member list, facilitator, and cog references.
    """

    fractal_number = discord.ui.TextInput(
        label="Fractal Number",
        placeholder="e.g. 5",
        required=True,
        max_length=10,
        style=discord.TextStyle.short
    )

    group_number = discord.ui.TextInput(
        label="Group Number",
        placeholder="e.g. 2",
        required=True,
        max_length=10,
        style=discord.TextStyle.short
    )

    def __init__(self, confirmation_view):
        """Initialize the modal with a reference to the parent confirmation view.

        Args:
            confirmation_view (MemberConfirmationView): The view that triggered
                this modal, providing access to the member list, facilitator,
                and cog instance needed to create the fractal group.
        """
        super().__init__()
        self.confirmation_view = confirmation_view

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission: create thread, initialize group, and start the fractal.

        This method performs the following steps in order:
        1. Constructs a human-readable group name from the submitted numbers.
        2. Creates a public Discord thread in the designated fractal-bot channel.
        3. Adds all confirmed members to the thread.
        4. Instantiates a FractalGroup and registers it as an active group.
        5. Updates the original confirmation message to indicate success.
        6. Starts the fractal session (which triggers round 1).

        Args:
            interaction (discord.Interaction): The interaction triggered by
                the modal submission.
        """
        fractal_num = self.fractal_number.value.strip()
        group_num = self.group_number.value.strip()

        group_name = f"Fractal {fractal_num} - Group {group_num}"

        # Defer to allow longer processing time (thread creation, member adds, etc.)
        await interaction.response.defer()

        # Prefer the dedicated fractal-bot channel so all fractal threads live
        # in one place, regardless of where the command was invoked.
        channel = interaction.guild.get_channel(FRACTAL_BOT_CHANNEL_ID)
        if not channel:
            # Fallback to current channel if the configured channel is missing.
            # If the current channel is itself a thread, use its parent text channel
            # since threads cannot contain child threads.
            channel = interaction.channel
            if isinstance(channel, discord.Thread):
                channel = channel.parent

        # Create public thread so all server members can observe the fractal session
        thread = await channel.create_thread(
            name=group_name,
            type=discord.ChannelType.public_thread,
            reason="ZAO Fractal Group"
        )

        # Explicitly add each member to the thread so they receive notifications.
        # Silently skip members that can't be added (e.g., left the server).
        for member in self.confirmation_view.members:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                pass

        # Create the FractalGroup instance that manages rounds, voting, and scoring
        fractal_group = FractalGroup(
            thread=thread,
            members=self.confirmation_view.members,
            facilitator=self.confirmation_view.facilitator,
            cog=self.confirmation_view.cog
        )

        # Store fractal/group numbers for constructing the submitBreakout API URL
        fractal_group.fractal_number = fractal_num
        fractal_group.group_number = group_num

        # Capture the facilitator's current voice channel so the group can send
        # audio notifications or voice-related prompts during the session.
        if self.confirmation_view.facilitator.voice and self.confirmation_view.facilitator.voice.channel:
            fractal_group.voice_channel = self.confirmation_view.facilitator.voice.channel

        # Register in the cog's active_groups dict keyed by thread ID, enabling
        # the cog to route thread messages to the correct FractalGroup instance.
        self.confirmation_view.cog.active_groups[thread.id] = fractal_group

        # Replace the confirmation buttons with a success message linking to the thread
        try:
            await interaction.edit_original_response(
                content=f"✅ **{group_name} started!** Check {thread.mention}",
                view=None  # Remove buttons since the fractal is now started
            )
        except (discord.NotFound, discord.HTTPException) as e:
            logging.getLogger('bot').warning(f"Failed to update confirmation message: {e}")

        # Begin the fractal session; any startup error is reported in the thread
        # and re-raised so the caller can handle cleanup.
        try:
            await fractal_group.start_fractal()
        except Exception as e:
            await thread.send(f"❌ Error starting fractal: {str(e)}")
            raise


class MemberConfirmationView(discord.ui.View):
    """A pre-start confirmation view that displays the proposed member list for a fractal group.

    This view is shown to the facilitator after they invoke the fractal start command.
    It presents two buttons:
    - "Start Fractal": Opens the FractalNameModal to collect naming info and proceed.
    - "Modify Members": Instructs the facilitator on how to add/remove members before starting.

    Only the facilitator who initiated the fractal can interact with these buttons.
    The view times out after 120 seconds if no action is taken.

    Attributes:
        cog: The Fractal cog instance that manages active groups and bot state.
        members (list[discord.Member]): The list of members who will participate.
        facilitator (discord.Member): The user who initiated the fractal and has
            exclusive control over the confirmation buttons.
        custom_name (str | None): An optional custom name for the fractal group
            (currently unused in favor of the modal-based naming flow).
        awaiting_modification (bool): Flag set to True when the facilitator clicks
            "Modify Members", signaling the cog to listen for add/remove commands.
    """
    def __init__(self, cog, members, facilitator, custom_name=None):
        """Initialize the confirmation view.

        Args:
            cog: The Fractal cog instance.
            members (list[discord.Member]): Initial list of group members.
            facilitator (discord.Member): The user who will control this view.
            custom_name (str | None): Optional custom name for the group.
        """
        super().__init__(timeout=120)  # Auto-expire after 2 minutes of inactivity
        self.cog = cog
        self.members = members
        self.facilitator = facilitator
        self.custom_name = custom_name
        self.awaiting_modification = False  # Toggled when facilitator requests member changes

    @discord.ui.button(label="✅ Start Fractal", style=discord.ButtonStyle.success)
    async def confirm_members(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle the 'Start Fractal' button click by presenting the naming modal.

        Only the facilitator is allowed to start the fractal. All other users
        receive an ephemeral rejection message.

        Args:
            interaction (discord.Interaction): The button click interaction.
            button (discord.ui.Button): The button instance that was clicked.
        """
        # Gate access: only the facilitator who created this view can proceed
        if interaction.user != self.facilitator:
            await interaction.response.send_message("Only the facilitator can start the fractal.", ephemeral=True)
            return

        # Present the naming modal; on_submit will handle thread creation and startup
        modal = FractalNameModal(self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="❌ Modify Members", style=discord.ButtonStyle.secondary)
    async def modify_members(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle the 'Modify Members' button click by enabling member modification mode.

        Sends the facilitator instructions on how to add or remove members, then
        sets the awaiting_modification flag so the cog knows to process subsequent
        messages as member list changes rather than normal chat.

        Only the facilitator is allowed to modify the member list.

        Args:
            interaction (discord.Interaction): The button click interaction.
            button (discord.ui.Button): The button instance that was clicked.
        """
        # Gate access: only the facilitator can modify the member list
        if interaction.user != self.facilitator:
            await interaction.response.send_message("Only the facilitator can modify members.", ephemeral=True)
            return

        await interaction.response.send_message(
            "**To modify members:**\n"
            "• Remove people: `@username @username`\n"
            "• Add people: `@username @username`\n"
            "• Then click ✅ to start",
            ephemeral=True
        )
        # Signal the cog to interpret the facilitator's next messages as member modifications
        self.awaiting_modification = True

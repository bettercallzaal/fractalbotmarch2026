"""
Central configuration for the ZAO Fractal Discord Bot.

All magic numbers, Discord snowflake IDs, and tunable parameters live here
so they can be adjusted without touching business logic.  Values are imported
by multiple cogs (proposals, fractals, wallet, guide, etc.) and by the main
bot entry point.

Convention:
    - Constants are UPPER_SNAKE_CASE.
    - Discord snowflake IDs are plain integers (not strings).
    - No runtime logic or imports beyond the standard library belong here;
      this module is purely declarative.
"""

# ---------------------------------------------------------------------------
# Role-based Permissions
# ---------------------------------------------------------------------------
# Discord role ID that gates admin-only slash commands (e.g. /admin_close_proposal,
# /admin_recover_proposals).  Only members with this role can invoke protected
# commands; all other users receive a permission-denied error.
SUPREME_ADMIN_ROLE_ID = 1142290553933938748

# ---------------------------------------------------------------------------
# Fractal Group Settings
# ---------------------------------------------------------------------------
# Bounds on how many participants can join a single fractal group.
# Groups smaller than MIN or larger than MAX are rejected by the bot.
MAX_GROUP_MEMBERS = 6   # Fractal consensus works best with at most 6 people
MIN_GROUP_MEMBERS = 2   # A minimum of 2 is needed to form a meaningful ranking

# ---------------------------------------------------------------------------
# Voting / Ranking Settings
# ---------------------------------------------------------------------------
# Levels count down during consensus ranking: 6 (best) -> 1 (lowest).
# The facilitator assigns Level 6 first (most impactful contribution) and
# works down to Level 1.  These bounds control the voting loop in the
# fractal cog.
STARTING_LEVEL = 6  # Highest rank awarded (most Respect)
ENDING_LEVEL = 1    # Lowest rank awarded (least Respect)

# ---------------------------------------------------------------------------
# UI Settings
# ---------------------------------------------------------------------------
# Cycling button colour palette for interactive Discord views.
# When there are more participants than colours, the palette wraps around.
# Strings must match valid ``discord.ButtonStyle`` names.
BUTTON_STYLES = [
    'primary',   # Blue  -- maps to discord.ButtonStyle.primary
    'success',   # Green -- maps to discord.ButtonStyle.success
    'danger',    # Red   -- maps to discord.ButtonStyle.danger
    'secondary'  # Gray  -- maps to discord.ButtonStyle.secondary
]

# ---------------------------------------------------------------------------
# Respect Points (Year 2 = 2x Fibonacci)
# ---------------------------------------------------------------------------
# Respect tokens awarded by rank position.  Index 0 = 1st place (Level 6),
# index 5 = 6th place (Level 1).  The distribution uses a doubled Fibonacci
# sequence to incentivise top contributors while still rewarding participation.
RESPECT_POINTS = [110, 68, 42, 26, 16, 10]

# ---------------------------------------------------------------------------
# Thread Settings
# ---------------------------------------------------------------------------
# Prefix prepended to every fractal discussion thread name.
# The full thread title follows the pattern: "ZAO Fractal: <group members>".
# This makes fractal threads easy to identify in the channel thread list.
THREAD_PREFIX = "ZAO Fractal:"

# ---------------------------------------------------------------------------
# Channel IDs (Discord snowflakes)
# ---------------------------------------------------------------------------
# Channel where member introductions are posted and cached.  The bot reads
# this channel on startup to build an in-memory map of member display names
# for use in fractal sessions and the leaderboard.
INTROS_CHANNEL_ID = 1145135336477950053

# ---------------------------------------------------------------------------
# Proposal Settings
# ---------------------------------------------------------------------------
# Allowed proposal categories shown in the /propose command's autocomplete
# choices.  Each type may trigger different validation or display logic in the
# proposals cog.
PROPOSAL_TYPES = ['text', 'governance', 'funding', 'curate']

# Maximum number of selectable options on a governance proposal ballot.
# Keeps ballots manageable for voters and avoids embed size limits.
MAX_PROPOSAL_OPTIONS = 5

# Dedicated channel where proposal threads and the active-proposals index
# message live.  The bot posts and pins an index embed here.
PROPOSALS_CHANNEL_ID = 1473782633384116397

# Channel the bot listens to for fractal-related commands (/zaofractal, etc.).
# Commands issued outside this channel are rejected with a friendly redirect.
FRACTAL_BOT_CHANNEL_ID = 1389323864751870122

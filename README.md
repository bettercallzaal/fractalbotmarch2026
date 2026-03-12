# ZAO Fractal Bot

A comprehensive Discord bot for running ZAO Fractal — a fractal democracy system where small groups present their contributions, reach consensus on rankings through structured voting, and earn onchain Respect tokens on Optimism.

Based on the [Respect Game](https://edenfractal.com/fractal-decision-making-processes) pioneered by Eden Fractal and Optimism Fractal.

## What It Does

ZAO Fractal Bot is the complete toolchain for running weekly fractal governance meetings inside Discord. It handles every stage of the process:

- **Meeting setup** — Split participants into small groups, assign facilitators, move members into voice rooms
- **Presentations** — Interactive timer system with speaker queue, countdowns, reactions, and intro previews so group members can get to know each other
- **Voting** — Structured multi-round voting (Level 6 down to Level 1) with colored button UI, public vote announcements, and automatic winner detection
- **Results & onchain submission** — Final rankings posted with Respect points earned and a one-click link to submit results to the ZAO Respect smart contract on Optimism
- **Governance** — Proposal system with Respect-weighted voting, threaded discussion, and 7-day auto-expiry
- **Member management** — Wallet registration (with ENS resolution), intro caching, hat-based role sync, leaderboards, and fractal history tracking

## Full Meeting Userflow

Here's the complete flow for running a weekly ZAO Fractal meeting from start to finish:

### Phase 1: Gathering (Before the Meeting)

1. Members join the **Fractal Waiting Room** voice channel
2. Members should have already:
   - Registered their wallet with `/register <wallet or ENS>` (needed for onchain submission)
   - Posted an introduction in the #intros channel (shown during presentations)

### Phase 2: Randomization (Admin)

3. Admin runs **`/randomize`** to split waiting room members into fractal rooms
   - Members are evenly distributed across fractal-1, fractal-2, etc. (max 6 per room)
   - Optional: pre-assign facilitators with `facilitator_1` through `facilitator_6` parameters
   - Bot automatically moves members into their assigned voice rooms via Discord

### Phase 3: Presentations (Each Group)

4. Facilitator runs **`/timer`** in the group's text channel
   - Bot detects all non-bot members in the facilitator's voice channel
   - A rich embed appears with the first speaker's name, a countdown timer, and a "Meet Your Group" section showing 2-line intro previews for each member
   - Members missing intros are @mentioned with a prompt to post one
5. **Each speaker presents** for their allotted time (default 4 minutes)
   - **Interactive controls** available to everyone:
     - **I'm Done** — End your turn early
     - **Skip** — Skip the current speaker
     - **Come Back** — Defer to end of queue, come back after everyone else
     - **+1 Min** — Add extra time
     - **Hand** — Raise/lower hand with notification to speaker
     - **Pause/Resume** — Pause the countdown
     - **Pick Next** — Dropdown to reorder the queue
   - **Reactions**: Fire, Clap — stackable counters displayed live on the embed
   - **Time warnings** at 1 minute and 30 seconds (embed turns red, speaker pinged)
6. When all speakers finish, embed updates to "Presentations Complete — Ready to start voting!"

### Phase 4: Fractal Voting (Each Group)

7. Facilitator runs **`/zaofractal`** to create the voting session
   - Bot shows a confirmation embed with all members, their wallet status, and intro status
   - Facilitator clicks **Start Fractal** and enters the fractal number and group number in a popup modal
   - Bot creates a dedicated thread (e.g. "Fractal 5 - Group 2") and adds all members
8. **Voting begins at Level 6** (highest rank)
   - Bot posts a voting message with colored buttons — one per candidate
   - Bot joins voice to play a level-specific audio ping and posts a link to the voting thread in voice chat
   - Each member clicks the button of who they think contributed most
   - Votes are announced publicly in the thread ("New Vote: @user voted for @candidate")
   - Members can change their vote at any time
   - When a candidate reaches the vote threshold (majority), they win the round
9. **Voting continues down through Level 5, 4, 3, 2, 1**
   - Each round's winner is removed from the candidate pool
   - Ties are broken by random selection
   - Bot plays ascending-pitch audio for each level

### Phase 5: Results & Onchain Submission

10. When all levels are decided, the bot posts **final results** showing:
    - Rankings with medal emojis and Respect points earned per member
    - A pre-filled link to [zao.frapps.xyz/submitBreakout](https://zao.frapps.xyz/submitBreakout) with all wallet addresses
    - @mentions everyone to go vote and confirm results onchain
11. Results are also posted to **#general** as a rich embed with the submit link
12. Results are automatically logged to **fractal history** for stats tracking

### Respect Points (Year 2 — 2x Fibonacci)

| Rank | Level | Respect |
|------|-------|---------|
| 1st  | 6     | 110     |
| 2nd  | 5     | 68      |
| 3rd  | 4     | 42      |
| 4th  | 3     | 26      |
| 5th  | 2     | 16      |
| 6th  | 1     | 10      |

## Commands

### Everyone

| Command | Description |
|---------|-------------|
| `/zaofractal [name]` | Start a fractal from your voice channel. Optional custom name. |
| `/endgroup` | End your fractal (facilitator only) |
| `/status` | Check fractal status (use in fractal thread) |
| `/groupwallets` | Show wallet addresses for all group members |
| `/register <wallet or ENS>` | Link your Ethereum wallet or ENS name (e.g. `vitalik.eth`) |
| `/wallet` | Show your linked wallet |
| `/guide` | Learn how ZAO Fractal works (with link to full web guide) |
| `/intro <@user>` | Look up a member's introduction from #intros |
| `/propose <title> <description> [type] [amount]` | Create a proposal for community voting |
| `/curate <project> [description] [image]` | Nominate a project for the ZAO Fund (yes/no vote) |
| `/proposals` | List all active proposals |
| `/proposal <id>` | View details and vote breakdown for a proposal |
| `/leaderboard` | View top 10 onchain Respect balances inline in Discord |
| `/randomize [facilitator_1..6]` | Split Fractal Waiting Room members into fractal rooms (max 6 per room) |
| `/timer [minutes] [shuffle]` | Start an interactive presentation timer for voice channel members |
| `/timer_add [minutes]` | Add extra time to the current speaker |
| `/history [query]` | Search completed fractals by member, group, or fractal number |
| `/mystats [@user]` | View cumulative fractal stats and Respect earned |
| `/rankings` | View cumulative Respect rankings from fractal history |
| `/hats` | View the ZAO Hats Protocol tree structure |
| `/hat <name>` | View details about a specific hat |
| `/myhats [@user]` | See which ZAO hats you or another member wear |
| `/claimhat` | Get a link to claim a hat on the Hats Protocol app |

### Supreme Admin Only

| Command | Description |
|---------|-------------|
| `/admin_register <user> <wallet or ENS>` | Register wallet or ENS for another user |
| `/admin_wallets` | List all wallet registrations + stats |
| `/admin_lookup <user>` | Look up a user's wallet |
| `/admin_match_all` | Auto-match server members to wallets by display name |
| `/admin_refresh_intros` | Rebuild intro cache from #intros channel history |
| `/admin_close_proposal <id>` | Close voting on a proposal and post results |
| `/admin_delete_proposal <id>` | Delete a proposal entirely |
| `/admin_reopen_proposal <id>` | Reopen a closed proposal with fresh 7-day window |
| `/admin_recover_proposals` | Scan #proposals for threads missing from database and recover them |
| `/admin_end_fractal [thread_id]` | Force end any fractal |
| `/admin_list_fractals` | List all active fractals |
| `/admin_cleanup` | Clean up stuck/old fractals |
| `/admin_force_round <thread_id>` | Skip voting, advance to next round |
| `/admin_reset_votes <thread_id>` | Clear all votes in current round |
| `/admin_declare_winner <thread_id> <user>` | Manually declare a round winner |
| `/admin_add_member <thread_id> <user>` | Add someone to an active fractal |
| `/admin_remove_member <thread_id> <user>` | Remove someone from an active fractal |
| `/admin_change_facilitator <thread_id> <user>` | Transfer facilitator role |
| `/admin_pause_fractal <thread_id>` | Pause voting |
| `/admin_resume_fractal <thread_id>` | Resume voting |
| `/admin_restart_fractal <thread_id>` | Restart from Level 6 with same members |
| `/admin_fractal_stats <thread_id>` | Detailed stats for a fractal |
| `/admin_server_stats` | Server-wide fractal statistics |
| `/admin_export_data [thread_id]` | Export fractal data as JSON file |
| `/admin_link_hat <hat_name> <hat_id> <role>` | Link a hat to a Discord role |
| `/admin_unlink_hat <hat_name>` | Remove a hat-to-role mapping |
| `/admin_hat_roles` | List all hat-to-role mappings |
| `/admin_sync_hats` | Manually trigger hat-to-role sync |

## Introduction Lookup

The bot integrates with the #intros channel in two ways:

### Manual Lookup (`/intro @user`)
- **Cached** — Intros are fetched once from channel history and cached in `data/intros.json`
- **Rich embed** — Shows intro text, link to their [thezao.com](https://thezao.com) community page, and wallet address if registered
- **Admin refresh** — `/admin_refresh_intros` rebuilds the entire cache from channel history

### Auto Intros on `/timer`
When `/timer` starts, the bot automatically sends a **"Meet Your Group"** embed to the channel before the timer begins:
- **1-2 line preview** of each member's intro with a **[read more]** link that jumps to the full intro message in #intros
- **Missing intro prompt** — Members without intros are @mentioned: "Post your intro in #intros so your group can get to know you!"
- This helps group members get to know each other before presentations begin

## Proposal & Curation System

Community proposals and project curation with threaded discussion and Respect-weighted voting:

- **`/propose`** — Create a proposal (Text, Governance, or Funding type)
  - **Text/Funding** — Yes / No / Abstain voting buttons
  - **Governance** — Custom options entered via modal (up to 5 choices)
  - Each proposal gets its own discussion thread in the dedicated #proposals channel
- **`/curate`** — Quick yes/no vote for project curation (e.g. Artizen Fund projects)
  - Accepts a project name or URL — auto-extracts name from URL slugs
  - Optional `description` and `image` parameters for richer embeds
  - Best-effort Open Graph scraper auto-fills title, description, and thumbnail from project URLs
  - Clickable embed title links directly to the project page
- **Thread visibility** — All proposal threads are created in the dedicated #proposals channel so everyone can see and vote
- **#general notifications** — New proposals post a notification to #general with a clickable link to #proposals
- **7-day auto-expiry** — Proposals automatically close after 7 days with final results posted to the thread
- **Live vote tallies** — Proposal embeds auto-update after each vote with progress bars showing weighted results
- **Proposals channel index** — Pinned active proposals list auto-updates on create/close
- **Transparent voting** — Each proposal embed shows every voter's name, choice, and Respect weight
- **Public vote confirmations** — When someone votes, a public message is posted to the proposal thread with vote details and time remaining
- **Voting window** — Embeds show creation date, closing date, and dynamic time remaining (e.g. "4d 12h remaining")
- **Persistent votes** — Voting buttons survive bot restarts (per-proposal button IDs, auto-migrated on startup)
- **Vote changes** — Members can change their vote at any time while the proposal is active
- **Respect-weighted** — Vote power = your total onchain Respect (OG + ZOR). Must hold Respect tokens and have a registered wallet to vote.
- **Admin controls** — Close voting to post final results, or delete proposals entirely

## Respect Leaderboard

### Discord (`/leaderboard`)
- Queries all 131 wallets' OG + ZOR balances via raw JSON-RPC `eth_call`
- Shows **top 10 inline** in Discord with name, total Respect, and OG/ZOR breakdown
- 5-minute cache for fast responses
- Links to the full web leaderboard at [thezao.com/zao-leaderboard](https://www.thezao.com/zao-leaderboard)

### Web ([zao-fractal.vercel.app/leaderboard](https://zao-fractal.vercel.app/leaderboard))
- Multicall3 batch queries across 130+ member wallets
- Searchable, sortable table with top-3 medal highlights
- Member names link to individual profile pages

## Hats Protocol Integration

The bot integrates with [Hats Protocol](https://www.hatsprotocol.xyz/) for role-based governance:

- **`/hats`** — View the ZAO Hats tree structure showing all roles and wearers
- **`/hat <name>`** — View details about a specific hat (description, wearers, eligibility)
- **`/myhats`** — See which hats you wear based on your registered wallet
- **`/claimhat`** — Get a link to claim eligible hats on the Hats Protocol app
- **Admin hat-role sync** — Link onchain hats to Discord roles for automatic syncing

## Offchain Respect Dashboard

Web dashboard replacing Airtable for tracking all three Respect types:

### Public Pages
- **`/respect`** — Combined leaderboard showing OG Respect (ERC-20), ZOR Respect (ERC-1155), and offchain fractal Respect per member with stat cards and sortable table
- **`/members/[slug]`** — Individual member profiles with fractal history, contribution log, and Respect breakdown

### Admin Dashboard (`/admin`)
- **Discord OAuth** — Login with Discord, requires Supreme Admin role
- **Members tab** — Searchable roster of all members with wallet and intro status
- **Contributions tab** — Log contributions (intro, attendance, special) that earn OG Respect
- **Allocations tab** — Queue of pending OG Respect distributions, mark as distributed or cancelled

### API Routes

| Route | Method | Auth | Purpose |
|-------|--------|------|---------|
| `/api/dashboard/leaderboard` | GET | Public | Combined leaderboard (onchain + offchain) |
| `/api/dashboard/stats` | GET | Public | Aggregate stats for header cards |
| `/api/dashboard/members/[slug]` | GET | Public | Full member profile data |
| `/api/dashboard/activity` | GET | Public | Recent activity feed |
| `/api/admin/contributions` | GET/POST | Admin | List/create contributions |
| `/api/admin/allocations` | GET/POST | Admin | List/create allocations |
| `/api/admin/allocations/[id]` | PATCH | Admin | Mark distributed/cancelled |
| `/api/admin/members` | GET | Admin | Full member roster with status |

## Fractal History & Stats

Every completed fractal is automatically logged to `data/history.json`:

- **`/history [query]`** — Search past fractals by member name, group name, or fractal number
- **`/mystats [@user]`** — View cumulative Respect earned, participation count, podium finishes, and recent fractals
- **`/rankings`** — Cumulative Respect leaderboard from all recorded fractal history
- Auto-records rankings, Respect points, facilitator, fractal/group number, and timestamp

## Room Randomizer

Run `/randomize` to split members from the Fractal Waiting Room voice channel into fractal rooms:

- **Even distribution** — Members split evenly across fractal-1, fractal-2, etc. (max 6 per room)
- **Facilitator assignment** — Optional `facilitator_1` through `facilitator_6` parameters to pre-assign facilitators to each room (facilitators are moved first)
- **Auto-move** — Bot moves members into their assigned rooms via Discord voice

## Presentation Timer

Run `/timer` before `/zaofractal` to give each member structured speaking time. This is the introduction/presentation phase where members share what they've been working on.

- **Auto-detects speakers** from your voice channel (1-6 members)
- **4-minute default** — Configurable 1-30 minutes per speaker via `minutes` parameter
- **Anyone can control** — All buttons available to everyone, not just the facilitator
- **Single consolidated embed** — One rich embed tracks speaker, countdown, reactions, queue, and intro previews
- **Interactive controls** (Row 1): I'm Done, Skip, Come Back, +1 Min, Hand
- **Timer controls + reactions** (Row 2): Pause/Resume, Stop, Fire, Clap
- **Pick Next dropdown** (Row 3): Select menu to reorder the queue and pick who goes next
- **Stackable emoji reactions** — Every click adds +1 (not a toggle), shown as live counters on the embed
- **Raise Hand** — Toggle hand raise with notification to the current speaker
- **Come Back** — Defers current speaker to the end of the queue
- **I'm Done** — Current speaker ends their turn early
- **Time warnings** — Embed color changes at 1-minute (yellow) and 30-second (red) marks with speaker ping
- **"Meet Your Group" section** — Shows a compact 2-line intro preview for each member (fetched from #intros cache) and @mentions anyone missing an intro
- **`/timer_add [minutes]`** — Add extra minutes to the current speaker mid-presentation
- **Shuffle option** — Randomize speaker order with `shuffle: True`
- When all speakers finish, embed updates to "Presentations Complete — Ready to begin voting!"

## Wallet System

The bot maps Discord users to Ethereum wallet addresses for onchain submission:

- **`/register`** — Users self-register their wallet address or ENS name (e.g. `vitalik.eth` → auto-resolves to `0x...` using Keccak-256 namehash)
- **Name matching** — 130+ pre-loaded name→wallet mappings in `data/names_to_wallets.json` auto-match by Discord display name
- **Admin override** — Admins can register wallets or ENS names for any user with `/admin_register`
- **`/admin_match_all`** — Shows which server members already have wallets matched

When a fractal completes, the bot generates a pre-filled `zao.frapps.xyz/submitBreakout` link with all ranked wallet addresses and @mentions everyone to go vote.

## Voice Channel Notifications

Each voting round, the bot:
- **Sends a link** to the voting thread in the voice channel's text chat so members can click through
- **Plays a level-specific sound** — Different ascending-pitch audio for each voting level (level1.mp3 through level6.mp3)
- **Stays connected for 5 minutes** — Bot remains in voice between rounds to avoid repeated join/leave spam, with auto-disconnect after 5 minutes of inactivity

## Project Structure

```
fractalbotfeb2026/
├── main.py                    # Bot entry point
├── requirements.txt           # Python dependencies
├── config/
│   ├── config.py              # Settings (roles, levels, respect points, channels)
│   └── .env.template          # Environment variable template
├── assets/
│   ├── ping.mp3               # Default audio notification
│   ├── level1.mp3             # Level 1 voting sound (lowest pitch)
│   ├── level2.mp3             # Level 2 voting sound
│   ├── level3.mp3             # Level 3 voting sound
│   ├── level4.mp3             # Level 4 voting sound
│   ├── level5.mp3             # Level 5 voting sound
│   └── level6.mp3             # Level 6 voting sound (highest pitch)
├── cogs/
│   ├── base.py                # Shared utilities (voice check, role check)
│   ├── guide.py               # /guide + /leaderboard (inline top 10)
│   ├── intro.py               # /intro command with cached #intros lookup
│   ├── proposals.py           # Proposal + curation voting system
│   ├── history.py             # Fractal history tracking + search
│   ├── timer.py               # Presentation timer with speaker queue
│   ├── wallet.py              # Wallet + ENS registration (Keccak-256)
│   ├── hats.py                # Hats Protocol tree + role sync
│   └── fractal/
│       ├── __init__.py
│       ├── cog.py             # Slash commands (48 total)
│       ├── group.py           # Core voting logic + voice notifications
│       └── views.py           # Discord button UIs + naming modal
├── utils/
│   ├── logging.py             # Color-coded logging
│   └── web_integration.py     # Webhook notifications to web dashboard
├── data/
│   ├── wallets.json           # Discord ID → wallet mappings
│   ├── names_to_wallets.json  # Name → wallet mappings (pre-loaded)
│   ├── intros.json            # Cached #intros channel messages
│   ├── proposals.json         # Proposal + curation data + votes
│   └── history.json           # Completed fractal results log
└── web/                       # Next.js web app (Vercel)
    ├── pages/
    │   ├── index.tsx          # Landing page
    │   ├── guide.tsx          # Full guide / slide deck (public)
    │   ├── leaderboard.tsx    # Respect leaderboard (public)
    │   ├── respect.tsx        # Combined Respect dashboard (public)
    │   ├── members/[slug].tsx # Member profile pages (public)
    │   ├── admin/index.tsx    # Admin dashboard (Supreme Admin only)
    │   └── api/
    │       ├── leaderboard.ts       # Onchain balance API (Multicall3)
    │       ├── auth/[...nextauth].ts # Discord OAuth + admin role check
    │       ├── dashboard/           # Public dashboard APIs
    │       │   ├── leaderboard.ts   # Combined leaderboard
    │       │   ├── stats.ts         # Aggregate stats
    │       │   ├── activity.ts      # Activity feed
    │       │   └── members/[slug].ts # Member profiles
    │       └── admin/               # Admin APIs (role-gated)
    │           ├── members.ts       # Member roster
    │           ├── contributions.ts # Log contributions
    │           └── allocations/     # OG Respect distributions
    │               ├── index.ts     # List/create
    │               └── [id].ts      # Update status
    ├── components/
    │   ├── ui/                # Radix UI components
    │   └── layout/
    │       └── DashboardLayout.tsx # Shared navbar + footer
    ├── types/
    │   ├── next-auth.d.ts     # Extended session types
    │   └── dashboard.ts       # Dashboard interfaces
    └── utils/
        ├── database.ts        # Drizzle + Neon Postgres
        ├── schema.ts          # DB schema (users, fractals, contributions, allocations)
        ├── admin.ts           # Discord role check + requireAdmin middleware
        ├── loadJsonData.ts    # Read bot JSON files with path fallbacks
        ├── respectCache.ts    # Multicall3 onchain balance cache
        └── cn.ts              # Tailwind class merge utility
```

## Setup

### Requirements
- Python 3.10+
- Discord bot token with Message Content, Members, and Guilds intents
- ffmpeg (for voice channel audio pings) — `brew install ffmpeg` on macOS

### Install & Run (Local)

```bash
pip install -r requirements.txt
cp config/.env.template .env
# Edit .env with your DISCORD_TOKEN
python3 main.py
```

### Web App (Local)

```bash
cd web
npm install
cp .env.example .env.local
# Edit .env.local with database URL, Discord OAuth credentials, and RPC key
npm run dev
```

### Deploy to Bot-Hosting.net

1. Create a Discord bot server at [bot-hosting.net](https://bot-hosting.net)
2. In the **Files** tab, upload all project files directly into `/home/container/` (no subfolders — `main.py` must be at the root)
3. Upload your `.env` file separately with your bot token
4. In the **Startup** tab, set **App py file** to `main.py`
5. Hit **Start** — dependencies install automatically from `requirements.txt`

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `DEBUG` | No | Set to `TRUE` for verbose logging |
| `WEB_WEBHOOK_URL` | No | Webhook URL for web dashboard |
| `WEBHOOK_SECRET` | No | Secret for webhook auth |
| `ALCHEMY_OPTIMISM_RPC` | For leaderboard | Alchemy RPC URL for Optimism |
| `DISCORD_CLIENT_ID` | For web auth | Discord OAuth client ID |
| `DISCORD_CLIENT_SECRET` | For web auth | Discord OAuth client secret |
| `NEXTAUTH_SECRET` | For web auth | NextAuth session secret |
| `DATABASE_URL` | For web dashboard | Neon Postgres connection string |

## Onchain Integration

- **Respect Contract**: Soulbound ERC-1155 on Optimism via [ORDAO](https://optimismfractal.com/council)
- **OG Respect (ERC-20)**: `0x34cE89baA7E4a4B00E17F7E4C0cb97105C216957`
- **ZOR Respect (ERC-1155)**: `0x9885CCeEf7E8371Bf8d6f2413723D25917E7445c`
- **Submit UI**: [zao.frapps.xyz/submitBreakout](https://zao.frapps.xyz/submitBreakout)
- **Toolkit**: [Optimystics/frapps](https://github.com/Optimystics/frapps)

## v1.1 Changelog

### Bug Fixes
- Fix double-delete KeyError crash in `/endgroup` when ending a fractal
- Fix `/pause` not actually blocking votes — votes now rejected while paused
- Fix debug info leak exposing active thread IDs in `/status` error messages
- Fix ENS namehash using wrong hash algorithm (NIST SHA-3 → Keccak-256)

### Bot Enhancements
- **Inline leaderboard** — `/leaderboard` now shows top 10 onchain Respect balances directly in Discord (OG + ZOR breakdown per member)
- **Proposal thread visibility** — All proposal threads now created in dedicated #proposals channel (visible to everyone, not just the creator)
- **Project name extraction** — `/curate` extracts clean names from URL slugs and OG meta tags instead of showing raw URLs
- **#general notifications** — New proposals post a notification to #general with a clickable link to #proposals
- **7-day auto-expiry** — Proposals automatically close after 7 days with final vote results posted to the thread
- **Hats Protocol** — New `/hats`, `/hat`, `/myhats`, `/claimhat` commands + admin hat-to-role sync

### Web Dashboard (New)
- **`/respect`** — Combined Respect dashboard with OG, ZOR, and offchain leaderboard
- **`/members/[slug]`** — Member profile pages with fractal history and Respect breakdown
- **`/admin`** — Admin dashboard with contribution logging, allocation tracking, and member roster
- **8 new API routes** — Dashboard stats, leaderboard, activity feed, member profiles, admin CRUD
- **Discord OAuth** — Login with Discord, Supreme Admin role check for admin pages
- **DB schema** — New `contributions` and `respect_allocations` Postgres tables

### Infrastructure
- Updated `.gitignore` for build artifacts and zip files
- 48 total slash commands (up from ~30)
- Production deployment on bot-hosting.net

## v1.2 Changelog

### Proposal Voting Fixes
- **Fix "Interaction failed" on proposals** — Vote buttons used a shared `custom_id` across all proposals, so after a bot restart only the last-registered proposal worked. Now each proposal gets unique button IDs (`proposal_yes_1`, `proposal_no_2`, etc.)
- **Auto-migrate buttons on startup** — On boot, the bot waits until connected then re-edits every active proposal message with updated buttons, so existing proposals work without resubmitting
- **Fix HTML entities in scraped titles** — OG-scraped project names (e.g. `&quot;Profundo&quot;`) are now properly decoded

### Transparency & UX
- **Voter breakdown on embeds** — Every proposal embed now shows a "Votes Cast" section listing each voter by name, their choice, and Respect weight
- **Public vote confirmations** — After each vote, a public message is posted to the proposal thread: "Vote accepted from @user (3,364 Respect) — 4d 12h remaining"
- **Voting window timestamps** — Proposal embeds show when voting opened, when it closes, and dynamic time remaining (e.g. "4d 12h remaining")
- **Time remaining on listings** — `/proposals` command and the proposals channel index now show time remaining per proposal

## v1.3 Changelog

### Proposal System Fixes
- **Fix expiry loop crash** — The hourly auto-expiry task would permanently die if any proposal's thread was inaccessible. Each proposal is now wrapped in its own try/except so one failure doesn't kill the entire loop.
- **Fix timezone-aware datetime crash** — Proposals created on certain server configurations stored timezone-aware timestamps, causing `can't subtract offset-naive and offset-aware datetimes` errors. All datetime parsing now strips timezone info.
- **Startup catch-up expiry** — On boot, the bot immediately closes any proposals past their 7-day window instead of waiting for the hourly loop. Prevents stale proposals from accumulating between restarts.
- **Channel fetch fallback** — Expiry and migration now use `fetch_channel()` as fallback when `get_channel()` returns None (stale cache after restart).

### New Admin Commands
- **`/admin_recover_proposals`** — Scans the #proposals channel for threads that exist in Discord but are missing from `proposals.json`. Parses the bot's embed to extract title, description, type, author, and timestamps, then re-adds them to the database and re-registers voting buttons. Handles both active and recently archived threads.
- **`/admin_reopen_proposal <id>`** — Reopens a closed proposal with a fresh 7-day voting window. Re-attaches voting buttons and updates the embed.

### UX Improvements
- **Paginated `/proposals`** — Now accepts an optional `page` parameter (10 proposals per page) so it never hits Discord's embed field limits. Shows total count and page navigation hint.
- **Title truncation** — Long proposal titles are truncated in the `/proposals` listing to stay within Discord's 256-char field name limit.

### Infrastructure
- 51 total slash commands (up from 48)

## v1.4 Changelog

### New Commands
- **`/randomize`** — Split members from Fractal Waiting Room into fractal rooms (max 6 per room, evenly distributed). Supports optional `facilitator_1` through `facilitator_6` parameters to pre-assign facilitators to rooms.

### Presentation Timer Overhaul
- **4-minute default** — Changed from 3 to 4 minutes per speaker
- **Anyone can control** — Removed facilitator-only restriction; all buttons open to everyone
- **Interactive button layout** — I'm Done, Skip, Skip & Come Back, +1 Min, Raise Hand, Pause/Resume, Stop, Fire/Clap reactions, Pick Next dropdown
- **Stackable emoji reactions** — Every click increments a counter (not a toggle). Live reaction bar displayed on embed.
- **Raise Hand** — Toggle hand raise with notification sent to current speaker
- **Skip & Come Back** — Defers current speaker to end of queue, comes back after everyone else
- **I'm Done** — Current speaker ends their turn early
- **Pick Next** — Select dropdown to reorder the queue and jump to any upcoming or skipped speaker
- **Intro previews on start** — Compact 2-line intro preview per member when timer starts, with @mentions for members missing intros

### Voice Channel Improvements
- **Bot stays connected 5 minutes** — Instead of disconnecting and reconnecting each voting round, bot stays in voice for 5 minutes with auto-disconnect timer
- **Level-specific sounds** — Different ascending-pitch audio files for each voting level (level1.mp3 through level6.mp3)

### `/zaofractal` Enhancements
- **Intro status check** — Shows wallet + intro status per member in the confirmation embed before starting

### Bug Fixes
- **Fix duplicate timer messages** — Timer command could execute twice due to HTTPException catch allowing continuation; now returns immediately on failed defer
- **Fix multiple countdown tasks** — Advance and resume now cancel the previous countdown before starting a new one, preventing duplicate warnings and double-advances
- **Fix Move Members permission** — Added `move_members=True` and `connect=True` to bot invite link permissions for `/randomize`

### Known Issues (Audit)
- **No duplicate prevention on `/zaofractal`** — Rapid double-click could create two fractal threads (needs lock guard)
- **Bare `except` clauses** — Several locations in fractal cog silently swallow errors instead of logging them
- **Uncancelled voice disconnect task** — `_voice_disconnect_task` not cancelled when fractal ends
- **Proposal expiry race condition** — Startup catchup and hourly loop could double-process the same proposal
- **Synchronous JSON I/O** — All data persistence uses blocking `json.load()/dump()`, could block event loop on large files
- **No rate limiting on `/propose`** — Users can spam proposals without cooldown
- **Proposal index embed overflow** — Could exceed Discord's 6000 char limit with many active proposals

## v1.5 Changelog

### Critical Bug Fix — Duplicate Command Messages
- **Root cause identified** — Slash commands were sending 2-5x duplicate responses due to three compounding issues:
  1. **Stale guild + global command registrations** — Commands registered both per-guild (instant) and globally caused Discord to dispatch multiple interactions per invocation
  2. **Webhook rate limiting (429)** — `defer()` + `followup.send()` triggers Discord webhook endpoints which rate-limit and retry, each retry creating a duplicate message
  3. **Multiple bot processes** — Accumulated zombie `main.py` processes (up to 21 found) each responded independently
- **Fix: Global-only command sync** — On startup, bot now clears all stale guild command registrations and syncs only globally. One registration per command = one dispatch per invocation.
- **Fix: Bot-wide interaction dedup** — Added `bot.tree.interaction_check` that tracks seen interaction IDs in an LRU cache. Blocks any duplicate dispatch before command handlers run.
- **Fix: `on_ready` guard** — Added `_ready_fired` flag so reconnects don't re-sync commands or re-register handlers
- **Fix: Direct response for fast paths** — Timer error responses (not in voice, already running, etc.) now use `interaction.response.send_message()` instead of `defer()` + `followup.send()`, avoiding the webhook rate-limit retry path entirely
- **Fix: Cog-level dedup** — Added `_InteractionDedup` class in `base.py` with shared instance across all cogs, providing a second layer of duplicate protection

### Timer Consolidation
- **Single timer embed** — Timer previously sent 3 separate messages (announcement + intro previews + timer embed). Now sends 1 clean timer embed for controls/countdown.
- **Separate "Meet Your Group" embed** — Intros are now sent as their own public embed before the timer starts, with 1-2 line previews and [read more] links to the full intro in #intros. Members without intros are @mentioned with a prompt to post one.
- **Edit-only on advance** — Advancing to next speaker now edits the existing embed instead of sending a separate ping message
- **Countdown task management** — `advance()` and `resume()` now cancel the previous countdown task before starting a new one, preventing stacked timers

### Infrastructure
- 52 total slash commands (up from 51)
- Added `test_timer.py` — Minimal 2-command test bot for isolating slash command bugs
- Comprehensive README rewrite with full meeting userflow documentation

## v1.6 Changelog

### Codebase Audit & Fixes
- **Atomic JSON writes** — All 5 data stores (`wallets.json`, `proposals.json`, `history.json`, `intros.json`, `hats_roles.json`) now use `utils/safe_json.atomic_save()` which writes to a temp file then does an atomic `os.replace()`, preventing data corruption on crash or power loss
- **URL injection fix** — Fractal submit URLs now use `urllib.parse.urlencode()` for safe parameter encoding instead of string concatenation
- **Bare except removal** — Replaced all bare `except:` clauses with specific exception types (`discord.NotFound`, `discord.HTTPException`, `ValueError`, etc.) and added logging
- **Concurrency lock** — Added `asyncio.Lock` around `active_groups` dict in the fractal cog to prevent race conditions from rapid double-clicks on `/zaofractal`
- **Timezone consistency** — Replaced all deprecated `datetime.utcnow()` calls with `datetime.now(timezone.utc)` and added `_parse_utc()` helper for safe parsing of mixed timezone-naive/aware ISO strings in legacy data

### Documentation
- **Comprehensive docstrings** — Every Python file now has a module-level docstring, and every class/method has a docstring with Args/Returns sections
- **Inline comments** — Non-obvious logic throughout the codebase is annotated: ABI encoding, vote threshold math, Discord interaction patterns, atomic write strategy, closure-based callbacks, cache TTL behavior, and more
- **2,695 lines of documentation** added across 15 files with zero logic changes

## Next Steps / Roadmap

### v1.6 — Reliability & Polish
- [ ] **Rate limiting on `/propose`** — Add a cooldown (e.g. 1 proposal per user per hour) to prevent spam
- [ ] **Proposal index overflow guard** — Ensure the pinned proposals index embed stays under Discord's 6000-char limit
- [ ] **Vote timeout** — Auto-advance or warn if a voting round goes too long without reaching threshold
- [ ] **Mid-fractal member handling** — Gracefully handle someone leaving voice/Discord mid-fractal (remove from candidates, adjust threshold)
- [ ] **Async JSON I/O** — Move blocking `json.load()`/`json.dump()` calls to `asyncio.to_thread()` so large files don't stall the event loop
- [ ] **Error alerting** — DM the Supreme Admin or post to an admin channel when background tasks (expiry loop, hat sync) encounter errors

### v1.7 — Proposals & Governance
- [ ] **Proposal filtering** — Filter `/proposals` by type (text/governance/funding/curate) and status (active/closed)
- [ ] **Auto-archive** — Move closed proposals to an archive category after 14 days
- [ ] **Proposal reminders** — Ping voters 24 hours before a proposal closes if they haven't voted
- [ ] **Quorum requirements** — Minimum vote count or Respect threshold for a proposal to pass
- [ ] **Web proposals page** — Browse, search, and vote on proposals from the website

### v1.8 — Meeting Experience
- [ ] **Facilitator rotation** — Track who's facilitated before and suggest/auto-assign facilitators fairly across weeks
- [ ] **Scheduled fractals** — `/schedule` command for recurring weekly fractals with Discord event integration and reminders
- [ ] **Multi-group coordination** — "Fractal master" dashboard showing status of all groups running in parallel
- [ ] **Post-meeting summary** — Auto-generate a recap embed after all groups finish (total participants, all rankings, Respect distributed)
- [ ] **Presentation notes** — Let speakers optionally attach a link or short description to their turn for the meeting record

### Future — Onchain & Integrations
- [ ] **Transaction verification** — Listen for onchain tx after submitBreakout and confirm back in Discord with a checkmark
- [ ] **Web voting** — Vote on proposals from the website (Discord OAuth, same Respect weighting)
- [ ] **Snapshot integration** — Cross-post proposals to Snapshot for formal governance votes
- [ ] **POAP / attendance tokens** — Auto-distribute attendance POAPs or tokens to fractal participants
- [ ] **Multi-server support** — Allow the bot to run in multiple Discord servers with per-server config

## Links

- **THE ZAO Discord**: [discord.gg/thezao](https://discord.gg/thezao)
- **Onchain Dashboard**: [zao.frapps.xyz](https://zao.frapps.xyz)
- **Respect Leaderboard**: [thezao.com/zao-leaderboard](https://www.thezao.com/zao-leaderboard)
- **Web Dashboard**: [zao-fractal.vercel.app](https://zao-fractal.vercel.app)
- **Optimism Fractal**: [optimismfractal.com](https://optimismfractal.com)
- **Eden Fractal**: [edenfractal.com](https://edenfractal.com)

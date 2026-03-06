# ZAO Fractal Bot

Discord bot for running ZAO Fractal voting — a fractal democracy system where small groups reach consensus on contribution rankings and earn onchain Respect tokens.

Based on the [Respect Game](https://edenfractal.com/fractal-decision-making-processes) pioneered by Eden Fractal and Optimism Fractal.

## How It Works

1. **Group up** — 2-6 people join a voice channel
2. **Start fractal** — Facilitator runs `/zaofractal`, confirms members, then enters the fractal number and group number via a popup modal
3. **Vote** — Members vote on who contributed most using colored button UI (Levels 6 → 1). Each round, the bot joins voice to play an audio ping and posts a voting link in the voice channel text chat.
4. **Results** — Bot posts a rich embed to the general channel with final rankings, Respect points earned, and a one-click link to submit results onchain
5. **Earn Respect** — Rankings are submitted to the ZAO Respect contract on Optimism via [zao.frapps.xyz](https://zao.frapps.xyz)

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
| `/timer [minutes] [shuffle]` | Start a presentation timer for voice channel members |
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

The `/intro` command lets anyone look up a member's introduction from the #intros channel:

- **Cached** — Intros are fetched once from channel history and cached in `data/intros.json`
- **Rich embed** — Shows intro text, link to their [thezao.com](https://thezao.com) community page, and wallet address if registered
- **Admin refresh** — `/admin_refresh_intros` rebuilds the entire cache from channel history

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

## Presentation Timer

Run `/timer` before voting to give each member structured speaking time:

- **Auto-detects speakers** from your voice channel (2-6 members)
- **Live countdown** using Discord's built-in relative timestamps (updates client-side)
- **Facilitator controls** — Skip, Pause, Resume, and Stop buttons
- **`/timer_add`** — Add extra minutes to the current speaker if needed
- **Shuffle option** — Randomize speaker order with `shuffle: True`
- When all speakers finish, the bot announces "Ready to begin voting!"

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
- **Plays an audio ping** by joining the voice channel, playing a short ding sound, then disconnecting

## Project Structure

```
fractalbotfeb2026/
├── main.py                    # Bot entry point
├── requirements.txt           # Python dependencies
├── config/
│   ├── config.py              # Settings (roles, levels, respect points, channels)
│   └── .env.template          # Environment variable template
├── assets/
│   └── ping.mp3               # Audio notification for voting rounds
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

## Roadmap / Ideas

### Scaling (v1.4)
- [ ] **Proposal filtering** — Filter by type (text/governance/funding/curate) and status (active/closed)
- [ ] **Auto-archive** — Move closed proposals to archive after 14 days
- [ ] **Web proposals page** — Browse and view proposals on the website

### UX Improvements
- [ ] **Vote timeout** — Auto-advance or warn if a round goes too long without reaching threshold
- [ ] **Auto-split into groups** — For larger meetings (7+ people in voice), automatically split into balanced groups of 3-6
- [ ] **Mid-fractal member handling** — Gracefully handle someone leaving voice/Discord mid-fractal
- [ ] **Facilitator rotation** — Track who's facilitated before and suggest/auto-assign facilitators fairly

### Onchain / Web
- [ ] **Transaction verification** — Listen for onchain tx after submitBreakout and confirm back in Discord
- [ ] **Web voting** — Vote on proposals from the website (not just Discord)

### Operational
- [ ] **Scheduled fractals** — `/schedule` command for recurring weekly fractals with reminders
- [ ] **Multi-group coordination** — "Fractal master" view showing status of all groups running in parallel

## Links

- **THE ZAO Discord**: [discord.gg/thezao](https://discord.gg/thezao)
- **Onchain Dashboard**: [zao.frapps.xyz](https://zao.frapps.xyz)
- **Respect Leaderboard**: [thezao.com/zao-leaderboard](https://www.thezao.com/zao-leaderboard)
- **Web Dashboard**: [zao-fractal.vercel.app](https://zao-fractal.vercel.app)
- **Optimism Fractal**: [optimismfractal.com](https://optimismfractal.com)
- **Eden Fractal**: [edenfractal.com](https://edenfractal.com)

# FractalBot + ZAO OS: Next Integration Steps

**Date:** 2026-03-27
**Author:** Zaal + Claude Opus 4.6
**Status:** Strategic planning document

---

## Table of Contents

1. [Current State Summary](#1-current-state-summary)
2. [Opportunities from ZAO OS Research](#2-opportunities-from-zao-os-research)
3. [Opportunities from FractalBot Research](#3-opportunities-from-fractalbot-research)
4. [Top 10 Integration Priorities](#4-top-10-integration-priorities)
5. [Architecture Recommendations](#5-architecture-recommendations)
6. [Quick Wins](#6-quick-wins)

---

## 1. Current State Summary

### What's Integrated Today

As of v2.0, FractalBot and ZAO OS share a single Supabase Postgres database. The bot writes; ZAO OS reads. Here is the complete data flow:

#### Shared Supabase Tables

| Table | Bot Writes | ZAO OS Reads | Data |
|-------|-----------|-------------|------|
| `wallets` | `/register` command upserts discord_id -> wallet mapping | Member profiles, wallet resolution | 53 discord_id entries + 166 name entries |
| `proposals` | `/propose`, `/curate` creates rows; vote buttons update status | `ProposalsTab.tsx` lists proposals with filters | 24 proposals |
| `proposal_votes` | Button clicks upsert respect-weighted votes | Vote tallies displayed on proposal cards | Per-user weighted votes |
| `fractal_sessions` | `/zaofractal` completion inserts session | `SessionsTab.tsx` history, `/fractals` page | 90+ weeks of sessions |
| `fractal_rankings` | Each participant's rank/level/respect written per session | `respect_leaderboard` VIEW powers the leaderboard | Rankings per session |
| `intros` | `/admin_refresh_intros` caches #introductions text | Member profiles, intro display | 6 cached intros |
| `events` | `/schedule`, `/edit_event`, `/cancel_event` | Event calendar display | Recurring weekly events |
| `hats_role_mappings` | Hats sync loop maps hat IDs to Discord roles | Hats tree viewer, role badges | ZAO tree 226 on Optimism |
| `bot_metadata` | Stores proposals index message ID | Internal reference | Key-value config |

#### ZAO OS Components That Read Bot Data

| ZAO OS Component | File | What It Reads |
|-----------------|------|---------------|
| **Proposals Tab** | `src/app/(auth)/fractals/ProposalsTab.tsx` | `proposals`, `proposal_votes` via `/api/proposals` |
| **Sessions Tab** | `src/app/(auth)/fractals/SessionsTab.tsx` | `fractal_sessions`, `fractal_rankings` |
| **Respect Leaderboard** | `src/app/api/respect/leaderboard/route.ts` | On-chain OG + ZOR balances via multicall, `respect_leaderboard` VIEW |
| **Hats Tree** | `src/components/hats/HatManager.tsx` | On-chain Hats tree 226 via `@hatsprotocol/sdk-v1-core` |
| **ZOUNZ Governance** | `src/components/zounz/ZounzProposals.tsx` | Governor contract `0x9d98...17f` on Base |
| **Snapshot Polls** | `src/components/governance/SnapshotPolls.tsx` | Snapshot GraphQL API for `zaal.eth` space |

#### What ZAO OS Also Writes (Independent of Bot)

| Feature | Source | Tables/APIs |
|---------|--------|------------|
| Community proposals (web) | Farcaster-authenticated users | `proposals`, `proposal_votes`, `proposal_comments` |
| Snapshot weekly polls | Admin via `CreateWeeklyPoll.tsx` | Snapshot API (`@snapshot-labs/snapshot.js`) |
| Cross-platform publishing | Auto-publish on Respect threshold | Farcaster (Neynar), Bluesky, X/Twitter |
| Respect-weighted voting | On-chain balance lookup at vote time | OG `0x34cE...6957` + ZOR `0x9885...445c` via viem multicall |

#### Data Sources NOT Yet Unified

| Data | Current Location | Missing Integration |
|------|-----------------|---------------------|
| **Airtable historical data** (Fractals 1-73) | 6 CSV files in `csv import/` | Not imported into Supabase |
| **ORDAO awards** (Fractals 74-90) | 3 awards.csv files | Not imported into Supabase |
| **Video participation** (10 pts/meeting) | Airtable only | No table in Supabase |
| **OG Respect distributions** (intros, articles, festivals) | Airtable + on-chain | No itemized log in Supabase |
| **Live fractal state** | Bot memory (not persisted) | No `fractal_live_sessions` table yet |
| **Discord OAuth linking** | Not built | No `user_discord_links` table |
| **Member profiles (Farcaster)** | ZAO OS `users` table | Not linked to bot `wallets` table |

---

## 2. Opportunities from ZAO OS Research

ZAO OS has 158+ research documents covering capabilities that FractalBot could leverage. Here are the key opportunities:

### 2.1 Farcaster Cross-Platform Publishing (Already Built)

**Research:** ZAO OS docs 28, 77, 96, 97

ZAO OS auto-publishes proposals to Farcaster, Bluesky, and X when a Respect-weighted vote threshold is met (`src/lib/publish/farcaster.ts`, `src/lib/publish/x.ts`). FractalBot could trigger cross-platform announcements for fractal results.

**Integration:** When the bot writes `fractal_sessions` + `fractal_rankings` to Supabase, a Supabase Database Webhook or Edge Function could auto-post results to Farcaster/Bluesky/X using the same publish libraries ZAO OS already has. This means fractal results get community visibility beyond Discord.

### 2.2 ZID Identity System (Designed, Not Built)

**Research:** ZAO OS doc 05

The ZID schema wraps Farcaster FIDs with music profiles, Respect balances, Hats roles, and linked wallets. The `zids` table design includes `music_role`, `genres`, `artist_verified`, and links to `zid_wallets` and `zid_hats`.

**Integration:** The bot's `wallets` table already maps discord_id -> wallet. If ZAO OS builds the `zids` table, the identity chain becomes: **Discord ID <-> Wallet <-> FID <-> ZID**. The bot could display ZID-enriched profiles in Discord (music role, tier, hat badges) and ZAO OS could show Discord activity for Farcaster users.

### 2.3 Snapshot Weekly Polls (Built)

**Research:** ZAO OS docs 131, 132, 133

ZAO OS has a full Snapshot integration: admin one-click poll creation via `CreateWeeklyPoll.tsx`, poll display via `SnapshotPolls.tsx`, and GraphQL API reads via `src/lib/snapshot/client.ts`. The `zaal.eth` space supports approval voting with ZAO workstream choices.

**Integration:** FractalBot could:
- Post weekly Snapshot poll links in Discord when a new poll is created
- Display poll results in Discord via a `/poll` or `/priorities` command
- Automatically create Snapshot polls after fractal sessions, asking "What should ZAO prioritize next week?" with choices derived from fractal discussion topics

### 2.4 ZOUNZ On-Chain Governance (Built, Read-Only)

**Research:** ZAO OS doc 131, 133

ZAO OS reads from the ZOUNZ Governor contract (`0x9d98...17f`) on Base showing proposal counts, quorum, and voting power. The `castVote()` and `propose()` functions are in the ABI but not wired to in-app UI yet.

**Integration:** FractalBot could post ZOUNZ proposal notifications in Discord when new on-chain proposals are detected, and provide a `/vote-zounz` command that generates a deep link to the voting UI. The bot already has blockchain query infrastructure (`_eth_call` pattern) that could be extended to Base.

### 2.5 Respect Tiers and Decay (Designed)

**Research:** ZAO OS docs 04, 58

The Respect system design includes 2% weekly decay, tier thresholds (Newcomer/Member/Curator/Elder/Legend), and equilibrium analysis. A `respect_balances` table is planned as an off-chain cache of on-chain state with decay applied.

**Integration:** FractalBot could apply the tier system to Discord role assignment. After each fractal, the bot recalculates decayed balances and auto-assigns Discord roles matching tiers. This extends the current Hats sync loop (`cogs/hats.py`) with Respect-based role gating — exactly what the Hats `RespectEligibility` module was designed for.

### 2.6 Hats Protocol Deeper Integration (Designed)

**Research:** ZAO OS docs 07, 59

ZAO's live hat tree (tree 226 on Optimism) has 17 sub-hats under Governance Council, most with 0 supply. The SDK (`@hatsprotocol/sdk-v1-core`) supports `mintHat()`, `createHat()`, and `transferHat()` operations. The bot already reads the tree and syncs roles.

**Integration:** The bot could become a hat minting agent: when a member reaches Elder tier (2000 Respect), the bot auto-proposes minting them a Governance Council Members hat. This closes the loop between fractal participation, Respect accumulation, and on-chain role assignment. The contract supports it — all hats are mutable, and the Configurator hat has 2 active wearers who could authorize the bot's address.

### 2.7 orclient SDK for Breakout Submission (Available)

**Research:** ZAO OS docs 56, 58, 103

The `@ordao/orclient` npm package provides `proposeBreakoutResult()`, `vote()`, and `execute()` functions. ZAO already has OREC deployed (`0xcB05...Be532`), ZOR Respect1155 deployed (`0x9885...445c`), and an ornode at `zao-ornode.frapps.xyz` (currently down).

**Integration:** Instead of generating a frapps URL for manual submission, FractalBot or ZAO OS could call `proposeBreakoutResult()` directly. This requires either a Node.js sidecar (since orclient is TypeScript) or implementing the raw OREC proposal submission in Python. The license is GPL-3.0, requiring a service boundary if used from MIT-licensed ZAO OS.

### 2.8 Music-Specific Fractal Adaptations (Researched)

**Research:** ZAO OS doc 103

No music-specific fractal community exists. ZAO could be the first. Opportunities include: song battles as breakout rooms, listening sessions with Fibonacci scoring, curation councils via earned Respect, and integration with the Optimystics Competition App (in development, designed for "ranking musical performances").

**Integration:** The bot's timer system (`cogs/timer.py`) could be extended with audio playback controls. When a member presents during a fractal, the bot could queue their submitted track from ZAO OS (which already has a 9-platform music player). The fractal results then double as curation signals: top-ranked tracks get promoted in ZAO OS's trending feed via the `respect-weighted trending` API at `/api/music/trending-weighted/`.

### 2.9 Supabase Realtime for Live Updates

**Research:** ZAO OS doc 116

The `useFractalLive.ts` hook pattern is designed: Supabase Realtime subscriptions on `fractal_live_sessions` push updates to the `/fractals` page. The architecture is Bot -> Webhook/Direct Write -> Supabase -> Realtime -> Client.

**Integration:** With the bot now writing directly to Supabase, enable Realtime on the session tables. Create a `fractal_live_sessions` table for in-progress state (who's in a room, current voting level, current leader). ZAO OS renders a live "fractal in progress" widget — no polling needed.

### 2.10 Discord OAuth Account Linking (Researched)

**Research:** ZAO OS doc 116

The OAuth flow is fully designed: SIWF (Sign In With Farcaster) for primary auth, Discord OAuth for account linking, `user_discord_links` table storing encrypted tokens. This creates the identity bridge: **FID <-> Discord ID <-> Wallet**.

**Integration:** Once account linking exists, ZAO OS can display Discord-sourced data (fractal history, proposal votes, intro text) on Farcaster user profiles, and vice versa. A member's complete participation history — across both platforms — becomes visible in one place.

---

## 3. Opportunities from FractalBot Research

FractalBot's research library contains deep technical analysis that ZAO OS could leverage:

### 3.1 Onchain Transaction Signing (Researched)

**Research:** `research/discord-transaction-signing.md`

Four approaches were evaluated for automated `submitBreakout` execution: hot wallet, MPC (Turnkey), Gnosis Safe multisig, and ERC-4337 session keys. The recommendation is **hot wallet MVP** with contract-level access control.

**What ZAO OS gains:** The same signing infrastructure could power ZAO OS features:
- **Auto-execute OREC proposals** — when breakout results pass the voting+veto period, ZAO OS or the bot calls `execute()` automatically
- **Mint OG Respect** — admin panel in ZAO OS triggers a `mint()` call on the OG ERC-20 for intro/article/festival rewards (replacing manual wallet transactions by zaal.eth)
- **Mint Hats** — ZAO OS hat management UI triggers `mintHat()` on-chain

The `utils/onchain.py` module recommended in the audit would be the shared signing backend.

### 3.2 Respect-Weighted Voting Patterns (Built)

**Research:** `research/fractalbot-audit-2026-03-27.md` (Section 3.6), bot code `cogs/proposals.py`

The bot implements Respect-weighted voting: vote power = OG + ZOR on-chain balance, looked up via `_eth_call` to both contracts. This pattern is duplicated in ZAO OS (`src/app/api/proposals/vote/route.ts`).

**What ZAO OS gains:** The weighting logic should be shared. Both systems currently make independent RPC calls to the same contracts. Centralizing this in a Supabase function (`get_respect_weight(wallet_address)`) or a shared API endpoint (`/api/respect/weight?address=0x...`) would:
- Eliminate duplicate RPC calls
- Apply consistent weighting (both systems currently sum OG + ZOR, but formatting differs)
- Enable caching — the balance changes only after OREC execution or OG minting, not on every vote

### 3.3 Proposal Governance Patterns (Built)

**Research:** Bot `cogs/proposals.py`, ZAO OS `src/app/api/proposals/`

The bot has 4 proposal types (text, governance, funding, curate) with threaded discussion, 7-day auto-expiry, and index embed maintenance. ZAO OS has 6 categories (governance, technical, community, wavewarz, social, treasury) with auto-publishing, comments, and admin status transitions.

**What ZAO OS gains:** The two proposal systems should converge:
- Bot proposals should appear in ZAO OS's governance tab (they now share the same `proposals` table)
- ZAO OS proposals should post to Discord via the bot (reverse direction — bot reads new proposals from Supabase and posts embeds)
- Voting from either platform should update the same row (already possible since both write to `proposal_votes`)

### 3.4 Hats-Discord Role Sync (Built)

**Research:** Bot `cogs/hats.py`

The bot runs a 10-minute sync loop: reads hat wearers from the Optimism contract, maps to Discord roles via `hats_role_mappings`, and assigns/removes Discord roles. This is the only system performing hat-to-Discord sync.

**What ZAO OS gains:** This sync data could populate ZAO OS profiles. Instead of ZAO OS making its own RPC calls to check hat ownership, it reads the bot's sync results from `hats_role_mappings` + a new `hat_wearers` table. The bot becomes the authoritative cache for hat state across both platforms.

### 3.5 Blockchain RPC Module (Planned)

**Research:** `research/fractalbot-audit-2026-03-27.md` (MEDIUM-013, Section 3.1)

The audit identified three duplicated `_eth_call` implementations across `cogs/proposals.py`, `cogs/guide.py`, and `cogs/hats.py`. The recommendation is a shared `utils/blockchain.py` with a singleton `aiohttp` session and Multicall3 batching.

**What ZAO OS gains:** If this module is built with a clean interface, ZAO OS could call it via an API endpoint rather than duplicating the same logic in TypeScript. Alternatively, the module's Multicall3 batching pattern could be ported to ZAO OS's viem setup, which already uses `multicall` in `leaderboard.ts`.

---

## 4. Top 10 Integration Priorities

Ranked by impact and effort. Each priority identifies what changes, where, and what it depends on.

### Priority 1: Bidirectional Proposal Sync

**What:** Bot-created proposals appear in ZAO OS governance tab. ZAO OS-created proposals appear in Discord. Votes from either platform count toward the same tally.

**Why it matters:** Currently, the community is split — Discord members and Farcaster members see different proposals. Unifying this doubles participation in every vote.

**Repos changed:**
- **FractalBot:** Add a Supabase polling loop in `cogs/proposals.py` that detects new ZAO OS proposals and posts Discord embeds with vote buttons
- **ZAO OS:** Already reads from the shared `proposals` table — just ensure Discord-created proposals render correctly in `ProposalsTab.tsx`

**Effort:** 4-6 hours
**Dependencies:** Shared Supabase (done), consistent proposal schema (done — both use the same table)

---

### Priority 2: Live Fractal Dashboard in ZAO OS

**What:** When a fractal is happening in Discord, ZAO OS shows a live widget: who's in the room, current voting level, current leader, vote counts updating in real-time.

**Why it matters:** Makes ZAO OS the "one place for all data" that Zaal wants. Members who aren't in Discord can still follow along. Creates urgency and FOMO.

**Repos changed:**
- **FractalBot:** Add Supabase writes for live state in `cogs/fractal/group.py` — upsert to `fractal_live_sessions` on each vote/round change
- **ZAO OS:** Create `fractal_live_sessions` table, enable Realtime, build `useFractalLive.ts` hook and live widget component

**Effort:** 6-8 hours
**Dependencies:** Supabase Realtime enabled (configuration step), `fractal_live_sessions` table created

---

### Priority 3: Discord-Farcaster Account Linking

**What:** ZAO OS users link their Discord account via OAuth. This creates the FID <-> Discord ID <-> Wallet identity chain.

**Why it matters:** Without this link, there's no way to show a member's complete participation history across platforms. A Farcaster user can't see their Discord fractal history. A Discord user can't see their ZAO OS proposals.

**Repos changed:**
- **ZAO OS:** Create `user_discord_links` table, Discord OAuth flow at `/api/auth/discord/`, callback handler, "Link Discord" button in settings
- **FractalBot:** No changes needed — the link is consumed downstream

**Effort:** 4-6 hours
**Dependencies:** Discord OAuth app configured in Developer Portal (already exists for the bot)

---

### Priority 4: Auto-Submit Breakout Results On-Chain

**What:** After fractal voting completes, the bot automatically submits the `submitBreakout` transaction on Optimism via a hot wallet, instead of generating a URL for manual submission.

**Why it matters:** Currently only zaal.eth and civilmonkey.eth submit results. This is a bottleneck and single point of failure. Automating it ensures every fractal's results are recorded on-chain immediately.

**Repos changed:**
- **FractalBot:** Create `utils/onchain.py` with `web3.py` signing. Modify `_post_submit_breakout` in `cogs/fractal/group.py` to call it. Add `BOT_PRIVATE_KEY` env var.

**Effort:** 4-6 hours (the research doc provides complete implementation code)
**Dependencies:** Dedicated bot wallet funded with ~0.01 ETH on Optimism, contract allowlist updated (if applicable)

---

### Priority 5: Respect Tier Roles in Discord

**What:** After each fractal, the bot calculates each member's decayed Respect balance (2% weekly decay), determines their tier (Newcomer/Member/Curator/Elder/Legend), and assigns corresponding Discord roles.

**Why it matters:** Creates visible social capital in Discord. Members see who's an Elder vs a Newcomer. This drives participation — people want to maintain their tier.

**Repos changed:**
- **FractalBot:** Add decay calculation in `cogs/hats.py` or a new `cogs/tiers.py`. Query `respect_leaderboard` VIEW, apply decay formula, map to Discord roles.
- **ZAO OS:** Add `respect_balances` table as off-chain cache. Sync job reads from on-chain (OG + ZOR) and applies decay. Display tier badges on profiles.

**Effort:** 6-8 hours
**Dependencies:** `respect_balances` table created, tier thresholds configured (0/100/500/2000/10000)

---

### Priority 6: Historical Data Import

**What:** Import all Airtable CSVs (Fractals 1-73, hosting points, festival attendance, miscellaneous contributions) and ORDAO awards CSVs (Fractals 74-90) into Supabase.

**Why it matters:** Without this, the leaderboard and member profiles only show recent data. 90+ weeks of history and 173 members' contribution records are trapped in spreadsheets.

**Repos changed:**
- **FractalBot:** Create `scripts/import_historical.py` using the reconciliation plan from ZAO OS doc 115
- **Supabase:** May need `og_respect_distributions` table for non-fractal rewards (intros, articles, festivals)

**Effort:** 8-12 hours (parsing 9 CSV files with different formats, deduplication, cross-referencing wallets)
**Dependencies:** Access to `csv import/` directory (6 Airtable CSVs) and `~/Downloads/awards*.csv` (3 ORDAO CSVs)

---

### Priority 7: Snapshot Poll Notifications in Discord

**What:** When a new Snapshot weekly poll is created in ZAO OS, the bot posts it in Discord with a link. When voting closes, the bot posts results.

**Why it matters:** Snapshot polls currently only reach Farcaster users via ZAO OS. Discord members (the larger community) miss them entirely.

**Repos changed:**
- **FractalBot:** Add a polling loop or Supabase trigger that checks for new Snapshot polls via the GraphQL API. Post embeds in a designated Discord channel.
- **ZAO OS:** Optionally write a `snapshot_polls` row to Supabase when creating a poll, so the bot can subscribe via Realtime instead of polling.

**Effort:** 3-4 hours
**Dependencies:** `zaal.eth` Snapshot space active, bot Discord channel designated for governance

---

### Priority 8: Unified Blockchain Module

**What:** Extract all blockchain RPC calls (OG Respect balance, ZOR balance, Hats tree, OREC state) into a shared `utils/blockchain.py` module with connection pooling, Multicall3 batching, and caching.

**Why it matters:** The audit found 3 duplicate `_eth_call` implementations. The Hats sync loop makes up to 1,000 RPC calls per 10 minutes. Multicall3 reduces this to ~5 calls. Shared session prevents socket exhaustion.

**Repos changed:**
- **FractalBot:** Create `utils/blockchain.py`. Refactor `cogs/proposals.py`, `cogs/guide.py`, `cogs/hats.py` to use it. Use the existing (but unused) `_wearer_cache` in hats.py.

**Effort:** 4-6 hours
**Dependencies:** None — purely internal refactor

---

### Priority 9: Cross-Platform Fractal Results Publishing

**What:** When a fractal completes, auto-post results to Farcaster, Bluesky, and X using ZAO OS's publish infrastructure.

**Why it matters:** Fractal results currently stay in Discord. Publishing them creates public proof of community activity, attracts new members, and gives participants external recognition.

**Repos changed:**
- **ZAO OS:** Add a Supabase Database Webhook on `fractal_sessions` INSERT that triggers an Edge Function. The function formats results and calls `src/lib/publish/farcaster.ts`, `src/lib/publish/x.ts`, etc.
- **FractalBot:** No changes needed — it already writes to `fractal_sessions`.

**Effort:** 4-6 hours
**Dependencies:** Supabase Edge Functions enabled, Neynar/Bluesky/X API keys configured in ZAO OS

---

### Priority 10: In-App ZOUNZ Voting

**What:** Wire up `castVote()` on the ZOUNZ Governor contract in ZAO OS so members can vote on-chain proposals without leaving the app.

**Why it matters:** Currently ZAO OS is read-only for ZOUNZ governance — users must go to nouns.build to vote. In-app voting increases participation.

**Repos changed:**
- **ZAO OS:** Add vote buttons in `ZounzProposals.tsx` using wagmi `useWriteContract`. The Governor ABI already includes `castVote(bytes32, uint256)`.

**Effort:** 3-4 hours
**Dependencies:** Wallet connection (already built via wagmi), Base RPC (already configured)

---

## 5. Architecture Recommendations

### 5.1 Deprecate the FractalBot Web Dashboard

**Recommendation: Yes, deprecate it.**

The FractalBot web dashboard (`web/` directory, Next.js 14, Neon Postgres, NextAuth) was built before the Supabase migration. Its current state:
- Vercel deployment is offline (doc 114)
- It read JSON files directly — impossible in production
- It duplicated data sources (Drizzle/Neon AND JSON files)

ZAO OS now provides everything the dashboard did and more:
- Fractal history and sessions tab
- Respect leaderboard with on-chain balances
- Proposal voting with Respect weighting
- Hats tree viewer
- ZOUNZ governance display
- Snapshot polls

**Action:** Archive the `web/` directory. Remove Drizzle/Neon/NextAuth dependencies. Direct all web traffic to ZAO OS.

### 5.2 Share the Blockchain Module

**Recommendation: Don't share code directly. Share via Supabase.**

The bot (Python) and ZAO OS (TypeScript) run on different runtimes. Code sharing would require a polyglot approach (REST API bridge, gRPC, etc.) that adds complexity.

Instead, use Supabase as the data bridge:
- **Bot writes blockchain state to Supabase** — Respect balances, hat wearers, OREC proposal status
- **ZAO OS reads from Supabase** — no RPC calls needed for cached data
- **ZAO OS makes direct RPC calls only for** — real-time operations (casting votes, checking current balance at vote time)

This means the bot's `utils/blockchain.py` becomes the canonical blockchain data ingester, and ZAO OS is a consumer.

```
Optimism/Base Contracts
    |
    | RPC calls (Multicall3 batched)
    v
FractalBot (utils/blockchain.py)
    |
    | Writes cached state
    v
Supabase (respect_balances, hat_wearers, orec_proposals)
    |
    | Reads cached state + Realtime subscriptions
    v
ZAO OS (Next.js)
    |
    | Direct RPC only for write operations
    v
Optimism/Base Contracts
```

### 5.3 Webhook vs Supabase Realtime for Live Updates

**Recommendation: Supabase Realtime. Deprecate webhooks.**

Now that both systems share Supabase, the webhook bridge (`utils/web_integration.py`) is redundant:

| Approach | Latency | Reliability | Maintenance |
|----------|---------|-------------|-------------|
| **Webhooks** (bot -> ZAO OS API) | ~200ms (HTTP round-trip) | Requires endpoint availability, retry logic, auth | Two systems to maintain |
| **Supabase Realtime** (bot writes -> ZAO OS subscribes) | ~50ms (WebSocket push) | Built-in reconnection, no auth needed (RLS handles it) | Zero additional code |
| **Supabase Direct Write** (bot -> Supabase -> ZAO OS reads) | ~100ms (DB write + read) | Depends only on Supabase uptime | Already built |

**Action:**
1. The bot already writes to Supabase. This is the "push."
2. ZAO OS subscribes to Supabase Realtime channels on the tables it cares about.
3. Remove `utils/web_integration.py` and the `WEB_WEBHOOK_URL`/`WEBHOOK_SECRET` env vars from the bot.
4. Delete unused webhook functions: `notify_fractal_paused()`, `notify_fractal_resumed()` (already identified as dead code in the audit).

### 5.4 Long-Term Architecture Vision

```
┌─────────────────────────────────────────────────────────┐
│                    ZAO Ecosystem                         │
│                                                         │
│  ┌──────────────┐     ┌──────────────┐                  │
│  │  FractalBot   │     │   ZAO OS     │                  │
│  │  (Python)     │     │  (Next.js)   │                  │
│  │               │     │              │                  │
│  │  Discord UX:  │     │  Farcaster   │                  │
│  │  - Fractals   │     │  UX:         │                  │
│  │  - Timer      │     │  - Music     │                  │
│  │  - Voice mgmt │     │  - Chat      │                  │
│  │  - Proposals  │     │  - Profiles  │                  │
│  │  - Hats sync  │     │  - Governance│                  │
│  │  - Wallet reg │     │  - Leaderboard│                 │
│  │               │     │  - Publishing│                  │
│  └───────┬───────┘     └──────┬───────┘                  │
│          │                    │                           │
│          │   Writes           │   Reads + Writes          │
│          v                    v                           │
│  ┌────────────────────────────────────┐                  │
│  │          Supabase                   │                  │
│  │                                    │                  │
│  │  Shared tables (proposals, wallets,│                  │
│  │  sessions, rankings, intros, events,│                 │
│  │  hats, respect_balances)           │                  │
│  │                                    │                  │
│  │  Realtime subscriptions            │                  │
│  │  RLS policies                      │                  │
│  │  Database webhooks -> Edge Functions│                 │
│  └──────────────┬─────────────────────┘                  │
│                 │                                         │
│                 │ Periodic sync                           │
│                 v                                         │
│  ┌────────────────────────────────────┐                  │
│  │    Optimism / Base Contracts        │                  │
│  │                                    │                  │
│  │  OG Respect (ERC-20)    0x34cE...  │                  │
│  │  ZOR Respect (ERC-1155) 0x9885...  │                  │
│  │  OREC Governance        0xcB05...  │                  │
│  │  Hats Tree 226          0x3bc1...  │                  │
│  │  ZOUNZ Governor (Base)  0x9d98...  │                  │
│  │  ZOUNZ Token (Base)     0xCB80...  │                  │
│  └────────────────────────────────────┘                  │
└─────────────────────────────────────────────────────────┘
```

**The roles are clear:**
- **FractalBot** = Discord-native operations (voice management, real-time voting, timer, role sync, blockchain data ingestion)
- **ZAO OS** = Web/Farcaster-native operations (music, profiles, governance display, cross-platform publishing, admin panel)
- **Supabase** = Single source of truth for all shared state
- **On-chain** = Permanent, verifiable records (Respect tokens, governance votes, hat assignments)

### 5.5 Should FractalBot Become a ZAO OS Microservice?

**Not yet.** The bot serves a fundamentally different UX — Discord voice channels, real-time button voting, audio playback in channels. These are Discord-native capabilities that a web app cannot replicate. The bot should remain an independent process that shares data via Supabase.

However, if ZAO OS ever builds a WebRTC-based fractal room (using Jitsi, which is already integrated at `src/app/(auth)/calls/`), the bot's voting logic could be ported to a ZAO OS API route. At that point, the bot would handle only Discord-specific features (role sync, voice management) and ZAO OS would handle the full fractal lifecycle.

---

## 6. Quick Wins

Things that can be done in a day or less:

### 6.1 Enable Supabase Realtime on `fractal_sessions` and `proposals` (15 min)

The migration plan specifies `ALTER PUBLICATION supabase_realtime ADD TABLE ...` for 4 tables. If this hasn't been run yet, do it now. ZAO OS can immediately subscribe to new fractal sessions and proposal changes.

**Where:** Supabase SQL Editor
**Impact:** ZAO OS governance tab auto-refreshes when someone votes in Discord

### 6.2 Post Discord Proposal Links in ZAO OS (30 min)

When the bot creates a proposal in the shared `proposals` table, include the `thread_id` field. ZAO OS can render a "Discuss in Discord" link: `https://discord.com/channels/{guild_id}/{thread_id}`.

**Where:** `ProposalsTab.tsx` in ZAO OS — add a Discord icon link next to proposals that have a `thread_id`
**Impact:** Bridges the community — Farcaster users can jump into Discord discussions

### 6.3 Post ZAO OS Proposal Links in Discord (1 hour)

Add a Supabase polling loop in the bot (check every 60s for new proposals where `thread_id IS NULL`) and auto-post a Discord embed with a link back to ZAO OS.

**Where:** `cogs/proposals.py` — add a `@tasks.loop(seconds=60)` task
**Impact:** Discord users discover Farcaster-originated proposals

### 6.4 Fix the `html` Variable Shadowing Bug (5 min)

The audit identified a live runtime crash in `cogs/proposals.py` line 244: `html = await resp.text()` shadows the `import html` at line 37. When `html.unescape()` is called at line 259, it crashes.

**Where:** `cogs/proposals.py` line 244
**Fix:** Rename `html` to `page_html`
**Impact:** Fixes broken `/curate` URL enrichment

### 6.5 Use the Existing `_wearer_cache` in hats.py (30 min)

The audit found that `self._wearer_cache` in `cogs/hats.py` is initialized as `{}` but never populated. The Hats sync loop makes up to 1,000 RPC calls per 10 minutes. Populating and checking this cache eliminates redundant calls.

**Where:** `cogs/hats.py` — populate `_wearer_cache` after each `_is_wearer_of_hat` call, check before making RPC call, TTL of 10 minutes
**Impact:** Cuts RPC calls by 90%+

### 6.6 Add On-Chain Contract Links to Discord Bot (30 min)

The bot's `/guide` command shows a leaderboard but no links to the actual contracts. Add clickable Etherscan links for OG Respect, ZOR, OREC, and the Hats tree.

**Where:** `cogs/guide.py` — add an embed field with contract links
**Impact:** Member transparency, easy verification

### 6.7 Create `respect_balances` Supabase Table (1 hour)

The off-chain cache table designed in ZAO OS doc 58. Populate it from the `respect_leaderboard` VIEW + on-chain balance reads, apply 2% weekly decay, store tier.

```sql
CREATE TABLE respect_balances (
  wallet_address TEXT PRIMARY KEY,
  display_name TEXT,
  og_balance DECIMAL DEFAULT 0,
  zor_balance DECIMAL DEFAULT 0,
  total_onchain DECIMAL DEFAULT 0,
  decayed_balance DECIMAL DEFAULT 0,
  tier TEXT DEFAULT 'newcomer',
  last_synced_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Where:** Supabase SQL Editor + bot sync job
**Impact:** Foundation for tier roles, weighted features, and profile badges across both platforms

### 6.8 Add `/priorities` Command to Bot (45 min)

A simple command that fetches the latest Snapshot poll results via GraphQL and displays them as a Discord embed.

**Where:** New command in `cogs/proposals.py` or a new `cogs/snapshot.py`
**Impact:** Discord members can see weekly priorities without leaving Discord

---

## Appendix: Research Documents Referenced

### FractalBot Research (`/Users/zaalpanthaki/Documents/fractalbotmarch2026/research/`)

| File | Key Content |
|------|-------------|
| `fractalbot-audit-2026-03-27.md` | 29 findings, dead code analysis, strategic roadmap |
| `supabase-migration-plan.md` | Full schema, migration script, bot code changes, web app changes |
| `discord-transaction-signing.md` | 5 signing approaches, comparison matrix, hot wallet recommendation |

### ZAO OS Research (`/Users/zaalpanthaki/Documents/ZAO OS V1/research/`)

| Doc | Key Content |
|-----|-------------|
| `04-respect-tokens/` | Respect token design: soulbound, decay, tiers, ERC-5192 |
| `05-zao-identity/` | ZID schema: FID wrapper with music profile, Respect, Hats, wallets |
| `07-hats-protocol/` | Hats Protocol deep dive: SDK, ZAO hat tree structure, eligibility modules |
| `56-ordao-respect-system/` | ORDAO/OREC architecture, Respect Game mechanics, Fibonacci scoring, contract addresses |
| `58-respect-deep-dive/` | On-chain state (OG + ZOR + OREC), decay math, orclient SDK integration |
| `59-hats-tree-integration/` | Live ZAO tree 226 on Optimism, hat IDs, integration architecture |
| `103-fractal-governance-ecosystem/` | Eden Fractal, Optimism Fractal, music-specific adaptations |
| `113-zao-fractal-bot-process/` | Bot architecture, voting flow, integration opportunities |
| `114-zao-fractal-live-infrastructure/` | Live services status, webhook events, frapps URL format |
| `115-zao-data-reconciliation/` | Airtable CSVs, ORDAO awards, import plan for historical data |
| `116-discord-integration-research/` | `@discordjs/rest`, Discord OAuth, webhook receiver, Supabase Realtime |
| `131-onchain-proposals-governance/` | ZOUNZ Governor, Snapshot, OpenZeppelin Governor comparison |
| `132-snapshot-weekly-polls/` | Snapshot.js SDK, weekly poll template, multi-project support |
| `133-governance-system-audit/` | Complete inventory of all governance features in ZAO OS |
| `139-trustware-sdk-deep-dive/` | Trustware evaluation: NOT YET, use Zora + 0xSplits instead |

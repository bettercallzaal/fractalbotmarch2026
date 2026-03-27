# Supabase Migration Plan for ZAO Fractal Bot

**Date:** 2026-03-27
**Status:** Research / Planning
**Author:** Zaal + Claude

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [Database Schema](#2-database-schema)
3. [Migration Script](#3-migration-script)
4. [Bot Code Changes](#4-bot-code-changes)
5. [Web App Changes](#5-web-app-changes)
6. [Environment Setup](#6-environment-setup)
7. [Rollout Plan](#7-rollout-plan)

---

## 1. Motivation

The bot currently uses six flat JSON files for persistence:

| File | Purpose | Size (approx) |
|------|---------|----------------|
| `proposals.json` | Proposals with embedded votes | ~24 proposals |
| `wallets.json` | Discord ID to wallet mapping | ~53 entries |
| `names_to_wallets.json` | Display name to wallet mapping | ~166 entries |
| `history.json` | Fractal session results with embedded rankings | ~7 sessions |
| `intros.json` | Cached member introductions | ~6 entries |
| `events.json` | Recurring event schedules | Currently empty |

**Problems with JSON files:**

- **Dual data source:** The Next.js web app reads the same JSON files via `loadJsonData.ts` (filesystem access), which only works when the web app runs on the same machine as the bot. In production (Vercel/Railway), the web app cannot access these files.
- **No concurrency safety:** Multiple cog operations can race on the same JSON file. `atomic_save` prevents corruption but not lost updates.
- **No queryability:** Every lookup is a linear scan (e.g., `get_by_user` iterates all fractals).
- **No realtime:** The web dashboard must poll; it cannot subscribe to changes.

**Why Supabase:**

- Hosted Postgres with connection pooling (PgBouncer) -- no infra to manage.
- Built-in REST API (PostgREST) so the Next.js app can query directly from the client.
- Built-in Realtime (WebSocket) for live dashboard updates.
- Built-in Auth with Discord OAuth provider support.
- Row Level Security (RLS) for fine-grained access control.
- `supabase-py` official async Python client for the bot.
- Generous free tier (500 MB database, 1 GB file storage).

---

## 2. Database Schema

### 2.1 Full SQL Migration

```sql
-- ============================================================
-- ZAO Fractal Bot -- Supabase Migration
-- Run this as a single transaction in the Supabase SQL Editor
-- ============================================================

-- Enable UUID generation (Supabase has this by default)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ──────────────────────────────────────────────────────────────
-- WALLETS
-- ──────────────────────────────────────────────────────────────
-- Merges wallets.json (discord_id -> wallet) and
-- names_to_wallets.json (display_name -> wallet) into one table.
-- The "source" column tracks provenance.

CREATE TABLE wallets (
    id              BIGSERIAL PRIMARY KEY,
    discord_id      VARCHAR(64) UNIQUE,          -- nullable for name-only entries
    display_name    VARCHAR(255),                 -- from names_to_wallets.json
    wallet_address  VARCHAR(255),                 -- can be empty string for unresolved names
    source          VARCHAR(20) NOT NULL DEFAULT 'name',  -- 'discord_id' or 'name'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_wallets_discord_id ON wallets(discord_id) WHERE discord_id IS NOT NULL;
CREATE INDEX idx_wallets_wallet_address ON wallets(wallet_address) WHERE wallet_address IS NOT NULL AND wallet_address != '';
CREATE INDEX idx_wallets_display_name ON wallets(lower(display_name)) WHERE display_name IS NOT NULL;

COMMENT ON TABLE wallets IS 'Maps Discord users and display names to Ethereum wallet addresses. Merges both wallets.json and names_to_wallets.json.';

-- ──────────────────────────────────────────────────────────────
-- PROPOSALS
-- ──────────────────────────────────────────────────────────────

CREATE TABLE proposals (
    id              BIGSERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT,
    proposal_type   VARCHAR(50) NOT NULL,         -- 'text', 'governance', 'funding', 'curate'
    author_id       VARCHAR(64) NOT NULL,         -- Discord user ID of proposer
    thread_id       VARCHAR(64),                  -- Discord thread snowflake
    message_id      VARCHAR(64),                  -- Discord message snowflake (embed with buttons)
    status          VARCHAR(20) NOT NULL DEFAULT 'active',  -- 'active', 'closed'
    options         TEXT[] DEFAULT '{}',           -- governance proposal options
    funding_amount  DECIMAL(18,2),                -- funding proposals only
    image_url       TEXT,
    project_url     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ
);

CREATE INDEX idx_proposals_status ON proposals(status);
CREATE INDEX idx_proposals_author ON proposals(author_id);
CREATE INDEX idx_proposals_thread ON proposals(thread_id);

COMMENT ON TABLE proposals IS 'Governance proposals created via /propose and /curate. Votes are in a separate table.';

-- ──────────────────────────────────────────────────────────────
-- PROPOSAL VOTES (normalized out of the embedded JSON dict)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE proposal_votes (
    id              BIGSERIAL PRIMARY KEY,
    proposal_id     BIGINT NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    voter_id        VARCHAR(64) NOT NULL,         -- Discord user ID
    vote_value      VARCHAR(100) NOT NULL,        -- 'yes', 'no', 'abstain', or governance option text
    weight          DECIMAL(18,4) NOT NULL DEFAULT 1.0,  -- Respect-weighted vote power
    voted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(proposal_id, voter_id)                 -- one vote per user per proposal
);

CREATE INDEX idx_proposal_votes_proposal ON proposal_votes(proposal_id);
CREATE INDEX idx_proposal_votes_voter ON proposal_votes(voter_id);

COMMENT ON TABLE proposal_votes IS 'Individual Respect-weighted votes on proposals. UNIQUE constraint enforces one vote per user; re-voting UPSERTs.';

-- ──────────────────────────────────────────────────────────────
-- PROPOSAL INDEX MESSAGE (bot metadata)
-- ──────────────────────────────────────────────────────────────
-- The bot maintains a pinned "index" embed in the proposals channel.
-- We store its message ID in a simple key-value metadata table.

CREATE TABLE bot_metadata (
    key             VARCHAR(100) PRIMARY KEY,
    value           TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE bot_metadata IS 'Key-value store for bot-level config (e.g., proposals index message ID).';

-- ──────────────────────────────────────────────────────────────
-- FRACTAL HISTORY (sessions)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE fractal_sessions (
    id              BIGSERIAL PRIMARY KEY,
    group_name      VARCHAR(255) NOT NULL,
    facilitator_id  VARCHAR(64) NOT NULL,         -- Discord user ID
    facilitator_name VARCHAR(255) NOT NULL,
    fractal_number  VARCHAR(100),                 -- e.g. '92', 'March 9', 'Feb 23rd'
    group_number    VARCHAR(100),                 -- e.g. '1', '2', '3'
    guild_id        VARCHAR(64) NOT NULL,
    thread_id       VARCHAR(64),
    completed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_fractal_sessions_facilitator ON fractal_sessions(facilitator_id);
CREATE INDEX idx_fractal_sessions_completed ON fractal_sessions(completed_at DESC);
CREATE INDEX idx_fractal_sessions_fractal_number ON fractal_sessions(fractal_number);

COMMENT ON TABLE fractal_sessions IS 'Completed fractal meeting sessions. Rankings are in a separate table.';

-- ──────────────────────────────────────────────────────────────
-- FRACTAL RANKINGS (normalized out of embedded JSON array)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE fractal_rankings (
    id              BIGSERIAL PRIMARY KEY,
    session_id      BIGINT NOT NULL REFERENCES fractal_sessions(id) ON DELETE CASCADE,
    user_id         VARCHAR(64) NOT NULL,         -- Discord user ID
    display_name    VARCHAR(255) NOT NULL,
    level           INTEGER NOT NULL,             -- 6 = 1st place, 1 = 6th place
    respect         INTEGER NOT NULL DEFAULT 0,   -- Respect points awarded
    rank_position   INTEGER NOT NULL,             -- 1-indexed position (1 = highest level)
    UNIQUE(session_id, user_id)
);

CREATE INDEX idx_fractal_rankings_session ON fractal_rankings(session_id);
CREATE INDEX idx_fractal_rankings_user ON fractal_rankings(user_id);
CREATE INDEX idx_fractal_rankings_respect ON fractal_rankings(respect DESC);

COMMENT ON TABLE fractal_rankings IS 'Per-participant rankings within each fractal session. Level 6 = 1st place (110 Respect).';

-- ──────────────────────────────────────────────────────────────
-- INTROS (cached member introductions)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE intros (
    id              BIGSERIAL PRIMARY KEY,
    discord_id      VARCHAR(64) NOT NULL UNIQUE,
    intro_text      TEXT NOT NULL,
    message_id      VARCHAR(64),                  -- Discord message snowflake
    posted_at       TIMESTAMPTZ,                  -- when the intro was originally posted
    cached_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_intros_discord_id ON intros(discord_id);

COMMENT ON TABLE intros IS 'Cached introductions from the #intros Discord channel.';

-- ──────────────────────────────────────────────────────────────
-- EVENTS (recurring scheduled events)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE events (
    id              BIGSERIAL PRIMARY KEY,
    slug            VARCHAR(255) NOT NULL UNIQUE,  -- URL-safe key (e.g. 'weekly-fractal')
    name            VARCHAR(255) NOT NULL,
    day_of_week     VARCHAR(20) NOT NULL,          -- 'monday'..'sunday'
    event_time      VARCHAR(10) NOT NULL,          -- '18:00' (24h format)
    timezone        VARCHAR(100) NOT NULL DEFAULT 'UTC',
    channel_id      VARCHAR(64) NOT NULL,          -- Discord channel for reminders
    created_by      VARCHAR(64) NOT NULL,          -- Discord user who created it
    last_reminded_24h VARCHAR(64),                 -- ISO timestamp of last 24h reminder occurrence
    last_reminded_6h  VARCHAR(64),
    last_reminded_1h  VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE events IS 'Recurring weekly events with automatic Discord reminders at 24h, 6h, and 1h.';

-- ──────────────────────────────────────────────────────────────
-- HATS ROLE MAPPINGS
-- ──────────────────────────────────────────────────────────────

CREATE TABLE hats_role_mappings (
    id              BIGSERIAL PRIMARY KEY,
    hat_id_hex      VARCHAR(66) NOT NULL UNIQUE,   -- 0x-prefixed 64-char hex hat ID
    role_id         VARCHAR(64) NOT NULL,           -- Discord role snowflake
    hat_name        VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE hats_role_mappings IS 'Maps Hats Protocol onchain hat IDs to Discord roles for automatic role sync.';

-- ──────────────────────────────────────────────────────────────
-- VIEWS (useful pre-built queries)
-- ──────────────────────────────────────────────────────────────

-- Cumulative Respect leaderboard
CREATE OR REPLACE VIEW respect_leaderboard AS
SELECT
    r.user_id,
    r.display_name,
    SUM(r.respect) AS total_respect,
    COUNT(*) AS participations,
    COUNT(*) FILTER (WHERE r.rank_position = 1) AS first_place,
    COUNT(*) FILTER (WHERE r.rank_position = 2) AS second_place,
    COUNT(*) FILTER (WHERE r.rank_position = 3) AS third_place,
    RANK() OVER (ORDER BY SUM(r.respect) DESC) AS rank
FROM fractal_rankings r
JOIN fractal_sessions s ON s.id = r.session_id
GROUP BY r.user_id, r.display_name
ORDER BY total_respect DESC;

COMMENT ON VIEW respect_leaderboard IS 'Pre-aggregated cumulative Respect leaderboard across all fractal sessions.';

-- Active proposals with vote summary
CREATE OR REPLACE VIEW active_proposals_summary AS
SELECT
    p.id,
    p.title,
    p.proposal_type,
    p.status,
    p.created_at,
    COUNT(v.id) AS total_votes,
    SUM(v.weight) AS total_weight,
    COUNT(v.id) FILTER (WHERE v.vote_value = 'yes') AS yes_count,
    SUM(v.weight) FILTER (WHERE v.vote_value = 'yes') AS yes_weight,
    COUNT(v.id) FILTER (WHERE v.vote_value = 'no') AS no_count,
    SUM(v.weight) FILTER (WHERE v.vote_value = 'no') AS no_weight
FROM proposals p
LEFT JOIN proposal_votes v ON v.proposal_id = p.id
WHERE p.status = 'active'
GROUP BY p.id;

COMMENT ON VIEW active_proposals_summary IS 'Active proposals with aggregated vote counts and Respect-weighted tallies.';

-- ──────────────────────────────────────────────────────────────
-- ROW LEVEL SECURITY (RLS)
-- ──────────────────────────────────────────────────────────────
-- Enable RLS on all tables. The bot uses the service_role key
-- (bypasses RLS). The web app uses the anon key with these policies.

ALTER TABLE wallets ENABLE ROW LEVEL SECURITY;
ALTER TABLE proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE proposal_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE fractal_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE fractal_rankings ENABLE ROW LEVEL SECURITY;
ALTER TABLE intros ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE hats_role_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_metadata ENABLE ROW LEVEL SECURITY;

-- Public read access for dashboard (anon key)
CREATE POLICY "Public read" ON proposals FOR SELECT USING (true);
CREATE POLICY "Public read" ON proposal_votes FOR SELECT USING (true);
CREATE POLICY "Public read" ON fractal_sessions FOR SELECT USING (true);
CREATE POLICY "Public read" ON fractal_rankings FOR SELECT USING (true);
CREATE POLICY "Public read" ON intros FOR SELECT USING (true);
CREATE POLICY "Public read" ON events FOR SELECT USING (true);
CREATE POLICY "Public read" ON hats_role_mappings FOR SELECT USING (true);
CREATE POLICY "Public read" ON wallets FOR SELECT USING (true);

-- Bot metadata: read-only for anon
CREATE POLICY "Public read" ON bot_metadata FOR SELECT USING (true);

-- Authenticated users can vote (the bot handles validation, but this
-- allows direct web voting in the future)
CREATE POLICY "Authenticated vote" ON proposal_votes
    FOR INSERT
    WITH CHECK (auth.role() = 'authenticated');

-- ──────────────────────────────────────────────────────────────
-- TRIGGERS: auto-update updated_at
-- ──────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER wallets_updated_at
    BEFORE UPDATE ON wallets
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ──────────────────────────────────────────────────────────────
-- REALTIME: enable for dashboard live updates
-- ──────────────────────────────────────────────────────────────

ALTER PUBLICATION supabase_realtime ADD TABLE proposals;
ALTER PUBLICATION supabase_realtime ADD TABLE proposal_votes;
ALTER PUBLICATION supabase_realtime ADD TABLE fractal_sessions;
ALTER PUBLICATION supabase_realtime ADD TABLE fractal_rankings;
```

### 2.2 Schema Design Decisions

| Decision | Rationale |
|----------|-----------|
| Votes in separate table | Enables aggregation queries, avoids JSON column updates, supports UNIQUE constraint for one-vote-per-user |
| Rankings in separate table | Enables per-user queries without scanning all sessions, supports the leaderboard VIEW |
| Wallets merged into one table | Both `wallets.json` and `names_to_wallets.json` serve the same purpose; `source` column tracks provenance |
| Discord IDs as VARCHAR(64) | Discord snowflakes are 64-bit integers but storing as strings matches current JSON format and avoids JavaScript number precision issues |
| `respect_leaderboard` VIEW | Replaces the `get_leaderboard()` linear scan with a single indexed query |
| `bot_metadata` table | Holds the proposals index message ID (and future bot config) without a dedicated table |
| Realtime on 4 tables | Only the tables the dashboard needs live updates on |

---

## 3. Migration Script

### 3.1 Script Outline (`scripts/migrate_to_supabase.py`)

```python
"""
One-time migration script: reads all JSON data files and inserts into Supabase.

Usage:
    export SUPABASE_URL=https://your-project.supabase.co
    export SUPABASE_SERVICE_KEY=eyJ...  # service_role key (bypasses RLS)
    python scripts/migrate_to_supabase.py

Prerequisites:
    pip install supabase httpx
"""

import json
import os
import asyncio
from supabase import create_client, Client

# ── Config ──────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def load_json(filename: str):
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.exists(filepath):
        return None
    with open(filepath) as f:
        return json.load(f)


def migrate_wallets():
    """Migrate wallets.json and names_to_wallets.json into the wallets table."""
    print("=== Migrating wallets ===")

    # 1. Discord ID wallets (wallets.json)
    wallets = load_json("wallets.json") or {}
    discord_rows = []
    for discord_id, address in wallets.items():
        discord_rows.append({
            "discord_id": discord_id,
            "wallet_address": address,
            "source": "discord_id",
        })

    if discord_rows:
        supabase.table("wallets").upsert(
            discord_rows, on_conflict="discord_id"
        ).execute()
        print(f"  Inserted {len(discord_rows)} discord_id wallet rows")

    # 2. Name wallets (names_to_wallets.json)
    names = load_json("names_to_wallets.json") or {}
    name_rows = []
    for name, address in names.items():
        name_rows.append({
            "display_name": name,
            "wallet_address": address or "",
            "source": "name",
        })

    if name_rows:
        # Batch insert; no upsert needed since these have no discord_id
        for i in range(0, len(name_rows), 50):
            batch = name_rows[i:i+50]
            supabase.table("wallets").insert(batch).execute()
        print(f"  Inserted {len(name_rows)} name wallet rows")


def migrate_proposals():
    """Migrate proposals.json into proposals + proposal_votes tables."""
    print("=== Migrating proposals ===")

    data = load_json("proposals.json")
    if not data:
        print("  No proposals data found")
        return

    # Store the index message ID in bot_metadata
    index_msg_id = data.get("_index_message_id")
    if index_msg_id:
        supabase.table("bot_metadata").upsert({
            "key": "proposals_index_message_id",
            "value": str(index_msg_id),
        }, on_conflict="key").execute()

    proposals = data.get("proposals", {})
    for pid, p in proposals.items():
        # Insert proposal
        row = {
            "id": int(pid),
            "title": p["title"],
            "description": p.get("description"),
            "proposal_type": p["type"],
            "author_id": p["author_id"],
            "thread_id": p.get("thread_id"),
            "message_id": p.get("message_id"),
            "status": p["status"],
            "options": p.get("options", []),
            "funding_amount": p.get("funding_amount"),
            "image_url": p.get("image_url"),
            "project_url": p.get("project_url"),
            "created_at": p["created_at"],
            "closed_at": p.get("closed_at"),
        }
        supabase.table("proposals").upsert(row, on_conflict="id").execute()

        # Insert votes
        votes = p.get("votes", {})
        vote_rows = []
        for voter_id, vote_data in votes.items():
            if isinstance(vote_data, str):
                value, weight = vote_data, 1.0
            else:
                value = vote_data["value"]
                weight = vote_data.get("weight", 1.0)
            vote_rows.append({
                "proposal_id": int(pid),
                "voter_id": voter_id,
                "vote_value": value,
                "weight": weight,
            })

        if vote_rows:
            supabase.table("proposal_votes").upsert(
                vote_rows, on_conflict="proposal_id,voter_id"
            ).execute()

    # Reset the sequence to continue from next_id
    next_id = data.get("next_id", len(proposals) + 1)
    supabase.rpc("setval_proposals_id", {"val": next_id}).execute()
    # NOTE: You need to create this RPC function, or run manually:
    #   SELECT setval('proposals_id_seq', <next_id>, false);

    print(f"  Migrated {len(proposals)} proposals")


def migrate_history():
    """Migrate history.json into fractal_sessions + fractal_rankings tables."""
    print("=== Migrating fractal history ===")

    data = load_json("history.json")
    if not data:
        print("  No history data found")
        return

    fractals = data.get("fractals", [])
    for f in fractals:
        # Insert session
        session_row = {
            "id": f["id"],
            "group_name": f["group_name"],
            "facilitator_id": str(f["facilitator_id"]),
            "facilitator_name": f["facilitator_name"],
            "fractal_number": f.get("fractal_number"),
            "group_number": f.get("group_number"),
            "guild_id": str(f["guild_id"]),
            "thread_id": str(f.get("thread_id", "")),
            "completed_at": f["completed_at"],
        }
        supabase.table("fractal_sessions").upsert(
            session_row, on_conflict="id"
        ).execute()

        # Insert rankings
        ranking_rows = []
        for i, r in enumerate(f["rankings"]):
            ranking_rows.append({
                "session_id": f["id"],
                "user_id": str(r["user_id"]),
                "display_name": r["display_name"],
                "level": r["level"],
                "respect": r.get("respect", 0),
                "rank_position": i + 1,
            })

        if ranking_rows:
            supabase.table("fractal_rankings").upsert(
                ranking_rows, on_conflict="session_id,user_id"
            ).execute()

    print(f"  Migrated {len(fractals)} fractal sessions")


def migrate_intros():
    """Migrate intros.json into the intros table."""
    print("=== Migrating intros ===")

    data = load_json("intros.json")
    if not data:
        print("  No intros data found")
        return

    rows = []
    for discord_id, entry in data.items():
        rows.append({
            "discord_id": discord_id,
            "intro_text": entry["text"],
            "message_id": str(entry.get("message_id", "")),
            "posted_at": entry.get("timestamp"),
        })

    if rows:
        supabase.table("intros").upsert(
            rows, on_conflict="discord_id"
        ).execute()
    print(f"  Migrated {len(rows)} intros")


def migrate_events():
    """Migrate events.json into the events table."""
    print("=== Migrating events ===")

    data = load_json("events.json")
    if not data:
        print("  No events data found")
        return

    events = data.get("events", {})
    rows = []
    for slug, ev in events.items():
        rows.append({
            "slug": slug,
            "name": ev["name"],
            "day_of_week": ev["day"],
            "event_time": ev["time"],
            "timezone": ev.get("timezone", "UTC"),
            "channel_id": str(ev["channel_id"]),
            "created_by": str(ev.get("created_by", "")),
            "last_reminded_24h": ev.get("last_reminded_24h"),
            "last_reminded_6h": ev.get("last_reminded_6h"),
            "last_reminded_1h": ev.get("last_reminded_1h"),
        })

    if rows:
        supabase.table("events").upsert(
            rows, on_conflict="slug"
        ).execute()
    print(f"  Migrated {len(rows)} events")


def migrate_hats_roles():
    """Migrate hats_roles.json into hats_role_mappings table."""
    print("=== Migrating hats role mappings ===")

    data = load_json("hats_roles.json")
    if not data:
        print("  No hats roles data found (file may not exist yet)")
        return

    rows = []
    for hat_id_hex, mapping in data.items():
        rows.append({
            "hat_id_hex": hat_id_hex,
            "role_id": str(mapping["role_id"]),
            "hat_name": mapping.get("hat_name"),
        })

    if rows:
        supabase.table("hats_role_mappings").upsert(
            rows, on_conflict="hat_id_hex"
        ).execute()
    print(f"  Migrated {len(rows)} hat-role mappings")


def main():
    print("Starting ZAO Fractal Bot -> Supabase migration\n")
    migrate_wallets()
    migrate_proposals()
    migrate_history()
    migrate_intros()
    migrate_events()
    migrate_hats_roles()
    print("\nMigration complete!")
    print("IMPORTANT: Run these SQL commands to reset sequences:")
    print("  SELECT setval('proposals_id_seq', (SELECT COALESCE(MAX(id), 0) + 1 FROM proposals), false);")
    print("  SELECT setval('fractal_sessions_id_seq', (SELECT COALESCE(MAX(id), 0) + 1 FROM fractal_sessions), false);")


if __name__ == "__main__":
    main()
```

### 3.2 Pre-Migration Checklist

1. Create the Supabase project and run the schema SQL from Section 2.
2. Back up all JSON files: `cp -r data/ data_backup_$(date +%Y%m%d)/`
3. Set `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` environment variables.
4. Run the migration script.
5. Verify data counts match:
   - `SELECT COUNT(*) FROM proposals;` should match `len(proposals.json.proposals)`
   - `SELECT COUNT(*) FROM fractal_sessions;` should match history.json fractal count
   - etc.
6. Reset Postgres sequences so new auto-IDs continue from the right number.

---

## 4. Bot Code Changes

### 4.1 New Dependency: `supabase-py`

Add to `requirements.txt`:

```
supabase>=2.0.0
```

### 4.2 New Utility: `utils/supabase_client.py`

```python
"""Singleton Supabase client used by all cogs."""

import os
from supabase import create_client, Client

_client: Client | None = None

def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
        )
    return _client
```

All cogs import `get_client()` instead of reading JSON files. The bot uses the **service_role key** which bypasses RLS, since the bot is a trusted backend.

### 4.3 Cog-by-Cog Changes

#### 4.3.1 `cogs/proposals.py` -- ProposalStore

**Current:** `ProposalStore` loads `proposals.json` into `self._data` dict, does dict lookups, and calls `atomic_save()` on every mutation.

**New:** Replace with Supabase queries. Remove `_load()`, `_save()`, and all `atomic_save` calls.

| Current Method | New Implementation |
|---|---|
| `__init__` / `_load()` | Remove. No in-memory state needed. |
| `_save()` | Remove. Each mutation writes directly to Supabase. |
| `create(...)` | `supabase.table("proposals").insert({...}).execute()` -- returns the new row with auto-generated ID |
| `get(proposal_id)` | `supabase.table("proposals").select("*").eq("id", pid).single().execute()` |
| `get_active()` | `supabase.table("proposals").select("*").eq("status", "active").execute()` |
| `vote(pid, uid, value, weight)` | `supabase.table("proposal_votes").upsert({"proposal_id": pid, "voter_id": uid, "vote_value": value, "weight": weight}, on_conflict="proposal_id,voter_id").execute()` |
| `close(pid)` | `supabase.table("proposals").update({"status": "closed", "closed_at": now()}).eq("id", pid).execute()` |
| `delete(pid)` | `supabase.table("proposals").delete().eq("id", pid).execute()` (CASCADE deletes votes) |
| `get_vote_summary(pid)` | `supabase.table("proposal_votes").select("vote_value, weight").eq("proposal_id", pid).execute()` then aggregate in Python, OR use an RPC function |
| `index_message_id` property | `supabase.table("bot_metadata").select("value").eq("key", "proposals_index_message_id").single().execute()` |

**Key consideration:** Every Supabase call is a network round-trip (~50-100ms). For the vote summary which is called frequently (on every button click to rebuild the embed), consider:
- Using a Postgres function (`supabase.rpc("get_vote_summary", {"pid": pid})`) for single-query aggregation.
- Keeping a short-lived in-memory cache (5s TTL) for the tally text.

#### 4.3.2 `cogs/wallet.py` -- WalletRegistry

**Current:** Two in-memory dicts (`_discord_wallets`, `_name_wallets`) loaded from JSON files.

**New:** Direct Supabase queries.

| Current Method | New Implementation |
|---|---|
| `register(discord_id, wallet)` | `supabase.table("wallets").upsert({"discord_id": str(did), "wallet_address": wallet, "source": "discord_id"}, on_conflict="discord_id").execute()` |
| `get_by_discord_id(did)` | `supabase.table("wallets").select("wallet_address").eq("discord_id", str(did)).eq("source", "discord_id").maybe_single().execute()` |
| `get_by_name(name)` | `supabase.table("wallets").select("wallet_address").ilike("display_name", name).eq("source", "name").maybe_single().execute()` |
| `lookup(member)` | Chain of queries: by discord_id, then by display_name/username/global_name |
| `get_all_discord()` | `supabase.table("wallets").select("discord_id, wallet_address").eq("source", "discord_id").execute()` |
| `stats()` | `supabase.table("wallets").select("source, wallet_address", count="exact").execute()` plus aggregation |

**Performance note:** The `lookup()` method currently does up to 4 lookups. With Supabase, batch these into a single query using `.or_()`:

```python
async def lookup(self, member) -> str | None:
    sb = get_client()
    # Single query: check discord_id OR any name variant
    result = sb.table("wallets").select("wallet_address, source").or_(
        f"discord_id.eq.{member.id},"
        f"display_name.ilike.{member.display_name},"
        f"display_name.ilike.{member.name}"
    ).execute()
    # Prefer discord_id source, then name
    for row in sorted(result.data, key=lambda r: 0 if r["source"] == "discord_id" else 1):
        if row["wallet_address"]:
            return row["wallet_address"]
    return None
```

#### 4.3.3 `cogs/history.py` -- FractalHistory

**Current:** Append-only list in `history.json` with linear scan queries.

**New:** Two-table Supabase queries with proper SQL joins.

| Current Method | New Implementation |
|---|---|
| `record(...)` | INSERT into `fractal_sessions`, then batch INSERT into `fractal_rankings` |
| `get_all()` | `supabase.table("fractal_sessions").select("*, fractal_rankings(*)").execute()` |
| `get_recent(count)` | `.select("*, fractal_rankings(*)").order("completed_at", desc=True).limit(count)` |
| `get_by_user(user_id)` | `.select("*, fractal_rankings!inner(*)").eq("fractal_rankings.user_id", uid)` |
| `get_user_stats(user_id)` | Use the `respect_leaderboard` VIEW: `.select("*").eq("user_id", uid).single()` |
| `get_leaderboard()` | `supabase.table("respect_leaderboard").select("*").execute()` -- the VIEW does all the work |
| `search(query)` | `.select("*, fractal_rankings(*)").or_(f"group_name.ilike.%{q}%,fractal_number.ilike.%{q}%")` |
| `total_fractals` | `.select("*", count="exact", head=True)` on `fractal_sessions` |

**Big win:** The `get_leaderboard()` method currently does a full linear scan of all fractals and all rankings. The `respect_leaderboard` VIEW replaces this with a single indexed query.

#### 4.3.4 `cogs/intro.py` -- IntroCache

**Current:** In-memory dict backed by `intros.json`.

**New:** Direct Supabase queries with optional in-memory cache.

| Current Method | New Implementation |
|---|---|
| `get(discord_id)` | `.select("*").eq("discord_id", str(did)).maybe_single()` |
| `set(discord_id, text, msg_id, ts)` | `.upsert({"discord_id": str(did), "intro_text": text, "message_id": str(msg_id), "posted_at": ts}, on_conflict="discord_id")` |
| `clear()` | `.delete().neq("id", 0)` (delete all rows) |
| `size` | `.select("*", count="exact", head=True)` |

Since intros are read frequently but written rarely, keep a lightweight in-memory TTL cache (60s) to avoid repeated network calls for `/intro` lookups.

#### 4.3.5 `cogs/events.py` -- Events

**Current:** Module-level `_load_events()` / `_save_events()` functions reading `events.json`.

**New:** Direct Supabase queries.

| Current Function | New Implementation |
|---|---|
| `_load_events()` | `supabase.table("events").select("*").execute()` -- transform into dict keyed by slug |
| `_save_events(data)` | Individual upsert/delete calls per event |
| Schedule create | `.insert({...})` |
| Edit event | `.update({...}).eq("slug", key)` |
| Cancel event | `.delete().eq("slug", key)` |
| Reminder loop | `.select("*")` each minute, then `.update({"last_reminded_24h": ...}).eq("slug", key)` after sending |

#### 4.3.6 `cogs/hats.py` -- HatsRoleMapping

**Current:** `hats_roles.json` dict of hat_id_hex to {role_id, hat_name}.

**New:**

| Current Method | New Implementation |
|---|---|
| `set(hat_id_hex, role_id, hat_name)` | `.upsert({...}, on_conflict="hat_id_hex")` |
| `remove(hat_id_hex)` | `.delete().eq("hat_id_hex", hat_id_hex)` |
| `get_all()` | `.select("*")` |

### 4.4 Removing `utils/safe_json.py`

After migration, `atomic_save` is no longer needed. The file can be removed (or kept temporarily for a rollback safety net).

### 4.5 Async Considerations

The current `supabase-py` client (v2.x) uses `httpx` under the hood and supports sync operations. For async operations inside discord.py's event loop:

**Option A (recommended):** Use `supabase-py` sync client inside `asyncio.to_thread()`:

```python
result = await asyncio.to_thread(
    lambda: supabase.table("proposals").select("*").eq("id", pid).single().execute()
)
```

**Option B:** Use the lower-level `postgrest-py` async client directly:

```python
from postgrest import AsyncPostgrestClient
async_client = AsyncPostgrestClient(f"{SUPABASE_URL}/rest/v1", headers={...})
```

**Option C:** Use `supabase-py` v2.x `acreate_client` (async factory, if available in the version you install).

Option A is the simplest migration path since it requires the least code change.

---

## 5. Web App Changes

### 5.1 Current Architecture Problem

The web app (`web/`) currently uses two data sources that cannot coexist in production:

1. **Drizzle ORM + Neon Postgres** (`utils/database.ts`, `utils/schema.ts`) -- a full relational schema that is largely unused.
2. **Direct JSON file reads** (`utils/loadJsonData.ts`) -- reads `wallets.json`, `history.json`, etc. from the filesystem. Every API endpoint (`dashboard/stats.ts`, `dashboard/leaderboard.ts`, `dashboard/members/[slug].ts`) uses this.

The JSON approach only works when the web app runs on the same machine as the bot. In any deployed environment (Vercel, Railway, etc.), it fails.

### 5.2 New Architecture

Replace **both** data sources with a single Supabase client:

```
[Discord Bot] --writes--> [Supabase Postgres] <--reads-- [Next.js Web App]
```

#### 5.2.1 Install Supabase JS Client

```bash
cd web
npm install @supabase/supabase-js
```

#### 5.2.2 New `utils/supabase.ts`

```typescript
import { createClient } from '@supabase/supabase-js'

// Server-side client (for API routes, using service_role key)
export const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!
)

// Client-side client (for browser, using anon key -- respects RLS)
export const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
)
```

#### 5.2.3 Replace `loadJsonData.ts`

Delete `utils/loadJsonData.ts` entirely. Replace each function with Supabase queries:

| Old Function | New Implementation |
|---|---|
| `loadNamesToWallets()` | `supabaseAdmin.from("wallets").select("display_name, wallet_address").eq("source", "name")` |
| `loadWallets()` | `supabaseAdmin.from("wallets").select("discord_id, wallet_address").eq("source", "discord_id")` |
| `loadHistory()` | `supabaseAdmin.from("fractal_sessions").select("*, fractal_rankings(*)")` |
| `loadIntros()` | `supabaseAdmin.from("intros").select("*")` |
| `loadProposals()` | `supabaseAdmin.from("proposals").select("*, proposal_votes(*)")` |
| `computeOffchainLeaderboard()` | `supabaseAdmin.from("respect_leaderboard").select("*")` -- the VIEW computes this |
| `getMemberStats()` | `supabaseAdmin.from("respect_leaderboard").select("*").eq("display_name", name)` |

#### 5.2.4 API Endpoint Changes

**`pages/api/dashboard/stats.ts`:**
```typescript
// Before: loadNamesToWallets(), loadHistory(), loadProposals() from JSON
// After:
const { count: totalMembers } = await supabaseAdmin
  .from("wallets").select("*", { count: "exact", head: true }).eq("source", "name");
const { count: totalFractals } = await supabaseAdmin
  .from("fractal_sessions").select("*", { count: "exact", head: true });
const { count: activeProposals } = await supabaseAdmin
  .from("proposals").select("*", { count: "exact", head: true }).eq("status", "active");
// Offchain respect: SUM from the view
const { data: lb } = await supabaseAdmin.from("respect_leaderboard").select("total_respect");
const totalOffchainRespect = lb?.reduce((s, r) => s + r.total_respect, 0) ?? 0;
```

**`pages/api/dashboard/leaderboard.ts`:**
```typescript
// Before: complex merge of loadNamesToWallets + loadHistory + onchain balances
// After: the respect_leaderboard VIEW provides offchain data; merge with onchain
const { data: offchain } = await supabaseAdmin.from("respect_leaderboard").select("*");
const { data: wallets } = await supabaseAdmin
  .from("wallets").select("display_name, wallet_address")
  .eq("source", "name").neq("wallet_address", "");
// Then fetch onchain balances and merge as before
```

**`pages/api/dashboard/members/[slug].ts`:**
```typescript
// Replace getMemberStats() with a single Supabase query
const { data } = await supabaseAdmin
  .from("respect_leaderboard").select("*")
  .ilike("display_name", memberName).single();
```

#### 5.2.5 Remove Drizzle / Neon

Since Supabase replaces the Neon database, remove:
- `utils/database.ts` (Neon/Drizzle connection)
- `utils/schema.ts` (Drizzle schema) -- or refactor to generate Supabase types
- `drizzle.config.ts`
- Dependencies: `@neondatabase/serverless`, `drizzle-orm`, `drizzle-kit`

#### 5.2.6 Auth: Discord OAuth via Supabase

Supabase has a built-in Discord OAuth provider. Replace the current NextAuth setup:

1. In Supabase Dashboard: Authentication > Providers > Discord > Enable
2. Enter your Discord app's Client ID and Client Secret.
3. Set the redirect URL to `https://your-supabase-url.supabase.co/auth/v1/callback`.

In the web app:

```typescript
// Login
const { data, error } = await supabase.auth.signInWithOAuth({
  provider: 'discord',
  options: { redirectTo: window.location.origin + '/dashboard' }
})

// Get current user
const { data: { user } } = await supabase.auth.getUser()
// user.user_metadata.provider_id = Discord user ID
// user.user_metadata.full_name = Discord display name
```

This eliminates `pages/api/auth/[...nextauth].ts` and the `next-auth` dependency.

#### 5.2.7 Realtime Subscriptions

Enable live updates on the dashboard without polling:

```typescript
// In a React component or hook:
import { supabase } from '../utils/supabase'

useEffect(() => {
  const channel = supabase
    .channel('dashboard-updates')
    .on('postgres_changes', {
      event: '*',
      schema: 'public',
      table: 'proposals',
    }, (payload) => {
      // Refresh proposals data
      refetchProposals()
    })
    .on('postgres_changes', {
      event: 'INSERT',
      schema: 'public',
      table: 'fractal_sessions',
    }, (payload) => {
      // New fractal completed -- refresh leaderboard
      refetchLeaderboard()
    })
    .subscribe()

  return () => { supabase.removeChannel(channel) }
}, [])
```

#### 5.2.8 Generated TypeScript Types

Use the Supabase CLI to auto-generate TypeScript types from the database schema:

```bash
npx supabase gen types typescript --project-id YOUR_PROJECT_ID > web/types/supabase.ts
```

This replaces the manually maintained `types/dashboard.ts` with auto-generated types that stay in sync with the database.

---

## 6. Environment Setup

### 6.1 Supabase Project

1. Go to [supabase.com](https://supabase.com) and create a new project.
2. Region: choose the closest to your bot's hosting (e.g., US East if on Railway).
3. Note the project URL and keys from Settings > API.

### 6.2 Environment Variables

**Bot (Python):**

```env
# Existing vars (unchanged)
DISCORD_TOKEN=...
OPTIMISM_RPC_URL=...

# New: Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=eyJ...service_role_key...  # Full access, bypasses RLS
```

**Web App (Next.js):**

```env
# Remove these:
# DATABASE_URL=...  (was Neon Postgres)
# NEXTAUTH_SECRET=...
# DISCORD_CLIENT_ID=...  (move to Supabase Dashboard instead)
# DISCORD_CLIENT_SECRET=...

# Add these:
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...anon_key...  # Public, respects RLS
SUPABASE_SERVICE_ROLE_KEY=eyJ...service_role_key...  # Server-side only, never expose to client
```

### 6.3 Supabase Dashboard Config

1. **Authentication > Providers > Discord:** Enable and configure with your Discord app credentials.
2. **Authentication > URL Configuration:** Set site URL and redirect URLs.
3. **Database > Extensions:** Ensure `uuid-ossp` is enabled (should be by default).
4. **Realtime:** Confirm the 4 tables are added to the `supabase_realtime` publication.

---

## 7. Rollout Plan

### Phase 1: Setup and Migration (1 day)

1. Create the Supabase project.
2. Run the schema SQL (Section 2.1) in the SQL Editor.
3. Run the migration script (Section 3.1) to populate all tables.
4. Verify data integrity: compare row counts and spot-check values.
5. Reset Postgres sequences.

### Phase 2: Dual-Write Bot (2-3 days)

Modify the bot to **write to both JSON files and Supabase** but **read from JSON files only**. This is the safety net phase.

- Each `_save()` call also writes to Supabase.
- If anything fails on the Supabase side, log the error but do not break the bot.
- Monitor Supabase data to confirm parity with JSON files over a few days.

### Phase 3: Switch Bot Reads (1 day)

- Change the bot to **read from Supabase** instead of JSON files.
- Keep JSON writes as a fallback (can be removed later).
- Test all commands:
  - `/propose`, `/curate`, vote on proposals, auto-close
  - `/register`, `/wallet`, `/admin_wallets`
  - `/history`, `/mystats`, `/rankings`
  - `/intro`, `/admin_refresh_intros`
  - `/schedule`, `/events`, `/cancel_event`, `/edit_event`
  - `/hats`, `/myhats`, role sync loop

### Phase 4: Switch Web App (1 day)

- Replace `loadJsonData.ts` calls with Supabase queries.
- Remove Drizzle/Neon dependencies.
- Deploy to Vercel/Railway and verify the dashboard works without local file access.
- Enable Realtime subscriptions for live updates.

### Phase 5: Auth Migration (1 day)

- Switch from NextAuth to Supabase Auth with Discord provider.
- Update protected routes to use `supabase.auth.getUser()`.
- Remove `next-auth` dependency.

### Phase 6: Cleanup (1 day)

- Remove all JSON file read/write code from the bot.
- Remove `utils/safe_json.py`.
- Remove `utils/loadJsonData.ts` from the web app.
- Remove `utils/database.ts` and `utils/schema.ts` (Drizzle).
- Archive the `data/*.json` files (keep as backup, do not delete).
- Update `.env.example` files with new variables.
- Update `README.md` with new architecture.

### Rollback Plan

At any phase, if Supabase causes issues:
- **Phase 2-3:** The bot can fall back to JSON files since dual-write keeps them in sync.
- **Phase 4-5:** The web app can be reverted to the previous commit.
- **Nuclear option:** The JSON files are the authoritative backup throughout the migration. They can be restored at any time.

### Estimated Timeline

| Phase | Duration | Risk |
|-------|----------|------|
| 1. Setup + Migration | 1 day | Low -- one-time data import |
| 2. Dual-Write | 2-3 days | Low -- JSON is still primary |
| 3. Switch Bot Reads | 1 day | Medium -- bot depends on network |
| 4. Switch Web App | 1 day | Low -- web app was already broken without local files |
| 5. Auth Migration | 1 day | Low -- can keep old auth temporarily |
| 6. Cleanup | 1 day | Low -- removing dead code |
| **Total** | **7-8 days** | |

---

## Appendix: File Inventory

Files that will be **modified**:

| File | Change |
|------|--------|
| `cogs/proposals.py` | Replace ProposalStore with Supabase queries |
| `cogs/wallet.py` | Replace WalletRegistry with Supabase queries |
| `cogs/history.py` | Replace FractalHistory with Supabase queries |
| `cogs/intro.py` | Replace IntroCache with Supabase queries |
| `cogs/events.py` | Replace `_load_events`/`_save_events` with Supabase queries |
| `cogs/hats.py` | Replace HatsRoleMapping with Supabase queries |
| `requirements.txt` | Add `supabase>=2.0.0` |
| `web/package.json` | Add `@supabase/supabase-js`, remove Drizzle/Neon/NextAuth |
| `web/pages/api/dashboard/*.ts` | Replace JSON reads with Supabase queries |
| `web/pages/api/auth/[...nextauth].ts` | Replace with Supabase Auth |

Files that will be **created**:

| File | Purpose |
|------|---------|
| `utils/supabase_client.py` | Singleton Supabase Python client |
| `web/utils/supabase.ts` | Supabase JS client (admin + anon) |
| `web/types/supabase.ts` | Auto-generated types from database |
| `scripts/migrate_to_supabase.py` | One-time data migration script |

Files that will be **removed**:

| File | Reason |
|------|--------|
| `utils/safe_json.py` | No longer writing JSON files |
| `web/utils/loadJsonData.ts` | Replaced by Supabase queries |
| `web/utils/database.ts` | Replaced by Supabase (was Neon/Drizzle) |
| `web/utils/schema.ts` | Replaced by Supabase schema + generated types |
| `web/drizzle.config.ts` | No longer using Drizzle |

-- ============================================================
-- ZAO Fractal Bot -- Supabase Schema (discord-prefixed tables)
-- Run this as a single transaction in the Supabase SQL Editor
--
-- IMPORTANT: This script assumes ZAO OS already has these tables:
--   - users (discord_id, primary_wallet, display_name, fid, etc.)
--   - fractal_sessions (id UUID, session_date, name, host_name,
--       host_wallet, scoring_era, participant_count, notes)
--   - fractal_scores (id UUID, session_id FK, member_name,
--       wallet_address, rank, score)
--   - respect_members (name, wallet_address, total_respect, etc.)
--
-- This script:
--   1. Adds Discord-specific columns to existing ZAO OS tables
--   2. Creates discord_ prefixed tables for bot-only data
--   3. Creates views, RLS policies, and Realtime publication
-- ============================================================

-- Enable UUID generation (Supabase has this by default)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ──────────────────────────────────────────────────────────────
-- 1. ADD COLUMNS TO EXISTING ZAO OS TABLES
-- ──────────────────────────────────────────────────────────────

-- fractal_sessions: Discord-specific metadata
ALTER TABLE fractal_sessions ADD COLUMN IF NOT EXISTS thread_id TEXT;
ALTER TABLE fractal_sessions ADD COLUMN IF NOT EXISTS facilitator_discord_id TEXT;
ALTER TABLE fractal_sessions ADD COLUMN IF NOT EXISTS group_number TEXT;
ALTER TABLE fractal_sessions ADD COLUMN IF NOT EXISTS guild_id TEXT;
ALTER TABLE fractal_sessions ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

-- fractal_scores: Discord user link and computed fields
ALTER TABLE fractal_scores ADD COLUMN IF NOT EXISTS discord_id TEXT;
ALTER TABLE fractal_scores ADD COLUMN IF NOT EXISTS level INTEGER;
ALTER TABLE fractal_scores ADD COLUMN IF NOT EXISTS respect_points INTEGER DEFAULT 0;

-- Indexes on new columns
CREATE INDEX IF NOT EXISTS idx_fractal_sessions_thread_id
    ON fractal_sessions(thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fractal_sessions_facilitator_discord
    ON fractal_sessions(facilitator_discord_id) WHERE facilitator_discord_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fractal_sessions_completed
    ON fractal_sessions(completed_at DESC) WHERE completed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fractal_scores_discord_id
    ON fractal_scores(discord_id) WHERE discord_id IS NOT NULL;

-- ──────────────────────────────────────────────────────────────
-- 2. DISCORD PROPOSALS
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS discord_proposals (
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

CREATE INDEX IF NOT EXISTS idx_discord_proposals_status ON discord_proposals(status);
CREATE INDEX IF NOT EXISTS idx_discord_proposals_author ON discord_proposals(author_id);
CREATE INDEX IF NOT EXISTS idx_discord_proposals_thread ON discord_proposals(thread_id);

COMMENT ON TABLE discord_proposals IS 'Governance proposals created via Discord /propose and /curate commands. Votes stored in discord_proposal_votes.';

-- ──────────────────────────────────────────────────────────────
-- 3. DISCORD PROPOSAL VOTES
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS discord_proposal_votes (
    id              BIGSERIAL PRIMARY KEY,
    proposal_id     BIGINT NOT NULL REFERENCES discord_proposals(id) ON DELETE CASCADE,
    voter_id        VARCHAR(64) NOT NULL,         -- Discord user ID
    vote_value      VARCHAR(100) NOT NULL,        -- 'yes', 'no', 'abstain', or governance option text
    weight          DECIMAL(18,4) NOT NULL DEFAULT 1.0,  -- Respect-weighted vote power
    voted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(proposal_id, voter_id)                 -- one vote per user per proposal
);

CREATE INDEX IF NOT EXISTS idx_discord_proposal_votes_proposal ON discord_proposal_votes(proposal_id);
CREATE INDEX IF NOT EXISTS idx_discord_proposal_votes_voter ON discord_proposal_votes(voter_id);

COMMENT ON TABLE discord_proposal_votes IS 'Individual Respect-weighted votes on Discord proposals. UNIQUE constraint enforces one vote per user; re-voting UPSERTs.';

-- ──────────────────────────────────────────────────────────────
-- 4. DISCORD INTROS
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS discord_intros (
    id              BIGSERIAL PRIMARY KEY,
    discord_id      VARCHAR(64) NOT NULL UNIQUE,
    intro_text      TEXT NOT NULL,
    message_id      VARCHAR(64),                  -- Discord message snowflake
    posted_at       TIMESTAMPTZ,                  -- when the intro was originally posted
    cached_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_discord_intros_discord_id ON discord_intros(discord_id);

COMMENT ON TABLE discord_intros IS 'Cached introductions from the #intros Discord channel.';

-- ──────────────────────────────────────────────────────────────
-- 5. DISCORD EVENTS
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS discord_events (
    id              BIGSERIAL PRIMARY KEY,
    slug            VARCHAR(255) NOT NULL UNIQUE,  -- URL-safe key (e.g. 'weekly-fractal')
    name            VARCHAR(255) NOT NULL,
    day_of_week     VARCHAR(20) NOT NULL,          -- 'monday'..'sunday'
    event_time      VARCHAR(10) NOT NULL,          -- '18:00' (24h format)
    timezone        VARCHAR(100) NOT NULL DEFAULT 'UTC',
    channel_id      VARCHAR(64) NOT NULL,          -- Discord channel for reminders
    created_by      VARCHAR(64) NOT NULL,          -- Discord user who created it
    last_reminded_24h VARCHAR(64),                 -- ISO timestamp of last 24h reminder
    last_reminded_6h  VARCHAR(64),
    last_reminded_1h  VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE discord_events IS 'Recurring weekly events with automatic Discord reminders at 24h, 6h, and 1h.';

-- ──────────────────────────────────────────────────────────────
-- 6. DISCORD HATS ROLE MAPPINGS
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS discord_hats_role_mappings (
    id              BIGSERIAL PRIMARY KEY,
    hat_id_hex      VARCHAR(66) NOT NULL UNIQUE,   -- 0x-prefixed 64-char hex hat ID
    role_id         VARCHAR(64) NOT NULL,           -- Discord role snowflake
    hat_name        VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE discord_hats_role_mappings IS 'Maps Hats Protocol onchain hat IDs to Discord roles for automatic role sync.';

-- ──────────────────────────────────────────────────────────────
-- 7. DISCORD BOT METADATA
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS discord_bot_metadata (
    key             VARCHAR(100) PRIMARY KEY,
    value           TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE discord_bot_metadata IS 'Key-value store for bot-level config (e.g., proposals index message ID).';

-- ──────────────────────────────────────────────────────────────
-- 8. VIEWS
-- ──────────────────────────────────────────────────────────────

-- NOTE: respect_leaderboard already exists in ZAO OS (managed there).
-- FractalBot uses respect_members table directly for leaderboard data.

-- Active proposals with vote summary (reads from discord_proposals)
CREATE OR REPLACE VIEW active_proposals_summary AS
SELECT
    p.id,
    p.title,
    p.proposal_type,
    p.status,
    p.created_at,
    COUNT(v.id)       AS total_votes,
    SUM(v.weight)     AS total_weight,
    COUNT(v.id) FILTER (WHERE v.vote_value = 'yes')  AS yes_count,
    SUM(v.weight) FILTER (WHERE v.vote_value = 'yes') AS yes_weight,
    COUNT(v.id) FILTER (WHERE v.vote_value = 'no')   AS no_count,
    SUM(v.weight) FILTER (WHERE v.vote_value = 'no')  AS no_weight
FROM discord_proposals p
LEFT JOIN discord_proposal_votes v ON v.proposal_id = p.id
WHERE p.status = 'active'
GROUP BY p.id;

COMMENT ON VIEW active_proposals_summary IS 'Active Discord proposals with aggregated vote counts and Respect-weighted tallies.';

-- ──────────────────────────────────────────────────────────────
-- 9. ROW LEVEL SECURITY (RLS)
-- ──────────────────────────────────────────────────────────────
-- Enable RLS on all discord_ tables. The bot uses the service_role key
-- (bypasses RLS). The web app uses the anon key with these policies.

ALTER TABLE discord_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE discord_proposal_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE discord_intros ENABLE ROW LEVEL SECURITY;
ALTER TABLE discord_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE discord_hats_role_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE discord_bot_metadata ENABLE ROW LEVEL SECURITY;

-- Public read access for dashboard (anon key)
CREATE POLICY "Public read" ON discord_proposals FOR SELECT USING (true);
CREATE POLICY "Public read" ON discord_proposal_votes FOR SELECT USING (true);
CREATE POLICY "Public read" ON discord_intros FOR SELECT USING (true);
CREATE POLICY "Public read" ON discord_events FOR SELECT USING (true);
CREATE POLICY "Public read" ON discord_hats_role_mappings FOR SELECT USING (true);
CREATE POLICY "Public read" ON discord_bot_metadata FOR SELECT USING (true);

-- Authenticated users can vote (the bot handles validation, but this
-- allows direct web voting in the future)
CREATE POLICY "Authenticated vote" ON discord_proposal_votes
    FOR INSERT
    WITH CHECK (auth.role() = 'authenticated');

-- ──────────────────────────────────────────────────────────────
-- 10. TRIGGERS: auto-update updated_at
-- ──────────────────────────────────────────────────────────────

-- update_updated_at() likely already exists in ZAO OS; CREATE OR REPLACE is safe
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER discord_bot_metadata_updated_at
    BEFORE UPDATE ON discord_bot_metadata
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ──────────────────────────────────────────────────────────────
-- 11. REALTIME: enable for dashboard live updates
-- ──────────────────────────────────────────────────────────────

ALTER PUBLICATION supabase_realtime ADD TABLE discord_proposals, discord_proposal_votes;

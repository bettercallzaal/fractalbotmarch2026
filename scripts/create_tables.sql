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

-- Enable Realtime for live dashboard updates
ALTER PUBLICATION supabase_realtime ADD TABLE proposals, proposal_votes, fractal_sessions, fractal_rankings;
